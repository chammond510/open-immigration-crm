import json
import time
from unittest import mock

import httpx
from django.test import TestCase
from django.urls import reverse

from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, WebhookEvent
from crm.connect.services import acknowledge_data_flows, disconnect
from crm.connect.tasks import process_webhook_event
from crm.connect.testing import ConnectorContract
from crm.customfields.models import CustomField
from crm.models import Activity, Contact, WorkItem
from crm.tests.factories import make_contact, make_user

from . import API_URL, SIGNATURE_HEADER, CalendlyConnector, sign

TOKEN = "fictional-calendly-token"
ORG = f"{API_URL}/organizations/ORG-FICTIONAL"
CONSULT = f"{API_URL}/event_types/CONSULT"
OTHER = f"{API_URL}/event_types/OTHER"
STAFF_EMAIL = "paralegal@example.test"


def booking(event="invitee.created", invitee="INV-1", *, event_type=CONSULT, **extra):
    return {
        "event": event,
        "created_at": "2026-10-30T15:00:00.000000Z",
        "created_by": f"{API_URL}/users/U1",
        "payload": {
            "uri": f"{API_URL}/scheduled_events/EV-{invitee}/invitees/{invitee}",
            "email": "lena@example.test",
            "name": "Lena Fictional",
            "text_reminder_number": "+1 202-555-0144",
            "status": "canceled" if event == "invitee.canceled" else "active",
            "rescheduled": False,
            "old_invitee": None,
            "questions_and_answers": [
                {"question": "What is your case about?", "answer": "Family petition", "position": 0}
            ],
            "tracking": {"utm_source": "google-ads", "utm_campaign": "fall", "utm_medium": None},
            "scheduled_event": {
                "uri": f"{API_URL}/scheduled_events/EV-{invitee}",
                "name": "Immigration consultation",
                "start_time": "2026-11-03T15:00:00.000000Z",
                "end_time": "2026-11-03T15:30:00.000000Z",
                "event_type": event_type,
                "location": {"type": "zoom", "join_url": "https://zoom.example.test/j/1"},
                "event_memberships": [{"user": f"{API_URL}/users/U1", "user_email": STAFF_EMAIL}],
            },
            **extra,
        },
    }


class FakeCalendly:
    def __init__(self):
        self.requests = []
        self.made = 0

    def __call__(self, request):
        self.requests.append(request)
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"title": "Unauthenticated"})
        path = request.url.path
        if path == "/users/me":
            return httpx.Response(
                200, json={"resource": {"uri": f"{API_URL}/users/U1", "current_organization": ORG}}
            )
        if path == "/event_types":
            if request.url.params.get("page_token") == "p2":
                return httpx.Response(
                    200,
                    json={
                        "collection": [{"uri": OTHER, "name": "Quick call"}],
                        "pagination": {"next_page_token": None},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "collection": [{"uri": CONSULT, "name": "Immigration consultation"}],
                    "pagination": {"next_page_token": "p2"},
                },
            )
        if path == "/webhook_subscriptions" and request.method == "POST":
            self.made += 1
            uri = f"{API_URL}/webhook_subscriptions/W{self.made}"
            return httpx.Response(201, json={"resource": {"uri": uri, "state": "active"}})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    def calls(self, method, path=""):
        return [r for r in self.requests if r.method == method and path in r.url.path]


class FakeCalendlyMixin:
    def setUp(self):
        super().setUp()
        self.calendly = FakeCalendly()
        transport = http.use_transport(httpx.MockTransport(lambda r: self.calendly(r)))
        transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)
        patcher = mock.patch.object(http, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)


def signed(event, secret, at=None):
    body = json.dumps(event).encode()
    return body, {SIGNATURE_HEADER: sign(body, secret, int(at or time.time()))}


class CalendlyContractTests(FakeCalendlyMixin, ConnectorContract, TestCase):
    connector_class = CalendlyConnector
    valid_credentials = {"token": TOKEN}
    invalid_credentials = {"token": "wrong"}
    connection_config = {"consult_event_types": [CONSULT]}

    def make_webhook(self, secret):
        return signed(booking(), secret)


class SignatureTests(FakeCalendlyMixin, TestCase):
    def test_signatures_are_checked_with_a_time_window(self):
        connection = Connection.objects.create(
            connector_key="calendly", label="Calendly", enabled_capabilities=["scheduling"]
        )
        connector = CalendlyConnector(connection)
        body, headers = signed(booking(), "secret")
        self.assertTrue(connector.verify_webhook(body, headers, "secret"))
        self.assertFalse(connector.verify_webhook(body + b" ", headers, "secret"))
        self.assertFalse(connector.verify_webhook(body, headers, "other"))
        stale = signed(booking(), "secret", at=time.time() - 200)
        self.assertFalse(connector.verify_webhook(*stale, "secret"))
        for header in ("", "t=abc,v1=x", "v1=x", "t=1"):
            self.assertFalse(connector.verify_webhook(body, {SIGNATURE_HEADER: header}, "secret"))


class CalendlyFlowTests(FakeCalendlyMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user()
        self.staff = make_user("paralegal", superuser=False)
        self.source = CustomField.objects.create(
            applies_to="contact",
            label="Lead source",
            key="lead_source",
            field_type="choice",
            choices="Google Ads\nReferral",
        )
        self.connection = Connection.objects.create(
            connector_key="calendly",
            label="Calendly",
            enabled_capabilities=["scheduling"],
            credentials={"token": TOKEN},
            config={"lead_source_from": "utm_source"},
        )
        acknowledge_data_flows(self.connection, self.admin)
        self.client.force_login(self.admin)

    def turn_on(self):
        response = self.client.post(
            reverse("connection_webhooks", args=[self.connection.pk]), {"register": "on"}
        )
        self.url, self.secret = response.context["webhook_url"], response.context["secret"]
        self.connection.refresh_from_db()
        return response

    def deliver(self, event):
        body, headers = signed(event, self.secret)
        with self.captureOnCommitCallbacks(execute=True):
            answer = self.client.post(
                self.url, body, content_type="application/json", headers=headers
            )
        for queued in WebhookEvent.objects.filter(status=WebhookEvent.Status.QUEUED):
            process_webhook_event.call(queued.pk)
        return answer

    def choose(self, **settings):
        data = {
            "setting_consult_event_types": [CONSULT],
            "setting_lead_source_field": "lead_source",
            "setting_lead_source_from": "utm_source",
            **settings,
        }
        return self.client.post(reverse("connection_config", args=[self.connection.pk]), data)

    def test_settings_come_live_from_calendly_and_the_crm(self):
        page = self.client.get(reverse("connection_detail", args=[self.connection.pk]))
        self.assertContains(page, "Event types that are consultations")
        self.assertContains(page, "Immigration consultation")
        self.assertContains(page, "Quick call")
        self.assertContains(page, '<option value="lead_source">Lead source</option>', html=True)
        self.choose()
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.config["consult_event_types"], [CONSULT])
        self.assertEqual(self.connection.config["lead_source_field"], "lead_source")
        refused = self.choose(setting_consult_event_types=[f"{API_URL}/event_types/MADE-UP"])
        self.assertEqual(refused.status_code, 302)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.config["consult_event_types"], [CONSULT])
        self.connection.credentials = {"token": "revoked"}
        self.connection.save()
        page = self.client.get(reverse("connection_detail", args=[self.connection.pk]))
        self.assertContains(page, "could not be loaded: Calendly did not accept the token.")

    def test_turning_on_webhooks_creates_one_organization_webhook(self):
        response = self.turn_on()
        self.assertContains(response, "Calendly will send bookings and cancellations here.")
        made = json.loads(self.calendly.calls("POST", "/webhook_subscriptions")[0].content)
        self.assertEqual(
            made,
            {
                "url": self.url,
                "events": ["invitee.created", "invitee.canceled"],
                "organization": ORG,
                "scope": "organization",
                "signing_key": self.secret,
            },
        )
        self.assertEqual(
            self.connection.config["webhook_uri"], f"{API_URL}/webhook_subscriptions/W1"
        )
        self.turn_on()
        self.assertEqual(
            str(self.calendly.calls("DELETE")[0].url), f"{API_URL}/webhook_subscriptions/W1"
        )
        disconnect(self.connection, self.admin)
        self.assertEqual(
            str(self.calendly.calls("DELETE")[-1].url), f"{API_URL}/webhook_subscriptions/W2"
        )

    def test_a_consult_booking_makes_the_contact_and_appointment(self):
        self.choose()
        self.turn_on()
        self.assertEqual(self.deliver(booking(event_type=OTHER, invitee="INV-0")).status_code, 200)
        self.assertFalse(Contact.objects.exists())
        self.deliver(booking())
        contact = Contact.objects.get()
        self.assertEqual((contact.full_name, contact.stage), ("Lena Fictional", "consultation"))
        self.assertEqual(contact.phone_e164, "+12025550144")
        self.assertEqual(contact.custom_fields, {"lead_source": "Google Ads"})
        note = Activity.objects.get(contact=contact)
        self.assertEqual(
            note.body, "Booking link tracking: utm_campaign: fall; utm_source: google-ads"
        )
        item = WorkItem.objects.get()
        self.assertEqual(item.title, "Immigration consultation")
        self.assertEqual(item.scheduled_for.isoformat(), "2026-11-03T15:00:00+00:00")
        self.assertEqual(item.assigned_to, self.staff)
        self.assertIn("What is your case about?: Family petition", item.description)
        self.assertIn("https://zoom.example.test/j/1", item.description)

        # A reschedule moves the appointment; the cancellation that comes with it is ignored.
        self.deliver(booking("invitee.canceled", rescheduled=True))
        moved = booking(
            invitee="INV-2", old_invitee=f"{API_URL}/scheduled_events/EV-INV-1/invitees/INV-1"
        )
        moved["payload"]["scheduled_event"]["start_time"] = "2026-11-05T16:00:00.000000Z"
        self.deliver(moved)
        item.refresh_from_db()
        self.assertEqual(WorkItem.objects.count(), 1)
        self.assertEqual(item.scheduled_for.isoformat(), "2026-11-05T16:00:00+00:00")
        self.assertFalse(item.is_completed)

        # A real cancellation of the new booking cancels it.
        self.deliver(booking("invitee.canceled", invitee="INV-2"))
        item.refresh_from_db()
        self.assertTrue(item.title.startswith("Cancelled: "))

    def test_known_contacts_keep_their_lead_source(self):
        self.choose()
        self.turn_on()
        known = make_contact(email="lena@example.test", custom_fields={"lead_source": "Referral"})
        self.deliver(booking())
        known.refresh_from_db()
        self.assertEqual(known.custom_fields, {"lead_source": "Referral"})
        self.assertEqual(WorkItem.objects.get().contact, known)
        # A tracking value that matches no choice leaves the field empty.
        self.choose(setting_lead_source_from="utm_campaign")
        unknown = booking(invitee="INV-9", email="new.lead@example.test")
        self.deliver(unknown)
        self.assertEqual(Contact.objects.get(email="new.lead@example.test").custom_fields, {})


class RefusalTests(FakeCalendlyMixin, TestCase):
    def test_unusable_answers_and_events_are_refused_cleanly(self):
        connection = Connection.objects.create(
            connector_key="calendly",
            label="Calendly",
            enabled_capabilities=["scheduling"],
            credentials={"token": TOKEN},
            config={
                "consult_event_types": [CONSULT],
                "webhook_uri": f"{API_URL}/webhook_subscriptions/W9",
            },
        )
        connector = CalendlyConnector(connection)
        for body, message in (
            (b"not json", "not valid JSON"),
            (json.dumps({"event": "invitee_no_show.created"}).encode(), "not a booking"),
            (json.dumps({"event": "invitee.created", "payload": {}}).encode(), "no invitee"),
        ):
            with self.assertRaisesMessage(ConnectorError, message):
                connector.parse_webhook(body, {})
        envelope = connector.parse_webhook(json.dumps(booking()).encode(), {})
        with self.assertRaisesMessage(ConnectorError, "no start time"):
            connector.handle_webhook("scheduling", {**envelope.payload, "starts_at": "soon"})

        self.calendly = lambda request: httpx.Response(403 if request.method == "GET" else 500)
        with self.assertRaisesMessage(ConnectorError, "have the scopes listed"):
            connector.test_connection()
        with self.assertRaisesMessage(ConnectorError, "Calendly answered HTTP 500"):
            connector.remove_webhook()
        self.calendly = lambda request: httpx.Response(200, json={"resource": {}})
        with self.assertRaisesMessage(ConnectorError, "which organization"):
            CalendlyConnector(connection).test_connection()
