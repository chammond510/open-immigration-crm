import imaplib
import re
import smtplib
from email.message import EmailMessage
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from crm.comms.models import Communication
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, SyncRun
from crm.connect.services import acknowledge_data_flows
from crm.connect.tasks import run_sync
from crm.connect.testing import ConnectorContract
from crm.tests.factories import make_contact, make_user

from . import BACKFILL_DAYS, RESYNC_DAYS, SNIPPET_LIMIT, ImapSmtpConnector, snippet_of

ACCOUNT = "associate@firm.example.test"
CLIENT = "lena@example.test"
PASSWORD = "fictional-app-password"
CONFIG = {
    "imap_host": "imap.example.test",
    "imap_port": "993",
    "smtp_host": "smtp.example.test",
    "smtp_port": "465",
    "smtp_security": "ssl",
    "unmatched": "keep",
    "copy_to_sent": "yes",
}


def mail(
    sender=CLIENT,
    to=ACCOUNT,
    subject="Fictional subject",
    body="Thanks for the update & the forms.",
    html=False,
    message_id="<in-1@example.test>",
    **extra,
):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = "Fri, 30 Oct 2026 10:00:00 +0000"
    message["Message-ID"] = message_id
    for name, value in extra.items():
        message[name.replace("_", "-")] = value
    if html:
        message.set_content(f"<p>{body}</p>", subtype="html")
    else:
        message.set_content(body)
    return message.as_bytes()


class FakeIMAP:
    """imaplib.IMAP4_SSL, answering as a mail server would, from ``folders``."""

    folders = {}
    validity = {}
    appended = []
    commands = []
    password = PASSWORD
    sent_name = "Sent Items"
    sizes = {}

    def __init__(self, host, port, ssl_context=None, timeout=None):
        self.host, self.port, self.selected = host, port, None
        assert ssl_context is not None and timeout

    def login(self, user, password):
        if password != self.password:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        return "OK", [b"Logged in"]

    def logout(self):
        return "BYE", [b""]

    def list(self, directory, pattern):
        lines = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Drafts"']
        if self.sent_name:
            lines.append(f'(\\HasNoChildren \\Sent) "/" "{self.sent_name}"'.encode())
        return "OK", lines

    def select(self, mailbox, readonly=False):
        self.selected = mailbox.strip('"')
        self.commands.append(("select", self.selected, readonly))
        return "OK", [str(len(self.folders.get(self.selected, {}))).encode()]

    def response(self, name):
        return name, [str(self.validity.get(self.selected, 7)).encode()]

    def uid(self, command, *args):
        self.commands.append((command, self.selected) + args)
        messages = self.folders.get(self.selected, {})
        if command == "SEARCH":
            criteria = " ".join(a for a in args if a)
            found = sorted(messages)
            ranged = re.match(r"UID (\d+):\*", criteria)
            if ranged:
                found = [u for u in found if u >= int(ranged.group(1))] or found[-1:]
            return "OK", [" ".join(str(u) for u in found).encode()]
        uid, items = int(args[0]), args[1]
        if uid not in messages:
            return "OK", [None]
        raw = messages[uid]
        size = self.sizes.get(uid, len(raw))
        if "HEADER.FIELDS" in items:
            header = raw.split(b"\n\n", 1)[0] + b"\n\n"
            meta = (
                f'{uid} (UID {uid} RFC822.SIZE {size} INTERNALDATE "30-Oct-2026 10:05:00 +0000" '
                f"BODY[HEADER.FIELDS (FROM)] {{{len(header)}}}"
            ).encode()
            return "OK", [(meta, header), b")"]
        return "OK", [(f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]

    def append(self, mailbox, flags, date, message):
        self.appended.append((mailbox.strip('"'), flags, message))
        return "OK", [b"APPEND completed"]


class FakeSMTP:
    sent = []
    password = PASSWORD
    started_tls = False

    def __init__(self, host, port, context=None, timeout=None):
        self.host, self.port = host, port

    def starttls(self, context=None):
        FakeSMTP.started_tls = True

    def login(self, user, password):
        if password != self.password:
            raise smtplib.SMTPAuthenticationError(535, b"Authentication failed")

    def send_message(self, message):
        self.sent.append(message)

    def quit(self):
        pass

    def close(self):
        pass


class FakeMailMixin:
    def setUp(self):
        super().setUp()
        FakeIMAP.folders = {"INBOX": {}, "Sent Items": {}}
        FakeIMAP.validity, FakeIMAP.appended, FakeIMAP.commands, FakeIMAP.sizes = {}, [], [], {}
        FakeIMAP.sent_name = "Sent Items"
        FakeSMTP.sent, FakeSMTP.started_tls = [], False
        for target, fake in (
            ("imaplib.IMAP4_SSL", FakeIMAP),
            ("smtplib.SMTP_SSL", FakeSMTP),
            ("smtplib.SMTP", FakeSMTP),
        ):
            patcher = mock.patch(target, fake)
            patcher.start()
            self.addCleanup(patcher.stop)


class ImapSmtpContractTests(FakeMailMixin, ConnectorContract, TestCase):
    connector_class = ImapSmtpConnector
    valid_credentials = {"email": ACCOUNT, "password": PASSWORD}
    invalid_credentials = {"email": ACCOUNT, "password": "wrong"}
    connection_config = CONFIG
    sync_examples = {"mailbox": {}}
    outbound_examples = {
        "send_email": {
            "to": CLIENT,
            "subject": "Hello",
            "body": "Fictional.",
            "staff_email": ACCOUNT,
        }
    }

    def setUp(self):
        super().setUp()
        FakeIMAP.folders = {"INBOX": {1: mail()}, "Sent Items": {}}


class MailTestCase(FakeMailMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user()
        self.owner = make_user("associate", superuser=False)
        self.contact = make_contact(self.owner, email=CLIENT)
        self.connection = Connection.objects.create(
            connector_key="imap_smtp",
            label="My mailbox",
            owner=self.owner,
            enabled_capabilities=["mailbox"],
            credentials={"email": ACCOUNT, "password": PASSWORD},
            config=dict(CONFIG),
            preview_mode=False,
        )
        acknowledge_data_flows(self.connection, self.admin)

    def connector(self):
        self.connection.refresh_from_db()
        return ImapSmtpConnector(self.connection)

    def sync(self):
        run = SyncRun.objects.create(connection=self.connection, capability="mailbox")
        run_sync.call(run.pk)
        run.refresh_from_db()
        return run


class MailSyncTests(MailTestCase):
    def test_first_sync_files_client_mail_from_the_inbox_and_sent_folder(self):
        FakeIMAP.folders = {
            "INBOX": {
                1: mail(),
                2: mail(
                    message_id="<list@example.test>", List_Unsubscribe="<mailto:x@example.test>"
                ),
                3: mail(message_id="<auto@example.test>", Auto_Submitted="auto-replied"),
                4: mail(sender=ACCOUNT, message_id="<self@example.test>"),
                5: mail(
                    sender="new.lead@example.test",
                    message_id="<html@example.test>",
                    html=True,
                    body="Can you help with a <b>visa</b>?",
                ),
                6: mail(message_id="<big@example.test>", subject="=?utf-8?q?Gro=C3=9Fe_Datei?="),
            },
            "Sent Items": {
                9: mail(
                    sender=ACCOUNT, to=f"Lena <{CLIENT}>", message_id="<out-1@firm.example.test>"
                ),
            },
        }
        FakeIMAP.sizes = {6: SNIPPET_LIMIT + 1}
        run = self.sync()
        self.assertEqual(run.status, SyncRun.Status.SUCCEEDED, run.error)
        stored = {c.external_id: c for c in Communication.objects.all()}
        self.assertEqual(
            set(stored),
            {
                "in-1@example.test",
                "html@example.test",
                "big@example.test",
                "out-1@firm.example.test",
            },
        )
        inbound, outbound = stored["in-1@example.test"], stored["out-1@firm.example.test"]
        self.assertEqual((inbound.contact, inbound.direction), (self.contact, "inbound"))
        self.assertEqual((outbound.contact, outbound.direction), (self.contact, "outbound"))
        self.assertEqual(inbound.summary, "Thanks for the update & the forms.")
        self.assertEqual(inbound.logged_by, self.owner)
        self.assertEqual(stored["html@example.test"].summary, "Can you help with a visa?")
        self.assertIsNone(stored["html@example.test"].contact)
        self.assertEqual(stored["big@example.test"].summary, "")
        self.assertEqual(stored["big@example.test"].subject, "Große Datei")
        searches = [c for c in FakeIMAP.commands if c[0] == "SEARCH"]
        self.assertEqual([c[1] for c in searches], ["INBOX", "Sent Items"])
        self.assertEqual(searches[0][3], "SINCE")
        self.assertTrue(all(c[2] for c in FakeIMAP.commands if c[0] == "select"))
        fetches = [c for c in FakeIMAP.commands if c[0] == "FETCH"]
        self.assertTrue(all("PEEK" in c[-1] for c in fetches))
        self.assertEqual(
            run.cursor_after,
            {"INBOX": {"validity": "7", "last": 6}, "Sent Items": {"validity": "7", "last": 9}},
        )
        self.assertEqual(BACKFILL_DAYS, 30)

    def test_later_syncs_continue_and_recover_when_a_folder_is_renumbered(self):
        FakeIMAP.folders = {"INBOX": {1: mail(), 2: mail(message_id="<in-2@example.test>")}}
        FakeIMAP.sent_name = ""
        cursor = {"INBOX": {"validity": "7", "last": 1}}
        batch = self.connector().sync("mailbox", cursor)
        self.assertEqual([r.external_id for r in batch.records], ["in-2@example.test"])
        self.assertEqual(batch.cursor["INBOX"], {"validity": "7", "last": 2})
        batch = self.connector().sync("mailbox", batch.cursor)
        self.assertEqual(batch.records, [])  # "UID 3:*" still names message 2.
        FakeIMAP.validity = {"INBOX": 8}
        FakeIMAP.commands = []
        batch = self.connector().sync("mailbox", batch.cursor)
        self.assertEqual(len(batch.records), 2)
        since = next(c for c in FakeIMAP.commands if c[0] == "SEARCH")
        self.assertEqual(since[3], "SINCE")
        self.assertEqual(RESYNC_DAYS, 7)

    def test_a_named_sent_folder_and_unmatched_mail_left_out(self):
        self.connection.config = {**CONFIG, "sent_folder": "Outbox Copies", "unmatched": "skip"}
        self.connection.save()
        FakeIMAP.folders = {
            "INBOX": {1: mail(sender="stranger@example.test")},
            "Outbox Copies": {
                3: mail(sender=ACCOUNT, to=CLIENT, message_id="<o@firm.example.test>")
            },
        }
        self.sync()
        self.assertEqual(
            list(Communication.objects.values_list("external_id", flat=True)),
            ["o@firm.example.test"],
        )

    def test_sign_in_and_server_problems(self):
        self.connection.credentials = {"email": ACCOUNT, "password": "wrong"}
        with self.assertRaisesMessage(
            ConnectorError, "did not accept the address and app password"
        ):
            ImapSmtpConnector(self.connection).test_connection()

        def unreachable(*args, **kwargs):
            raise OSError("refused")

        with mock.patch("imaplib.IMAP4_SSL", unreachable):
            with self.assertRaisesMessage(ConnectorError, "could not be reached securely"):
                self.connector().sync("mailbox", {})
        with mock.patch.object(FakeIMAP, "select", lambda *a, **k: ("NO", [b"No such folder"])):
            with self.assertRaisesMessage(ConnectorError, "refused a request"):
                self.connector().sync("mailbox", {})

    def test_snippets(self):
        self.assertEqual(snippet_of(b"Subject: x\n\n"), "")
        self.assertEqual(snippet_of(mail(body="Line one\n\nLine   two")), "Line one Line two")


class MailSendTests(MailTestCase):
    def test_sending_email_from_the_contact_page(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("contact_detail", args=[self.contact.pk]))
        self.assertContains(page, "Send an email")
        response = self.client.post(
            reverse("communication_send_email"),
            {
                "contact": self.contact.pk,
                "connection": self.connection.pk,
                "subject": "Your interview notice",
                "body": "The interview notice arrived. We will call you to prepare.",
            },
            follow=True,
        )
        self.assertContains(response, "Email sent through My mailbox.")
        sent = FakeSMTP.sent[0]
        self.assertEqual((sent["From"], sent["To"]), (ACCOUNT, CLIENT))
        self.assertTrue(sent["Message-ID"].endswith("@firm.example.test>"))
        self.assertEqual(FakeIMAP.appended[0][0], "Sent Items")
        logged = Communication.objects.get()
        key = sent["Message-ID"].strip("<>").lower()
        self.assertEqual((logged.external_id, logged.direction), (key, "outbound"))
        # The sent folder's copy, once synced, lands on the same record.
        FakeIMAP.folders = {
            "INBOX": {},
            "Sent Items": {4: mail(sender=ACCOUNT, to=CLIENT, message_id=sent["Message-ID"])},
        }
        run = self.sync()
        self.assertEqual(run.records_received, 1)
        self.assertEqual(Communication.objects.count(), 1)

    def test_starttls_no_copy_and_send_failures(self):
        self.connection.config = {
            **CONFIG,
            "smtp_security": "starttls",
            "smtp_port": "587",
            "copy_to_sent": "no",
        }
        self.connection.save()
        payload = {"to": CLIENT, "subject": "Hi", "body": "Hello", "staff_email": ACCOUNT}
        self.connector().perform("send_email", payload)
        self.assertTrue(FakeSMTP.started_tls)
        self.assertEqual(FakeIMAP.appended, [])
        with mock.patch.object(
            FakeSMTP, "send_message", side_effect=smtplib.SMTPRecipientsRefused({})
        ):
            with self.assertRaisesMessage(ConnectorError, "did not take the email"):
                self.connector().perform("send_email", payload)
        with mock.patch.object(FakeSMTP, "password", "other"):
            with self.assertRaisesMessage(ConnectorError, "outgoing mail server did not accept"):
                self.connector().perform("send_email", payload)
        with mock.patch("smtplib.SMTP", side_effect=OSError("refused")):
            with self.assertRaisesMessage(ConnectorError, "could not be reached securely"):
                self.connector().perform("send_email", payload)
        with self.assertRaisesMessage(ConnectorError, "address or subject was not valid"):
            self.connector().perform("send_email", {**payload, "subject": "a\nb"})
        with self.assertRaisesMessage(ConnectorError, "cannot send_text"):
            self.connector().perform("send_text", payload)

    def test_a_lost_copy_does_not_undo_the_send(self):
        payload = {"to": CLIENT, "subject": "Hi", "body": "Hello", "staff_email": ACCOUNT}
        with mock.patch.object(FakeIMAP, "append", lambda *a: ("NO", [b"Over quota"])):
            self.assertTrue(self.connector().perform("send_email", payload))
        with mock.patch.object(FakeIMAP, "password", "other"):
            self.assertTrue(self.connector().perform("send_email", payload))
        self.assertEqual(len(FakeSMTP.sent), 2)

    def test_settings_are_checked(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("connector_detail", args=["imap_smtp"]))
        self.assertContains(page, "App password")
        response = self.client.post(
            reverse("connector_detail", args=["imap_smtp"]),
            {
                "label": "Mine",
                "capabilities": ["mailbox"],
                "credential_email": ACCOUNT,
                "credential_password": PASSWORD,
                "setting_imap_host": "not a host",
                "setting_imap_port": "99999",
                "setting_smtp_host": "smtp.example.test",
                "setting_smtp_port": "465",
                "setting_smtp_security": "ssl",
                "setting_unmatched": "keep",
                "setting_copy_to_sent": "yes",
            },
        )
        self.assertContains(response, "Enter the server&#x27;s name")
        self.assertContains(response, "Enter a port number")
