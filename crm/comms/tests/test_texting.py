from datetime import UTC, datetime, timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from crm.comms.emailing import MAX_BODY, email_connections, send_email
from crm.comms.models import Communication
from crm.comms.texting import MAX_LENGTH, texting_connections
from crm.connect.models import Connection, OutboundRequest
from crm.connect.records import CallRecord, TextRecord
from crm.connect.services import acknowledge_data_flows, create_connection
from crm.connect.signals import records_received
from crm.models import MatterParty
from crm.tests.factories import make_contact, make_matter, make_user

AT = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
NUMBER = "+12025550144"


class TextingTestCase(TestCase):
    def setUp(self):
        self.admin = make_user()
        self.staff = make_user("paralegal", superuser=False)
        self.connection = self.connect("Front desk texts")
        self.contact = make_contact(self.staff, phone="(202) 555-0144")
        self.client.force_login(self.staff)

    def connect(self, label, owner=None, preview=False):
        connection = create_connection(
            "example",
            label=label,
            created_by=self.admin,
            capabilities=["texting"],
            credentials={"api_key": "example-fictional-key"},
        )
        acknowledge_data_flows(connection, self.admin)
        Connection.objects.filter(pk=connection.pk).update(preview_mode=preview, owner=owner)
        connection.refresh_from_db()
        return connection

    def send(self, body="Your biometrics appointment is Tuesday.", **overrides):
        data = {
            "contact": self.contact.pk,
            "connection": self.connection.pk,
            "body": body,
            "next": reverse("contact_detail", args=[self.contact.pk]),
            **overrides,
        }
        return self.client.post(reverse("communication_send_text"), data, follow=True)

    def deliver(self, *records):
        records_received.send(
            sender=None,
            connection=self.connection,
            capability="texting",
            records=list(records),
            source="webhook",
        )


class SendTextTests(TextingTestCase):
    def test_a_sent_text_is_logged_on_the_contact(self):
        page = self.client.get(reverse("contact_detail", args=[self.contact.pk]))
        self.assertContains(page, "Send a text")
        self.assertContains(page, f"Text to {NUMBER}")
        response = self.send()
        self.assertContains(response, "Text sent through Front desk texts.")
        text = Communication.objects.get()
        self.assertEqual(
            (text.channel, text.direction, text.counterpart, text.delivery_status),
            ("text", "outbound", NUMBER, "sent"),
        )
        self.assertEqual(text.summary, "Your biometrics appointment is Tuesday.")
        self.assertEqual((text.contact, text.logged_by), (self.contact, self.staff))
        request = OutboundRequest.objects.get()
        self.assertEqual(text.external_id, request.provider_ref)
        self.assertNotIn("biometrics", request.summary)
        self.assertContains(response, "Sent")

    def test_preview_mode_records_without_sending(self):
        Connection.objects.filter(pk=self.connection.pk).update(preview_mode=True)
        response = self.send()
        self.assertContains(response, "in preview mode, so the text was recorded but not sent")
        self.assertEqual(OutboundRequest.objects.get().status, OutboundRequest.Status.PREVIEWED)
        self.assertFalse(Communication.objects.exists())

    def test_texts_are_refused_when_they_cannot_go(self):
        self.contact.texting_opted_out_at = AT
        self.contact.save()
        page = self.client.get(reverse("contact_detail", args=[self.contact.pk]))
        self.assertContains(page, "opted out of texts, so none can be sent")
        self.assertContains(self.send(), "The recipient opted out of texts.")
        self.contact.texting_opted_out_at = None
        self.contact.phone = "not a number"
        self.contact.save()
        page = self.client.get(reverse("contact_detail", args=[self.contact.pk]))
        self.assertContains(page, "Add a valid mobile number")
        self.assertContains(self.send(), "Add a valid phone number for")
        self.contact.phone = "(202) 555-0144"
        self.contact.save()
        self.assertContains(self.send(body="  "), "Write the text first.")
        self.assertContains(self.send(body="x" * (MAX_LENGTH + 1)), "at most 1600 characters")
        self.assertFalse(Communication.objects.exists())

    def test_only_connections_the_sender_may_use(self):
        other = make_user("other", superuser=False)
        theirs = self.connect("Other's phone", owner=other)
        mine = self.connect("My phone", owner=self.staff, preview=True)
        self.assertEqual(
            {c.label for c in texting_connections(self.staff)},
            {"Front desk texts", "My phone"},
        )
        page = self.client.get(reverse("contact_detail", args=[self.contact.pk]))
        self.assertContains(page, "My phone (yours) · preview mode")
        self.assertNotContains(page, "Other&#x27;s phone")
        self.assertEqual(self.send(connection=theirs.pk).status_code, 403)
        self.assertEqual(self.send(connection=mine.pk).status_code, 200)

    def test_matter_pages_text_the_primary_contact_and_link_the_matter(self):
        matter = make_matter(self.contact, self.staff)
        page = self.client.get(reverse("matter_detail", args=[matter.pk]))
        self.assertContains(page, f"Send a text to {self.contact}")
        self.send(matter=matter.pk, next=reverse("matter_detail", args=[matter.pk]))
        self.assertEqual(Communication.objects.get().matter, matter)
        stranger = make_contact(first_name="Noor", email="noor@example.test", phone="+12025550145")
        self.assertEqual(self.send(contact=stranger.pk, matter=matter.pk).status_code, 403)
        MatterParty.objects.create(matter=matter, contact=stranger, role="beneficiary")
        self.assertEqual(self.send(contact=stranger.pk, matter=matter.pk).status_code, 200)

    def test_without_a_texting_connection_the_page_offers_one(self):
        Connection.objects.update(status=Connection.Status.DISCONNECTED)
        page = self.client.get(reverse("contact_detail", args=[self.contact.pk]))
        self.assertNotContains(page, "Send a text")
        self.assertContains(page, "texting service")


class ProviderUpdateTests(TextingTestCase):
    def test_delivery_updates_move_forward_and_keep_the_text(self):
        self.send()
        text = Communication.objects.get()
        sent_at = text.occurred_at
        update = {"direction": "outbound", "remote": NUMBER, "occurred_at": AT}
        self.deliver(TextRecord(external_id=text.external_id, status="delivered", **update))
        self.deliver(TextRecord(external_id=text.external_id, status="sent", **update))
        text.refresh_from_db()
        self.assertEqual(text.delivery_status, "delivered")
        self.assertEqual(text.summary, "Your biometrics appointment is Tuesday.")
        self.assertEqual(text.occurred_at, sent_at)
        self.assertEqual(text.logged_by, self.staff)
        # An update for a text this CRM never sent is ignored.
        self.deliver(TextRecord(external_id="from-another-app", status="delivered", **update))
        self.assertEqual(Communication.objects.count(), 1)

    def test_the_providers_opt_out_reading_counts(self):
        self.deliver(
            TextRecord(
                external_id="in-1",
                occurred_at=AT,
                direction="inbound",
                remote=NUMBER,
                body="ARRET",
                opt_out="stop",
            )
        )
        self.contact.refresh_from_db()
        self.assertIsNotNone(self.contact.texting_opted_out_at)
        self.deliver(
            TextRecord(
                external_id="in-2",
                occurred_at=AT + timedelta(minutes=5),
                direction="inbound",
                remote=NUMBER,
                body="Oui",
                opt_out="start",
            )
        )
        self.contact.refresh_from_db()
        self.assertIsNone(self.contact.texting_opted_out_at)

    def test_parts_of_a_call_sent_separately_are_combined(self):
        call = {"external_id": "call-1", "direction": "inbound", "remote": NUMBER}
        self.deliver(CallRecord(occurred_at=AT, duration_seconds=95, **call))
        self.deliver(
            CallRecord(
                occurred_at=AT + timedelta(minutes=2),
                recording_url="https://recordings.example.test/1",
                **call,
            )
        )
        self.deliver(CallRecord(occurred_at=AT, summary="Asked about the interview.", **call))
        stored = Communication.objects.get()
        self.assertEqual(stored.occurred_at, AT)
        self.assertEqual(stored.duration, timedelta(seconds=95))
        self.assertEqual(
            stored.summary,
            "Recording: https://recordings.example.test/1\n\nAsked about the interview.",
        )


class SendEmailValidationTests(TextingTestCase):
    def test_emails_are_checked_before_anything_is_sent(self):
        self.assertEqual(email_connections(self.staff), [])
        cases = [
            ({"subject": "  ", "body": "Hello"}, "Write a subject and a message."),
            ({"subject": "Forms", "body": "x" * (MAX_BODY + 1)}, "Keep the subject under"),
        ]
        for fields, message in cases:
            with self.assertRaisesMessage(ValidationError, message):
                send_email(self.connection, contact=self.contact, actor=self.staff, **fields)
        self.contact.email = "not an address"
        with self.assertRaisesMessage(ValidationError, "Add a valid email address"):
            send_email(
                self.connection, contact=self.contact, subject="Hi", body="Hello", actor=self.staff
            )
        self.assertFalse(OutboundRequest.objects.exists())
