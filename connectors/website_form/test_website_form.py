import json
import time
import uuid

from django.test import TestCase
from django.urls import reverse

from connectors.webhooks import sign
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, WebhookEvent
from crm.connect.tasks import process_webhook_event
from crm.connect.testing import ConnectorContract
from crm.customfields.models import CustomField
from crm.models import Activity, Contact
from crm.tests.factories import make_contact, make_user

from . import WebsiteFormConnector

SECRET = "whsec_" + "ZmljdGlvbmFsLXNpZ25pbmctc2VjcmV0LWZvci10ZXN0cw=="


def submission(**extra):
    return {
        "name": "Lena Fictional",
        "email": "lena@example.test",
        "phone": "202-555-0144",
        "message": "My spouse is a U.S. citizen. Can you help with a green card?",
        "form": "Contact page",
        "source": "google-ads",
        "utm_campaign": "fall",
        "answers": {"Preferred language": "Spanish", "Best time to call": "Mornings"},
        "Country of birth": "Fictionland",
        **extra,
    }


def signed(payload, secret=SECRET, *, message_id=None, at=None):
    body = json.dumps(payload).encode()
    message_id = message_id or f"msg_{uuid.uuid4().hex}"
    stamp = str(int(at or time.time()))
    return body, {
        "webhook-id": message_id,
        "webhook-timestamp": stamp,
        "webhook-signature": sign(secret, message_id, stamp, body),
    }


class WebsiteFormContractTests(ConnectorContract, TestCase):
    connector_class = WebsiteFormConnector
    valid_credentials = {"signing_secret": SECRET}
    invalid_credentials = {"signing_secret": "made-up"}

    def make_webhook(self, secret):
        return signed(submission(), secret)


class WebsiteFormTests(TestCase):
    def setUp(self):
        self.admin = make_user()
        self.source = CustomField.objects.create(
            applies_to="contact",
            label="Lead source",
            key="lead_source",
            field_type="choice",
            choices="Google Ads\nReferral",
        )
        self.client.force_login(self.admin)
        page = self.client.get(reverse("connector_detail", args=["website_form"]))
        self.assertContains(page, "Copy this into the website")
        generated = page.context["form"]["credential_signing_secret"].value()
        self.assertTrue(generated.startswith("whsec_"))
        self.client.post(
            reverse("connector_detail", args=["website_form"]),
            {
                "label": "Website form",
                "capabilities": ["lead_capture"],
                "credential_signing_secret": SECRET,
                "setting_lead_source_from": "source",
                "acknowledge": "on",
            },
        )
        self.connection = Connection.objects.get()
        self.connection.config = {"lead_source_field": "lead_source", "lead_source_from": "source"}
        self.connection.save()
        response = self.client.post(reverse("connection_webhooks", args=[self.connection.pk]))
        self.url = response.context["webhook_url"]
        self.assertContains(response, "Standard Webhooks scheme")

    def deliver(self, body, headers):
        with self.captureOnCommitCallbacks(execute=True):
            answer = self.client.post(
                self.url, body, content_type="application/json", headers=headers
            )
        for queued in WebhookEvent.objects.filter(status=WebhookEvent.Status.QUEUED):
            process_webhook_event.call(queued.pk)
        return answer

    def test_a_submission_becomes_a_lead(self):
        self.assertEqual(self.connection.status, Connection.Status.ACTIVE)
        answer = self.deliver(*signed(submission()))
        self.assertEqual(answer.status_code, 200)
        contact = Contact.objects.get()
        self.assertEqual(
            (contact.full_name, contact.status, contact.stage, contact.phone_e164),
            ("Lena Fictional", "lead", "inquiry", "+12025550144"),
        )
        self.assertEqual(contact.custom_fields, {"lead_source": "Google Ads"})
        note = Activity.objects.get(contact=contact)
        self.assertEqual(
            (note.kind, note.subject), ("intake", "New lead from Website form (Contact page)")
        )
        self.assertIn("Can you help with a green card?", note.body)
        self.assertIn("Preferred language: Spanish", note.body)
        self.assertIn("Country of birth: Fictionland", note.body)
        self.assertIn("Tracking: source: google-ads; utm_campaign: fall", note.body)
        # The stored event keeps nothing once it is processed.
        self.assertIsNone(WebhookEvent.objects.get().payload)

    def test_an_existing_contact_gets_a_note_and_nothing_else(self):
        existing = make_contact(self.admin, email="lena@example.test", stage="active")
        self.deliver(*signed(submission(first_name="Lena", last_name="Other", name="")))
        existing.refresh_from_db()
        self.assertEqual((Contact.objects.count(), existing.stage), (1, "active"))
        self.assertEqual(existing.custom_fields, {})
        self.assertEqual(
            Activity.objects.get(contact=existing).subject,
            "Form submission through Website form (Contact page)",
        )

    def test_unsigned_stale_and_replayed_submissions_are_refused(self):
        body, headers = signed(submission(), message_id="msg_1")
        self.assertEqual(self.deliver(body, headers).status_code, 200)
        self.assertEqual(self.deliver(body, headers).status_code, 200)  # Replayed: filed once.
        self.assertEqual(Activity.objects.count(), 1)
        self.assertEqual(self.deliver(*signed(submission(), "whsec_d3Jvbmc=")).status_code, 401)
        stale = signed(submission(), at=time.time() - 600)
        self.assertEqual(self.deliver(*stale).status_code, 401)
        self.assertEqual(self.deliver(body, {}).status_code, 401)
        for broken in (
            {**headers, "webhook-timestamp": "soon"},
            {**headers, "webhook-signature": ""},
        ):
            self.assertEqual(self.deliver(body, broken).status_code, 401)

    def test_unusable_submissions(self):
        for payload in ([1], {"name": "No contact details"}):
            body = json.dumps(payload).encode()
            message_id, stamp = "msg_x", str(int(time.time()))
            headers = {
                "webhook-id": message_id,
                "webhook-timestamp": stamp,
                "webhook-signature": sign(SECRET, message_id, stamp, body),
            }
            self.assertEqual(self.deliver(body, headers).status_code, 400)
        not_json = b"name=Lena"
        stamp = str(int(time.time()))
        headers = {
            "webhook-id": "msg_y",
            "webhook-timestamp": stamp,
            "webhook-signature": sign(SECRET, "msg_y", stamp, not_json),
        }
        self.assertEqual(self.deliver(not_json, headers).status_code, 400)
        # A name is needed too; such a submission is recorded as failed, not filed.
        self.deliver(*signed({"email": "x@example.test"}))
        self.assertEqual(
            WebhookEvent.objects.get(status=WebhookEvent.Status.FAILED).error,
            "The submission had no name, or no email or phone.",
        )
        self.assertFalse(Contact.objects.exists())

    def test_secrets_and_values(self):
        connector = WebsiteFormConnector(self.connection)
        connector.test_connection()
        self.connection.credentials = {"signing_secret": "old"}
        with self.assertRaisesMessage(ConnectorError, "Connect again"):
            WebsiteFormConnector(self.connection).test_connection()
        envelope = connector.parse_webhook(
            json.dumps(
                submission(
                    answers={"Consent": True, "Topics": ["Visas", "Work"], "Nested": {"a": 1}},
                    extra_field=None,
                )
            ).encode(),
            {"webhook-id": "msg_v", "webhook-timestamp": "1793390000"},
        )
        answers = envelope.payload["answers"]
        self.assertEqual((answers["Consent"], answers["Topics"]), ("Yes", "Visas, Work"))
        self.assertNotIn("Nested", answers)
        self.assertNotIn("extra_field", answers)
