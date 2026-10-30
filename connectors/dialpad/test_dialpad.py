import json
from datetime import UTC, datetime, timedelta
from unittest import mock

import httpx
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.comms.models import Communication
from crm.comms.texting import send_text
from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, OutboundRequest, WebhookEvent
from crm.connect.services import acknowledge_data_flows, create_connection, disconnect
from crm.connect.tasks import process_webhook_event
from crm.connect.testing import ConnectorContract
from crm.tests.factories import make_contact, make_user

from . import (
    BACKFILL_DAYS,
    CALL_STATES,
    DialpadConnector,
    b64url,
    call_payload,
    jwt_claims,
    sign_jwt,
    validate_number,
)

API_KEY = "fictional-dialpad-key"
FIRM_NUMBER = "+12025550143"
CLIENT_NUMBER = "+12025550144"
STARTED = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
STAFF_EMAIL = "attorney@example.test"


def ms(moment):
    return int(moment.timestamp() * 1000)


def call_event(state="hangup", call_id=5747322335264768, **extra):
    return {
        "call_id": call_id,
        "state": state,
        "direction": "inbound",
        "external_number": CLIENT_NUMBER,
        "internal_number": FIRM_NUMBER,
        "date_started": ms(STARTED),
        "date_ended": ms(STARTED + timedelta(minutes=3)),
        "duration": 182503.75,
        "event_timestamp": ms(STARTED + timedelta(minutes=3)),
        "was_recorded": False,
        "voicemail_link": None,
        "target": {"type": "user", "id": 5908860123456789, "email": STAFF_EMAIL},
        **extra,
    }


def sms_event(text="Thank you", sms_id=5747322335264456, direction="inbound", **extra):
    inbound = direction == "inbound"
    return {
        "id": sms_id,
        "created_date": ms(STARTED),
        "direction": direction,
        "from_number": CLIENT_NUMBER if inbound else FIRM_NUMBER,
        "to_number": [FIRM_NUMBER if inbound else CLIENT_NUMBER],
        "mms": False,
        "text": text,
        **extra,
    }


class FakeDialpad:
    """Answers like Dialpad's API for one fictional company."""

    def __init__(self):
        self.requests = []
        self.users = {STAFF_EMAIL: 5908860123456789}
        self.calls = []
        self.webhooks = 0
        self.sms_status = "pending"

    def __call__(self, request):
        self.requests.append(request)
        if request.headers.get("Authorization") != f"Bearer {API_KEY}":
            return httpx.Response(401, json={"error": {"message": "Unauthorized"}})
        path = request.url.path.removeprefix("/api/v2")
        body = json.loads(request.content) if request.content else {}
        if path == "/users":
            found = self.users.get(request.url.params.get("email"))
            return httpx.Response(200, json={"items": [{"id": found}] if found else []})
        if path == "/sms":
            return httpx.Response(
                200, json={"id": 6100000000000001, "message_status": self.sms_status, **body}
            )
        if path == "/webhooks" and request.method == "POST":
            self.webhooks += 1
            return httpx.Response(200, json={"id": str(190 + self.webhooks), **body})
        if path == "/call":
            cursor = request.url.params.get("cursor")
            page = 1 if cursor else 0
            items = self.calls[page * 2 : page * 2 + 2]
            more = len(self.calls) > (page + 1) * 2
            return httpx.Response(200, json={"items": items, "cursor": "page-2" if more else None})
        return httpx.Response(200, json={"id": 1})

    def paths(self, method=None):
        return [
            r.url.path.removeprefix("/api/v2")
            for r in self.requests
            if method is None or r.method == method
        ]

    def body(self, path):
        request = next(r for r in reversed(self.requests) if r.url.path.endswith(path))
        return json.loads(request.content)


class FakeDialpadMixin:
    def setUp(self):
        super().setUp()
        self.dialpad = FakeDialpad()
        transport = http.use_transport(httpx.MockTransport(lambda r: self.dialpad(r)))
        transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)
        patcher = mock.patch.object(http, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def connect(self, admin, **config):
        connection = create_connection(
            "dialpad",
            label="Dialpad",
            created_by=admin,
            capabilities=["phone", "texting"],
            credentials={"api_key": API_KEY},
            config=config,
        )
        acknowledge_data_flows(connection, admin)
        Connection.objects.filter(pk=connection.pk).update(preview_mode=False)
        connection.refresh_from_db()
        return connection


class DialpadContractTests(FakeDialpadMixin, ConnectorContract, TestCase):
    connector_class = DialpadConnector
    valid_credentials = {"api_key": API_KEY}
    invalid_credentials = {"api_key": "wrong"}
    signature_in_headers = False
    sync_examples = {"phone": {}}
    outbound_examples = {
        "send_text": {"to": CLIENT_NUMBER, "body": "Fictional reminder", "staff_email": STAFF_EMAIL}
    }

    def setUp(self):
        super().setUp()
        self.dialpad.calls = [call_event(call_id=n) for n in range(1, 4)]

    def make_webhook(self, secret):
        return sign_jwt(call_event(), secret), {"Content-Type": "application/jwt"}


class DialpadConnectorTests(FakeDialpadMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user("attorney")
        self.contact = make_contact(self.admin, phone="(202) 555-0144")
        self.connection = self.connect(self.admin)

    def test_tokens_are_checked_strictly(self):
        token = sign_jwt(call_event(), "secret")
        self.assertEqual(jwt_claims(token, "secret")["call_id"], 5747322335264768)
        self.assertIsNone(jwt_claims(token, "other"))
        header, payload, sig = token.split(b".")
        forged = b64url(json.dumps({**call_event(), "external_number": "+12025550199"}).encode())
        self.assertIsNone(jwt_claims(header + b"." + forged + b"." + sig, "secret"))
        unsigned = b64url(json.dumps({"alg": "none"}).encode()) + b"." + payload + b"."
        self.assertIsNone(jwt_claims(unsigned, "secret"))
        self.assertIsNone(jwt_claims(json.dumps(call_event()).encode(), "secret"))
        self.assertIsNone(jwt_claims(b"a.b.c", "secret"))

    def test_texts_go_from_the_staff_members_line_or_the_firm_number(self):
        request = send_text(
            self.connection, contact=self.contact, body="Reminder", actor=self.admin
        )
        self.assertEqual(request.status, OutboundRequest.Status.SENT)
        self.assertEqual(request.provider_ref, "6100000000000001")
        self.assertEqual(
            self.dialpad.body("/sms"),
            {
                "to_numbers": [CLIENT_NUMBER],
                "text": "Reminder",
                "infer_country_code": False,
                "user_id": 5908860123456789,
            },
        )
        self.dialpad.users.clear()
        failed = send_text(self.connection, contact=self.contact, body="Again", actor=self.admin)
        self.assertIn("No Dialpad user has your email address", failed.error)
        self.connection.config = {**self.connection.config, "from_number": FIRM_NUMBER}
        self.connection.save()
        send_text(self.connection, contact=self.contact, body="From the firm", actor=self.admin)
        self.assertEqual(self.dialpad.body("/sms")["from_number"], FIRM_NUMBER)
        self.dialpad.sms_status = "failed"
        refused = send_text(self.connection, contact=self.contact, body="No", actor=self.admin)
        self.assertEqual(refused.error, "Dialpad did not send the text.")

    def test_settings(self):
        validate_number(FIRM_NUMBER)
        with self.assertRaises(ValidationError):
            validate_number("202-555-0143")
        self.assertEqual(self.connection.config, {"environment": "production", "from_number": ""})
        sandbox = self.connect(self.admin, environment="sandbox")
        DialpadConnector(sandbox).test_connection()
        self.assertEqual(self.dialpad.requests[-1].url.host, "sandbox.dialpad.com")
        sandbox.credentials = {"api_key": "wrong"}
        with self.assertRaisesMessage(ConnectorError, "did not accept the API key"):
            DialpadConnector(sandbox).test_connection()

    def test_unusable_events_and_answers_are_refused_cleanly(self):
        connector = DialpadConnector(self.connection)
        for event, message in (
            ([1, 2], "not understood"),
            ({**call_event(), "call_id": ""}, "no call ID"),
        ):
            with self.assertRaisesMessage(ConnectorError, message):
                connector.parse_webhook(sign_jwt(event, "s"), {})
        with self.assertRaisesMessage(ConnectorError, "not a valid token"):
            connector.parse_webhook(b"no-dots", {})
        for payload in (
            call_payload(call_event(external_number=None)),
            call_payload(call_event(direction="internal")),
        ):
            self.assertEqual(connector.handle_webhook("phone", payload), [])
        text = connector.parse_webhook(
            sign_jwt(sms_event(direction="outbound", to_number=[]), "s"), {}
        )
        self.assertEqual(connector.handle_webhook("texting", text.payload), [])
        with self.assertRaisesMessage(ConnectorError, "cannot call"):
            connector.perform("call", {})
        self.dialpad = lambda request: httpx.Response(403 if request.method == "GET" else 500)
        with self.assertRaisesMessage(ConnectorError, "Check the API key's scopes"):
            connector.test_connection()
        self.connection.config = {**self.connection.config, "webhook_id": 5}
        with self.assertRaisesMessage(ConnectorError, "Dialpad answered HTTP 500"):
            DialpadConnector(self.connection).remove_webhook()

    def test_sync_backfills_calls_and_then_catches_up(self):
        self.dialpad.calls = [
            call_event(call_id=n, date_started=ms(STARTED + timedelta(minutes=n))) for n in range(3)
        ]
        with mock.patch("django.utils.timezone.now", return_value=STARTED + timedelta(days=1)):
            batch = DialpadConnector(self.connection).sync("phone", {})
        first = self.dialpad.requests[0].url.params
        self.assertEqual(
            int(first["started_after"]),
            ms(STARTED + timedelta(days=1) - timedelta(days=BACKFILL_DAYS, hours=6)),
        )
        self.assertEqual(self.dialpad.requests[1].url.params["cursor"], "page-2")
        self.assertEqual([r.external_id for r in batch.records], ["0", "1", "2"])
        self.assertEqual(batch.records[0].duration_seconds, 183)
        self.assertEqual(batch.cursor, {"after": ms(STARTED + timedelta(minutes=2))})
        DialpadConnector(self.connection).sync("phone", batch.cursor)
        self.assertEqual(
            int(self.dialpad.requests[-1].url.params["started_after"]),
            ms(STARTED + timedelta(minutes=2) - timedelta(hours=6)),
        )


class DialpadWebhookTests(FakeDialpadMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user("attorney")
        self.contact = make_contact(self.admin, phone="(202) 555-0144")
        self.connection = self.connect(self.admin)
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("connection_webhooks", args=[self.connection.pk]), {"register": "on"}
        )
        self.url = response.context["webhook_url"]
        self.secret = response.context["secret"]
        self.response = response
        self.connection.refresh_from_db()

    def post(self, event, secret=None):
        with self.captureOnCommitCallbacks(execute=True):
            answer = self.client.post(
                self.url, sign_jwt(event, secret or self.secret), content_type="application/jwt"
            )
        for queued in WebhookEvent.objects.filter(status=WebhookEvent.Status.QUEUED):
            process_webhook_event.call(queued.pk)
        return answer

    def test_registration_creates_the_webhook_and_subscriptions(self):
        self.assertContains(
            self.response, "created the webhook and subscribed it to calls and texts"
        )
        self.assertEqual(
            self.dialpad.body("/webhooks"), {"hook_url": self.url, "secret": self.secret}
        )
        self.assertEqual(
            self.dialpad.body("/subscriptions/call"),
            {"endpoint_id": 191, "call_states": list(CALL_STATES), "enabled": True},
        )
        self.assertEqual(self.dialpad.body("/subscriptions/sms")["direction"], "all")
        self.assertEqual(self.connection.config["webhook_id"], 191)
        # Issuing a new address replaces the old webhook instead of adding another.
        self.client.post(
            reverse("connection_webhooks", args=[self.connection.pk]), {"register": "on"}
        )
        self.assertIn("/webhooks/191", self.dialpad.paths("DELETE"))
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.config["webhook_id"], 192)
        disconnect(self.connection, self.admin)
        self.assertIn("/webhooks/192", self.dialpad.paths("DELETE"))

    def test_a_calls_events_build_one_record(self):
        self.assertEqual(self.post(call_event()).status_code, 200)
        self.post(
            call_event(
                "recording",
                recording_details=[{"url": "https://dialpad.example.test/r/1", "duration": 180000}],
                event_timestamp=ms(STARTED + timedelta(minutes=4)),
            )
        )
        self.post(
            call_event(
                "recap_summary",
                recap_summary="Asked when the interview notice will arrive.",
                event_timestamp=ms(STARTED + timedelta(minutes=5)),
            )
        )
        call = Communication.objects.get()
        self.assertEqual(
            (call.contact, call.direction, call.occurred_at), (self.contact, "inbound", STARTED)
        )
        self.assertEqual(call.duration, timedelta(seconds=183))
        self.assertEqual(call.logged_by, self.admin)
        self.assertEqual(
            call.summary,
            "Recording: https://dialpad.example.test/r/1\n\n"
            "Asked when the interview notice will arrive.",
        )
        self.post(
            call_event(
                "voicemail_uploaded", call_id=2, voicemail_link="https://dialpad.example.test/v"
            )
        )
        self.assertEqual(Communication.objects.get(external_id="2").subject, "Call voicemail")

    def test_texts_arrive_both_ways_and_stop_opts_out(self):
        self.post(sms_event())
        text = Communication.objects.get()
        self.assertEqual(
            (text.channel, text.summary, text.contact), ("text", "Thank you", self.contact)
        )
        sent = send_text(self.connection, contact=self.contact, body="Reminder", actor=self.admin)
        self.post(
            sms_event(
                "Reminder",
                sms_id=int(sent.provider_ref),
                direction="outbound",
                message_status="delivered",
            )
        )
        reminder = Communication.objects.get(external_id=sent.provider_ref)
        self.assertEqual((reminder.delivery_status, reminder.logged_by), ("delivered", self.admin))
        self.post(sms_event("STOP", sms_id=7))
        self.contact.refresh_from_db()
        self.assertIsNotNone(self.contact.texting_opted_out_at)

    def test_unsigned_or_wrongly_signed_events_are_refused(self):
        forged = self.client.post(
            self.url, json.dumps(sms_event()), content_type="application/json"
        )
        self.assertEqual(forged.status_code, 401)
        self.assertEqual(self.post(sms_event(), secret="other").status_code, 401)
        self.assertEqual(self.post({"something": "else"}).status_code, 400)
        self.assertFalse(Communication.objects.exists())
        self.assertLess(timezone.now() - self.connection.created_at, timedelta(minutes=5))
