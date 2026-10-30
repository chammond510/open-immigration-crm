import json
from datetime import UTC, datetime, timedelta
from unittest import mock

import httpx
from django.test import TestCase
from django.urls import reverse

from crm.connect import http
from crm.connect.models import Connection, WebhookEvent
from crm.connect.services import acknowledge_data_flows, disconnect
from crm.connect.tasks import process_webhook_event
from crm.connect.testing import ConnectorContract
from crm.models import Contact, WorkItem
from crm.tests.factories import make_user

from . import HOSTED_API, SIGNATURE_HEADER, CalComConnector, sign

KEY = "cal_live_fictional"
STAFF_EMAIL = "paralegal@example.test"


def now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def booking(trigger="BOOKING_CREATED", uid="BK-1", *, event_type=11, **extra):
    payload = {
        "uid": uid,
        "bookingId": 501,
        "title": "Immigration consultation between Example Firm and Lena Fictional",
        "eventTitle": "Immigration consultation",
        "type": "consult",
        "eventTypeId": event_type,
        "startTime": "2026-11-03T15:00:00Z",
        "endTime": "2026-11-03T15:30:00Z",
        "status": "CANCELLED" if trigger == "BOOKING_CANCELLED" else "ACCEPTED",
        "organizer": {"name": "Paralegal", "email": STAFF_EMAIL, "timeZone": "America/Chicago"},
        "attendees": [
            {"name": "Lena Fictional", "email": "lena@example.test", "timeZone": "America/Chicago"}
        ],
        "responses": {
            "name": {"label": "Your name", "value": "Lena Fictional", "isHidden": False},
            "email": {"label": "Email", "value": "lena@example.test", "isHidden": False},
            "attendeePhoneNumber": {
                "label": "Phone",
                "value": "+1 202-555-0144",
                "isHidden": False,
            },
            "case": {
                "label": "What is your case about?",
                "value": "Family petition",
                "isHidden": False,
            },
            "languages": {"label": "Languages", "value": ["Spanish", "English"], "isHidden": False},
            "internal": {"label": "Internal", "value": "hidden", "isHidden": True},
        },
        "location": "integrations:daily",
        "metadata": {"videoCallUrl": "https://meet.example.test/consult"},
        "oneTimePassword": None,
        **extra,
    }
    return {"triggerEvent": trigger, "createdAt": now(), "payload": payload}


class FakeCalCom:
    def __init__(self):
        self.requests = []
        self.made = 0

    def __call__(self, request):
        self.requests.append(request)
        if request.headers.get("Authorization") != f"Bearer {KEY}":
            return httpx.Response(
                401,
                json={"status": "error", "error": {"code": "UnauthorizedException"}},
            )
        path = request.url.path
        if path.endswith("/me"):
            return httpx.Response(200, json={"status": "success", "data": {"id": 7}})
        if path.endswith("/event-types"):
            assert request.headers["cal-api-version"] == "2026-06-12"
            data = [{"id": 11, "title": "Immigration consultation"}, {"id": 12, "slug": "quick"}]
            return httpx.Response(200, json={"status": "success", "data": data})
        if path.endswith("/webhooks") and request.method == "POST":
            self.made += 1
            return httpx.Response(201, json={"status": "success", "data": {"id": self.made}})
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(404)

    def calls(self, method, path=""):
        return [r for r in self.requests if r.method == method and path in r.url.path]


class FakeCalComMixin:
    def setUp(self):
        super().setUp()
        self.calcom = FakeCalCom()
        transport = http.use_transport(httpx.MockTransport(lambda r: self.calcom(r)))
        transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)
        patcher = mock.patch.object(http, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)


def signed(event, secret):
    body = json.dumps(event).encode()
    return body, {SIGNATURE_HEADER: sign(body, secret)}


class CalComContractTests(FakeCalComMixin, ConnectorContract, TestCase):
    connector_class = CalComConnector
    valid_credentials = {"api_key": KEY}
    invalid_credentials = {"api_key": "cal_live_revoked"}
    connection_config = {"consult_event_types": ["11"]}

    def make_webhook(self, secret):
        return signed(booking(), secret)


class SignatureTests(TestCase):
    def test_signatures_and_age_are_checked(self):
        connector = CalComConnector(
            Connection(connector_key="calcom", enabled_capabilities=["scheduling"])
        )
        body, headers = signed(booking(), "secret")
        self.assertTrue(connector.verify_webhook(body, headers, "secret"))
        self.assertTrue(
            connector.verify_webhook(
                body, {SIGNATURE_HEADER: headers[SIGNATURE_HEADER].upper()}, "secret"
            )
        )
        self.assertFalse(connector.verify_webhook(body + b" ", headers, "secret"))
        self.assertFalse(connector.verify_webhook(body, headers, "other"))
        self.assertFalse(connector.verify_webhook(body, {}, "secret"))
        unsigned = {SIGNATURE_HEADER: "no-secret-provided"}
        self.assertFalse(connector.verify_webhook(body, unsigned, "secret"))
        old = booking()
        old["createdAt"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        self.assertFalse(connector.verify_webhook(*signed(old, "secret"), "secret"))
        undated = booking()
        undated.pop("createdAt")
        self.assertFalse(connector.verify_webhook(*signed(undated, "secret"), "secret"))
        listed = json.dumps([1]).encode()
        self.assertFalse(
            connector.verify_webhook(listed, {SIGNATURE_HEADER: sign(listed, "s")}, "s")
        )


class CalComFlowTests(FakeCalComMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user()
        self.staff = make_user("paralegal", superuser=False)
        self.connection = Connection.objects.create(
            connector_key="calcom",
            label="Cal.com",
            enabled_capabilities=["scheduling"],
            credentials={"api_key": KEY},
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
        data = {"setting_api_url": HOSTED_API, "setting_consult_event_types": ["11"], **settings}
        return self.client.post(reverse("connection_config", args=[self.connection.pk]), data)

    def test_settings_and_webhook_setup(self):
        page = self.client.get(reverse("connection_detail", args=[self.connection.pk]))
        self.assertContains(page, "Immigration consultation")
        self.assertContains(page, "quick")
        self.choose()
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.config["consult_event_types"], ["11"])
        refused = self.choose(setting_api_url="http://cal.example.test/api/v2")
        self.assertEqual(refused.status_code, 302)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.config["api_url"], HOSTED_API)

        response = self.turn_on()
        self.assertContains(response, "Cal.com will send this account&#x27;s bookings here.")
        self.assertContains(response, "For team event types")
        made = json.loads(self.calcom.calls("POST", "/webhooks")[0].content)
        self.assertEqual(
            made,
            {
                "subscriberUrl": self.url,
                "triggers": ["BOOKING_CREATED", "BOOKING_RESCHEDULED", "BOOKING_CANCELLED"],
                "active": True,
                "secret": self.secret,
                "version": "2021-10-20",
            },
        )
        self.turn_on()
        self.assertTrue(str(self.calcom.calls("DELETE")[0].url).endswith("/v2/webhooks/1"))
        disconnect(self.connection, self.admin)
        self.assertTrue(str(self.calcom.calls("DELETE")[-1].url).endswith("/v2/webhooks/2"))

    def test_bookings_reschedules_and_cancellations(self):
        self.choose()
        self.turn_on()
        self.assertEqual(
            self.deliver({"triggerEvent": "PING", "createdAt": now()}).status_code, 200
        )
        self.deliver(booking(event_type=12, uid="BK-0"))
        self.assertFalse(Contact.objects.exists())

        self.deliver(booking())
        contact = Contact.objects.get()
        self.assertEqual((contact.full_name, contact.stage), ("Lena Fictional", "consultation"))
        self.assertEqual(contact.phone_e164, "+12025550144")
        item = WorkItem.objects.get()
        self.assertEqual(item.scheduled_for.isoformat(), "2026-11-03T15:00:00+00:00")
        self.assertEqual(item.assigned_to, self.staff)
        self.assertIn("What is your case about?: Family petition", item.description)
        self.assertIn("Languages: Spanish, English", item.description)
        self.assertNotIn("hidden", item.description)
        self.assertIn("https://meet.example.test/consult", item.description)

        moved = booking(
            "BOOKING_RESCHEDULED",
            uid="BK-2",
            rescheduleUid="BK-1",
            startTime="2026-11-05T16:00:00Z",
            endTime="2026-11-05T16:30:00Z",
        )
        self.deliver(moved)
        item = WorkItem.objects.get()
        self.assertEqual(item.scheduled_for.isoformat(), "2026-11-05T16:00:00+00:00")
        self.assertFalse(item.is_completed)

        self.deliver(booking("BOOKING_CANCELLED", uid="BK-2", cancellationReason="Changed plans"))
        item.refresh_from_db()
        self.assertTrue(item.is_completed)
        self.assertTrue(item.title.startswith("Cancelled: "))

    def test_phone_only_bookers_and_refused_deliveries(self):
        self.choose()
        self.turn_on()
        phone_only = booking(uid="BK-9")
        phone_only["payload"]["attendees"][0]["email"] = "12025550144@sms.cal.com"
        self.deliver(phone_only)
        self.assertEqual(Contact.objects.get().email, "")
        body, headers = signed(booking(uid="BK-10"), "not-the-secret")
        refused = self.client.post(self.url, body, content_type="application/json", headers=headers)
        self.assertNotEqual(refused.status_code, 200)
        self.assertEqual(Contact.objects.count(), 1)

    def test_odd_events(self):
        connector = CalComConnector(self.connection)
        for body, message in (
            (b"not json", "not valid JSON"),
            (b"[1]", "not understood"),
            (json.dumps({"triggerEvent": "MEETING_ENDED"}).encode(), "not a booking"),
            (json.dumps({"triggerEvent": "BOOKING_CREATED", "payload": {}}).encode(), "no booking"),
        ):
            with self.assertRaisesMessage(Exception, message):
                connector.parse_webhook(body, {})
        envelope = connector.parse_webhook(json.dumps(booking(startTime="soon")).encode(), {})
        self.connection.config = {"consult_event_types": ["11"]}
        with self.assertRaisesMessage(Exception, "no start time"):
            CalComConnector(self.connection).handle_webhook("scheduling", envelope.payload)

    def test_keys_and_self_hosted_addresses(self):
        self.connection.credentials = {"api_key": "cal_live_revoked"}
        with self.assertRaisesMessage(Exception, "did not accept the API key"):
            CalComConnector(self.connection).test_connection()
        self.connection.credentials = {"api_key": KEY}
        self.connection.config = {"api_url": "http://10.0.0.5/api/v2"}
        CalComConnector(self.connection).test_connection()
        self.assertEqual(str(self.calcom.requests[-1].url), "http://10.0.0.5/api/v2/me")
        self.connection.config = {"api_url": "https://cal.example.test/nowhere"}
        with mock.patch.object(FakeCalCom, "__call__", lambda fake, request: httpx.Response(404)):
            with self.assertRaisesMessage(Exception, "check the API address"):
                CalComConnector(self.connection).test_connection()
