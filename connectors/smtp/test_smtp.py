import smtplib
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.billing.models import Invoice
from crm.billing.reminders import queue_due, send_reminder
from crm.billing.services import add_line, create_plan, issue_invoice, record_payment
from crm.comms import system_email
from crm.comms.models import Communication
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, OutboundRequest
from crm.connect.services import acknowledge_data_flows
from crm.connect.testing import ConnectorContract
from crm.models import AuditLog, FirmProfile, IntakeForm, IntakeInvite
from crm.tests.factories import make_contact, make_matter, make_user

from . import SmtpConnector

CLIENT = "lena@example.test"
PASSWORD = "fictional-smtp-password"
CREDENTIALS = {"username": "crm-sender", "password": PASSWORD}
CONFIG = {
    "smtp_host": "smtp.example.test",
    "smtp_port": "587",
    "smtp_security": "starttls",
    "from_address": "office@firm.example.test",
    "from_name": "",
    "reply_to": "",
}


class FakeSMTP:
    sent = []
    opened = []
    password = PASSWORD
    refuse = None

    def __init__(self, host, port, context=None, timeout=None):
        FakeSMTP.opened.append((self.__class__.__name__, host, port))
        self.tls = False

    def starttls(self, context=None):
        self.tls = context is not None

    def login(self, user, password):
        if password != self.password:
            raise smtplib.SMTPAuthenticationError(535, b"Authentication failed")

    def send_message(self, message):
        if self.refuse:
            raise self.refuse
        self.sent.append((message, self.tls))

    def quit(self):
        raise smtplib.SMTPServerDisconnected("Gone already")

    def close(self):
        pass


class FakeSMTPMixin:
    def setUp(self):
        super().setUp()
        FakeSMTP.sent, FakeSMTP.opened, FakeSMTP.refuse = [], [], None
        for target in ("smtplib.SMTP", "smtplib.SMTP_SSL"):
            patcher = mock.patch(target, FakeSMTP)
            patcher.start()
            self.addCleanup(patcher.stop)


class SmtpContractTests(FakeSMTPMixin, ConnectorContract, TestCase):
    connector_class = SmtpConnector
    valid_credentials = CREDENTIALS
    invalid_credentials = {"username": "crm-sender", "password": "wrong"}
    connection_config = CONFIG
    outbound_examples = {
        "send_system_email": {
            "to": CLIENT,
            "subject": "Hello",
            "body": "Fictional.",
            "sender_name": "Fictional Law Office",
        }
    }


class SmtpTestCase(FakeSMTPMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user()
        self.contact = make_contact(self.admin, first_name="Lena", email=CLIENT)
        profile = FirmProfile.load()
        profile.name = "Fictional Law Office"
        profile.save()
        self.connection = self.connect()

    def connect(self, **overrides):
        connection = Connection.objects.create(
            connector_key="smtp",
            label="Firm email",
            enabled_capabilities=["system_email"],
            credentials=dict(CREDENTIALS),
            config={**CONFIG, **overrides},
            preview_mode=False,
        )
        acknowledge_data_flows(connection, self.admin)
        return connection

    def message(self):
        self.assertEqual(len(FakeSMTP.sent), 1)
        return FakeSMTP.sent[0][0]


class SmtpSendTests(SmtpTestCase):
    def test_connecting_through_the_page(self):
        self.connection.delete()
        self.client.force_login(self.admin)
        page = self.client.get(reverse("connector_detail", args=["smtp"]))
        self.assertContains(page, "Send from this address")
        self.client.post(
            reverse("connector_detail", args=["smtp"]),
            {
                "label": "Firm email",
                "capabilities": ["system_email"],
                "credential_username": "crm-sender",
                "credential_password": PASSWORD,
                "setting_smtp_host": "smtp.example.test",
                "setting_smtp_port": "587",
                "setting_smtp_security": "starttls",
                "setting_from_address": "office@firm.example.test",
                "acknowledge": "on",
            },
        )
        connection = Connection.objects.get()
        self.assertEqual(connection.status, Connection.Status.ACTIVE)
        response = self.client.post(
            reverse("connection_config", args=[connection.pk]),
            {**{f"setting_{k}": v for k, v in CONFIG.items()}, "setting_from_address": "office"},
            follow=True,
        )
        self.assertContains(response, "Enter an email address, such as office@example.test.")
        self.assertEqual(FakeSMTP.opened, [("FakeSMTP", "smtp.example.test", 587)])
        self.assertEqual(system_email.sender(), connection)

    def test_an_email_from_the_firm(self):
        sent = system_email.send_system_email(
            self.contact,
            subject="Your  secure\nlink",
            body="Hello Lena.\n",
            log_summary="A fictional summary.",
            purpose="Intake link",
            actor=self.admin,
        )
        self.assertEqual(sent.status, OutboundRequest.Status.SENT)
        self.assertEqual(sent.summary, "Intake link email")
        message = self.message()
        self.assertTrue(FakeSMTP.sent[0][1])  # STARTTLS with a checked certificate.
        self.assertEqual(message["From"], "Fictional Law Office <office@firm.example.test>")
        self.assertEqual((message["To"], message["Subject"]), (CLIENT, "Your secure link"))
        self.assertEqual(message["Auto-Submitted"], "auto-generated")
        self.assertIsNone(message["Reply-To"])
        self.assertTrue(message["Message-ID"].endswith("@firm.example.test>"))
        logged = Communication.objects.get()
        self.assertEqual(
            (logged.channel, logged.direction, logged.counterpart, logged.summary),
            ("email", "outbound", CLIENT, "A fictional summary."),
        )
        self.assertEqual(logged.external_id, sent.provider_ref)

    def test_sender_name_reply_to_and_ssl(self):
        self.connection.config = {
            **CONFIG,
            "smtp_port": "465",
            "smtp_security": "ssl",
            "from_name": "Front Desk",
            "reply_to": "desk@firm.example.test",
        }
        self.connection.save()
        SmtpConnector(self.connection).perform(
            "send_system_email",
            {"to": CLIENT, "subject": "Hi", "body": "Hello.", "sender_name": "Ignored"},
        )
        message = self.message()
        self.assertEqual(FakeSMTP.opened, [("FakeSMTP", "smtp.example.test", 465)])
        self.assertEqual(message["From"], "Front Desk <office@firm.example.test>")
        self.assertEqual(message["Reply-To"], "desk@firm.example.test")

    def test_failures_are_explained(self):
        connector = SmtpConnector(self.connection)
        payload = {"to": CLIENT, "subject": "Hi", "body": "Hello.", "sender_name": "Firm"}
        cases = [
            (smtplib.SMTPRecipientsRefused({}), "refused the client's address"),
            (smtplib.SMTPSenderRefused(550, b"No", "x"), "refused the sending address"),
            (smtplib.SMTPDataError(554, b"No"), "did not take the email"),
        ]
        for error, text in cases:
            FakeSMTP.refuse = error
            with self.assertRaisesMessage(ConnectorError, text):
                connector.perform("send_system_email", payload)
        with self.assertRaisesMessage(ConnectorError, "not valid"):
            connector.perform("send_system_email", {**payload, "to": "a\nb@example.test"})
        with self.assertRaisesMessage(ConnectorError, "cannot send_text"):
            connector.perform("send_text", payload)
        with mock.patch.object(FakeSMTP, "password", "other"):
            with self.assertRaisesMessage(ConnectorError, "sign-in name and password"):
                connector.test_connection()
        with mock.patch("smtplib.SMTP", side_effect=OSError("refused")):
            with self.assertRaisesMessage(ConnectorError, "could not be reached securely"):
                connector.test_connection()

    def test_which_connection_sends(self):
        self.assertEqual(system_email.sender(), self.connection)
        later = self.connect()
        self.assertEqual(system_email.sender(), self.connection)
        self.connection.status = Connection.Status.NEEDS_ATTENTION
        self.connection.save()
        self.assertEqual(system_email.sender(), later)
        later.enabled_capabilities = []
        later.save()
        self.assertIsNone(system_email.sender())
        with self.assertRaisesMessage(Exception, "Connect an email delivery service first."):
            system_email.send_system_email(
                self.contact, subject="Hi", body="Hello", log_summary="", purpose="Test"
            )

    def test_held_in_preview_mode_and_bad_addresses(self):
        self.connection.preview_mode = True
        self.connection.save()
        held = system_email.send_system_email(
            self.contact, subject="Hi", body="Hello", log_summary="", purpose="Test"
        )
        self.assertEqual(held.status, OutboundRequest.Status.PREVIEWED)
        self.assertTrue(held.is_automation)
        self.assertEqual(
            system_email.not_sent_reason(held), "the email delivery service is in preview mode"
        )
        self.assertEqual((FakeSMTP.sent, Communication.objects.count()), ([], 0))
        self.contact.email = "not an address"
        with self.assertRaisesMessage(Exception, "Add a valid email address for Lena Example"):
            system_email.send_system_email(
                self.contact, subject="Hi", body="Hello", log_summary="", purpose="Test"
            )


class IntakeLinkEmailTests(SmtpTestCase):
    def setUp(self):
        super().setUp()
        self.intake_form = IntakeForm.objects.create(name="Family questions")
        self.matter = make_matter(self.contact, self.admin)
        self.client.force_login(self.admin)

    def create(self, **extra):
        return self.client.post(
            reverse("intake_invite_create"),
            {
                "intake_form": self.intake_form.pk,
                "contact": self.contact.pk,
                "matter": self.matter.pk,
                "expires_in_days": 7,
                **extra,
            },
        )

    def test_the_link_is_emailed_and_shown_once(self):
        page = self.client.get(reverse("intake_invite_create"))
        self.assertContains(page, "Email the link to the contact")
        response = self.create(send_email="on")
        self.assertContains(response, f"Emailed to {CLIENT}.")
        url = response.context["intake_url"]
        self.assertContains(response, url)
        message = self.message()
        self.assertEqual(
            message["Subject"], "Family questions: your secure link from Fictional Law Office"
        )
        self.assertIn(url, message.get_content())
        self.assertIn("Hello Lena,", message.get_content())
        logged = Communication.objects.get()
        self.assertEqual(logged.matter, self.matter)
        self.assertNotIn(url.rstrip("/").rsplit("/", 1)[-1], logged.summary)
        self.assertIn('Secure intake link for "Family questions"', logged.summary)
        self.assertTrue(AuditLog.objects.filter(action="intake_invite.emailed").exists())

    def test_staff_can_still_copy_it(self):
        response = self.create()
        self.assertNotContains(response, "Emailed to")
        self.assertContains(response, "Copy this link now.")
        self.assertEqual(FakeSMTP.sent, [])

    def test_an_email_that_is_not_sent_says_why(self):
        self.connection.preview_mode = True
        self.connection.save()
        response = self.create(send_email="on")
        self.assertContains(
            response,
            "Not emailed: the email delivery service is in preview mode. Copy the link instead.",
        )
        self.assertEqual(IntakeInvite.objects.count(), 1)
        self.connection.preview_mode = False
        self.connection.save()
        with mock.patch.object(system_email, "address_problem", return_value="Gone"):
            response = self.create(send_email="on")
        self.assertContains(response, "Not emailed: Gone. Copy the link instead.")
        FakeSMTP.refuse = smtplib.SMTPRecipientsRefused({})
        response = self.create(send_email="on")
        self.assertContains(
            response, "Not emailed: The mail server refused the client&#x27;s address."
        )

    def test_a_contact_without_an_address(self):
        self.contact.email = ""
        self.contact.save()
        response = self.create(send_email="on")
        self.assertContains(response, "Add a valid email address for Lena Example first.")
        self.assertFalse(IntakeInvite.objects.exists())


class PaymentReminderEmailTests(SmtpTestCase):
    def setUp(self):
        super().setUp()
        self.invoice = Invoice.objects.create(contact=self.contact)
        add_line(self.invoice, description="Fictional consultation", unit_price=Decimal("600"))
        self.invoice = issue_invoice(self.invoice, self.admin)
        self.today = timezone.localdate()

    def plan(self, **overrides):
        options = {
            "count": 3,
            "first_due_date": self.today + timedelta(days=2),
            "frequency": "monthly",
            "reminder_days_before": 3,
            "email_reminders": True,
        }
        return create_plan(self.invoice, self.admin, **{**options, **overrides})

    def run_worker(self, now=None):
        task = SimpleNamespace(enqueue=send_reminder.call)
        with mock.patch("crm.billing.reminders.send_reminder", task):
            with self.captureOnCommitCallbacks(execute=True):
                return queue_due(now=now)

    def test_the_client_is_emailed_once_before_each_installment(self):
        plan = self.plan()
        self.assertEqual(self.run_worker(), 1)
        self.assertEqual(self.run_worker(), 0)  # Each installment is emailed once.
        message = self.message()
        due = self.today + timedelta(days=2)
        due_text = f"{due:%B} {due.day}, {due:%Y}"
        self.assertEqual(message["Subject"], f"Payment reminder: $200.00 due {due_text}")
        self.assertIn(
            f"installment 1 of 3 on invoice {self.invoice.number}, $200.00, is due on {due_text}",
            message.get_content(),
        )
        first = plan.installments.get(sequence=1)
        self.assertIsNotNone(first.reminder_emailed_at)
        self.assertEqual(first.reminder_email_error, "")
        logged = Communication.objects.get()
        self.assertTrue(logged.summary.startswith("Payment reminder for invoice"))
        self.assertIsNone(logged.logged_by)
        page = self.client_page()
        self.assertContains(page, "The client is emailed each reminder too.")
        self.assertContains(page, " · emailed</td>", count=1)
        # The next installment's reminder day has not come yet.
        later = timezone.make_aware(datetime.combine(due + timedelta(days=28), datetime.min.time()))
        self.assertEqual(self.run_worker(later), 1)

    def client_page(self):
        self.client.force_login(self.admin)
        return self.client.get(reverse("invoice_detail", args=[self.invoice.pk]))

    def test_paid_late_or_unsent_reminders(self):
        plan = self.plan(count=2, first_due_date=self.today)
        record_payment(self.invoice, self.admin, amount=Decimal("300"), **PAYMENT)
        self.assertEqual(self.run_worker(), 0)  # Paid: nothing is sent.
        self.assertEqual(FakeSMTP.sent, [])
        first = plan.installments.get(sequence=1)
        send_reminder.call(first.pk)  # Paid after it was queued: released, not sent.
        first.refresh_from_db()
        self.assertIsNone(first.reminder_emailed_at)
        plan.installments.filter(sequence=2).update(due_date=self.today - timedelta(days=1))
        self.assertEqual(self.run_worker(), 0)  # Past its due date.
        self.connection.preview_mode = True
        self.connection.save()
        plan.installments.filter(sequence=2).update(due_date=self.today + timedelta(days=1))
        self.assertEqual(self.run_worker(), 1)
        second = plan.installments.get(sequence=2)
        self.assertEqual(
            second.reminder_email_error, "the email delivery service is in preview mode"
        )
        self.assertIn("The client's email reminder was not sent", second.reminder.description)
        self.assertContains(self.client_page(), "email not sent")

    def test_no_sender_or_no_address(self):
        plan = self.plan()
        self.contact.email = "gone"
        self.contact.save()
        self.run_worker()
        first = plan.installments.get(sequence=1)
        self.assertIn("Add a valid email address", first.reminder_email_error)
        self.connection.delete()
        plan.installments.update(reminder_emailed_at=None)
        self.assertEqual(self.run_worker(), 0)
        send_reminder.call(first.pk)  # Queued before the connection went: nothing is sent.
        self.assertEqual(FakeSMTP.sent, [])
        send_reminder.call(0)

    def test_turning_email_reminders_on_and_off(self):
        self.client.force_login(self.admin)
        url = reverse("plan_email_reminders", args=[self.invoice.pk])
        response = self.client.post(url, {"on": "1"}, follow=True)
        self.assertContains(response, "This invoice has no payment plan.")
        self.plan(email_reminders=False)
        page = self.client_page()
        self.assertContains(page, "Email the client reminders too")
        response = self.client.post(url, {"on": "1"}, follow=True)
        self.assertContains(response, "Email reminders turned on.")
        self.assertContains(response, "Stop email reminders")
        response = self.client.post(url, {"on": "0"}, follow=True)
        self.assertContains(response, "Email reminders turned off.")
        self.contact.email = ""
        self.contact.save()
        response = self.client.post(url, {"on": "1"}, follow=True)
        self.assertContains(response, "Add a valid email address")
        self.connection.delete()
        response = self.client.post(url, {"on": "1"}, follow=True)
        self.assertContains(response, "Connect an email delivery service first.")
        self.assertNotContains(response, "Email the client reminders too")

    def test_creating_a_plan_with_email_reminders(self):
        self.client.force_login(self.admin)
        page = self.client_page()
        self.assertContains(page, "Also email the client a reminder then")
        response = self.client.post(
            reverse("plan_create", args=[self.invoice.pk]),
            {
                "count": "2",
                "frequency": "monthly",
                "first_due_date": (self.today + timedelta(days=9)).isoformat(),
                "reminder_days_before": "3",
                "email_reminders": "on",
            },
            follow=True,
        )
        self.assertContains(response, "2 installments and staff and email reminders.")
        self.assertTrue(self.invoice.plan.email_reminders)


PAYMENT = {"method": "check", "deposit_account": "operating"}
