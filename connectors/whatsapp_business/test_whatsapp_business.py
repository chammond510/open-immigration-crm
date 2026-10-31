import hashlib
import hmac
import json
from datetime import timedelta
from unittest import mock

import httpx
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.comms.models import Communication
from crm.comms.texting import can_text_freely, send_template, send_text
from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, OutboundRequest, WebhookEvent
from crm.connect.services import acknowledge_data_flows, create_connection
from crm.connect.tasks import process_webhook_event
from crm.connect.testing import ConnectorContract
from crm.tests.factories import make_contact, make_user

from . import (
    SIGNATURE_HEADER,
    WhatsAppBusinessConnector,
    delivery_update,
    describe,
    incoming,
    template_of,
)

TOKEN = "fictional-system-user-token"
APP_SECRET = "fictional-app-secret"
NUMBER_ID = "100000000000001"
ACCOUNT_ID = "200000000000002"
CLIENT_NUMBER = "+12025550144"
CREDENTIALS = {
    "access_token": TOKEN,
    "phone_number_id": NUMBER_ID,
    "business_account_id": ACCOUNT_ID,
    "app_secret": APP_SECRET,
}
TEMPLATES = [
    {
        "name": "appointment_reminder",
        "language": "en_US",
        "status": "APPROVED",
        "category": "UTILITY",
        "components": [
            {
                "type": "HEADER",
                "format": "TEXT",
                "text": "Appointment on {{1}}",
                "example": {"header_text": ["Tuesday"]},
            },
            {
                "type": "BODY",
                "text": "Hello {{1}}, your appointment with {{2}} is on {{3}}. Reply here with "
                "any questions.",
                "example": {"body_text": [["Ana", "the firm", "Tuesday at 10 am"]]},
            },
            {"type": "FOOTER", "text": "Example Law Office"},
            {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Confirm"}]},
        ],
    },
    {
        "name": "case_update",
        "language": "es",
        "status": "APPROVED",
        "category": "UTILITY",
        "parameter_format": "NAMED",
        "components": [
            {
                "type": "BODY",
                "text": "Hola {{first_name}}, tenemos novedades sobre su caso.",
                "example": {
                    "body_text_named_params": [{"param_name": "first_name", "example": "Ana"}]
                },
            }
        ],
    },
    # Left out: a photo header, a one-time code, a link with a value, and a carousel.
    {
        "name": "new_service",
        "language": "en_US",
        "status": "APPROVED",
        "category": "MARKETING",
        "components": [{"type": "HEADER", "format": "IMAGE"}, {"type": "BODY", "text": "New!"}],
    },
    {
        "name": "sign_in_code",
        "language": "en_US",
        "status": "APPROVED",
        "category": "AUTHENTICATION",
        "components": [{"type": "BODY", "text": "{{1}} is your code."}],
    },
]
SECOND_PAGE = [
    {
        "name": "office_hours",
        "language": "en_US",
        "status": "APPROVED",
        "category": "UTILITY",
        "components": [
            {"type": "BODY", "text": "Our office is open from 9 to 5."},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "PHONE_NUMBER", "text": "Call us", "phone_number": "+12025550143"},
                    {"type": "URL", "text": "Directions", "url": "https://law.example.test/map"},
                ],
            },
        ],
    },
    {
        "name": "pay_invoice",
        "language": "en_US",
        "status": "APPROVED",
        "category": "UTILITY",
        "components": [
            {"type": "BODY", "text": "Your invoice is ready."},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "URL", "text": "Pay", "url": "https://law.example.test/pay/{{1}}"}
                ],
            },
        ],
    },
    {
        "name": "draft_notice",
        "language": "en_US",
        "status": "PENDING",
        "category": "UTILITY",
        "components": [{"type": "BODY", "text": "Not yet approved."}],
    },
    {
        "name": "gallery",
        "language": "en_US",
        "status": "APPROVED",
        "category": "MARKETING",
        "components": [{"type": "BODY", "text": "See these."}, {"type": "CAROUSEL"}],
    },
]


def error(code, status=400):
    return httpx.Response(status, json={"error": {"message": f"(#{code})", "code": code}})


class FakeMeta:
    """Answers like Meta's Graph API for one fictional app, account, and number."""

    def __init__(self):
        self.requests = []
        self.sent = []
        self.refuse = {}  # Client number -> error code for messages to them.

    def __call__(self, request):
        self.requests.append(request)
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return error(190, 401)
        path = request.url.path
        if path == f"/v26.0/{NUMBER_ID}" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": NUMBER_ID,
                    "display_phone_number": "1 202-555-0143",
                    "verified_name": "Example Law Office",
                    "quality_rating": "GREEN",
                },
            )
        if path == f"/v26.0/{ACCOUNT_ID}/message_templates":
            if request.url.params.get("after") == "page-2":
                return httpx.Response(200, json={"data": SECOND_PAGE, "paging": {}})
            following = (
                f"https://graph.facebook.com/v26.0/{ACCOUNT_ID}/message_templates?after=page-2"
            )
            return httpx.Response(
                200, json={"data": TEMPLATES, "paging": {"cursors": {}, "next": following}}
            )
        if path == f"/v26.0/{NUMBER_ID}/messages" and request.method == "POST":
            message = json.loads(request.content)
            if message["to"] in self.refuse:
                return error(self.refuse[message["to"]])
            self.sent.append(message)
            return httpx.Response(
                200,
                json={
                    "messaging_product": "whatsapp",
                    "contacts": [{"input": message["to"], "wa_id": message["to"][1:]}],
                    "messages": [{"id": f"wamid.fictional-{len(self.sent)}"}],
                },
            )
        return error(803, 404)


class FakeMetaMixin:
    def setUp(self):
        super().setUp()
        self.meta = FakeMeta()
        transport = http.use_transport(httpx.MockTransport(lambda r: self.meta(r)))
        transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)
        patcher = mock.patch.object(http, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)


def signed(payload, secret=APP_SECRET):
    body = json.dumps(payload).encode()
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, {SIGNATURE_HEADER: f"sha256={digest}"}


def event(*, messages=(), statuses=(), contacts=(), number_id=NUMBER_ID):
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "12025550143", "phone_number_id": number_id},
    }
    if contacts:
        value["contacts"] = list(contacts)
    if messages:
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = list(statuses)
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": ACCOUNT_ID, "changes": [{"value": value, "field": "messages"}]}],
    }


def text(body="Can we talk tomorrow?", wamid="wamid.in-1", sender="12025550144", at=None):
    at = at or timezone.now()
    return {
        "from": sender,
        "id": wamid,
        "timestamp": str(int(at.timestamp())),
        "type": "text",
        "text": {"body": body},
    }


def status(wamid, state, recipient="12025550144", at=None):
    at = at or timezone.now()
    item = {"id": wamid, "status": state, "timestamp": str(int(at.timestamp()))}
    if recipient:
        item["recipient_id"] = recipient
    return item


def connect(admin):
    connection = create_connection(
        "whatsapp_business",
        label="Client WhatsApp",
        created_by=admin,
        capabilities=["texting"],
        credentials=dict(CREDENTIALS),
    )
    acknowledge_data_flows(connection, admin)
    Connection.objects.filter(pk=connection.pk).update(preview_mode=False)
    connection.refresh_from_db()
    return connection


class WhatsAppContractTests(FakeMetaMixin, ConnectorContract, TestCase):
    connector_class = WhatsAppBusinessConnector
    valid_credentials = CREDENTIALS
    invalid_credentials = {**CREDENTIALS, "access_token": "wrong"}
    outbound_examples = {
        "send_text": {"to": CLIENT_NUMBER, "body": "Fictional reminder"},
        "send_template": {
            "to": CLIENT_NUMBER,
            "template": "case_update:es",
            "values": {"body_first_name": "Ana"},
        },
    }
    webhook_secret = APP_SECRET

    def make_webhook(self, secret):
        return signed(event(messages=[text()]), secret)


class WhatsAppSendingTests(FakeMetaMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.connector = WhatsAppBusinessConnector(connect(make_user()))

    def test_texts_and_templates_are_sent_to_the_number(self):
        message_id = self.connector.perform(
            "send_text", {"to": CLIENT_NUMBER, "body": "See you at 10."}
        )
        self.assertEqual(message_id, "wamid.fictional-1")
        self.assertEqual(
            self.meta.sent[0],
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": CLIENT_NUMBER,
                "type": "text",
                "text": {"preview_url": False, "body": "See you at 10."},
            },
        )
        self.connector.perform(
            "send_template",
            {
                "to": CLIENT_NUMBER,
                "template": "appointment_reminder:en_US",
                "values": {
                    "header_1": "Tuesday",
                    "body_3": "Tuesday at 10 am",
                    "body_1": "Ana",
                    "body_2": "the firm",
                },
            },
        )
        self.assertEqual(
            self.meta.sent[1]["template"],
            {
                "name": "appointment_reminder",
                "language": {"code": "en_US"},
                "components": [
                    {"type": "header", "parameters": [{"type": "text", "text": "Tuesday"}]},
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": "Ana"},
                            {"type": "text", "text": "the firm"},
                            {"type": "text", "text": "Tuesday at 10 am"},
                        ],
                    },
                ],
            },
        )
        self.connector.perform(
            "send_template",
            {
                "to": CLIENT_NUMBER,
                "template": "case_update:es",
                "values": {"body_first_name": "Ana"},
            },
        )
        self.assertEqual(
            self.meta.sent[2]["template"]["components"],
            [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "parameter_name": "first_name", "text": "Ana"}],
                }
            ],
        )
        self.connector.perform(
            "send_template", {"to": CLIENT_NUMBER, "template": "office_hours:en_US", "values": {}}
        )
        self.assertNotIn("components", self.meta.sent[3]["template"])

    def test_refusals_are_explained_without_the_number(self):
        cases = [
            ("+12025550145", 131047, "allows only an approved template"),
            ("+12025550146", 131026, "could not deliver the message"),
            ("+12025550147", 131050, "stop marketing messages"),
            ("+12025550148", 230, "lacks a permission"),
            ("+12025550149", 999999, "Meta refused the request (error 999999)."),
        ]
        for number, code, message in cases:
            self.meta.refuse[number] = code
            with self.assertRaisesMessage(ConnectorError, message) as caught:
                self.connector.perform("send_text", {"to": number, "body": "Hello"})
            self.assertNotIn(number, str(caught.exception))
        for payload, message in (
            ({"to": "12025550144", "body": "Hello"}, "not a valid international number"),
            ({"to": CLIENT_NUMBER, "body": "x" * 4097}, "at most 4096 characters"),
        ):
            with self.assertRaisesMessage(ConnectorError, message):
                self.connector.perform("send_text", payload)
        with self.assertRaisesMessage(ConnectorError, "Choose an approved template."):
            self.connector.perform("send_template", {"to": CLIENT_NUMBER, "template": "x"})
        with self.assertRaisesMessage(ConnectorError, "cannot send_fax"):
            self.connector.perform("send_fax", {})

    def test_odd_answers(self):
        answers = iter(
            [
                httpx.Response(200, json={"messages": []}),
                httpx.Response(200, json=["not", "an", "object"]),
                httpx.Response(400, content=b"not json"),
                httpx.Response(200, json={"id": NUMBER_ID}),
            ]
        )
        with http.use_transport(httpx.MockTransport(lambda r: next(answers))):
            with self.assertRaisesMessage(ConnectorError, "did not return a message ID"):
                self.connector.perform("send_text", {"to": CLIENT_NUMBER, "body": "Hi"})
            with self.assertRaisesMessage(ConnectorError, "not understood"):
                self.connector.perform("send_text", {"to": CLIENT_NUMBER, "body": "Hi"})
            with self.assertRaisesMessage(ConnectorError, "Meta answered HTTP 400."):
                self.connector.perform("send_text", {"to": CLIENT_NUMBER, "body": "Hi"})
            with self.assertRaisesMessage(ConnectorError, "did not find the firm's WhatsApp"):
                self.connector.test_connection()

    def test_connection_test_checks_the_ids(self):
        self.connector.test_connection()
        self.assertEqual(len(self.meta.requests), 2)
        for name, label in (
            ("phone_number_id", "phone number ID"),
            ("business_account_id", "WhatsApp Business Account ID"),
        ):
            self.connector.connection.credentials = {**CREDENTIALS, name: "12-34"}
            with self.assertRaisesMessage(ConnectorError, f"The {label} should be digits only."):
                self.connector.test_connection()

    def test_templates_that_need_only_words_are_offered(self):
        templates = self.connector.message_templates()
        self.assertEqual(
            [t.id for t in templates],
            ["appointment_reminder:en_US", "case_update:es", "office_hours:en_US"],
        )
        reminder, update, hours = templates
        self.assertEqual(
            reminder.text,
            "Appointment on {{header_1}}\n\nHello {{body_1}}, your appointment with {{body_2}} "
            "is on {{body_3}}. Reply here with any questions.\n\nExample Law Office\n\n[Confirm]",
        )
        self.assertEqual(
            [(f.key, f.label, f.example) for f in reminder.fields],
            [
                ("header_1", "Value 1 in the header", "Tuesday"),
                ("body_1", "Value 1 in the message", "Ana"),
                ("body_2", "Value 2 in the message", "the firm"),
                ("body_3", "Value 3 in the message", "Tuesday at 10 am"),
            ],
        )
        self.assertEqual((reminder.category, reminder.language), ("utility", "en_US"))
        self.assertEqual(
            [(f.key, f.label, f.example) for f in update.fields],
            [("body_first_name", "First name", "Ana")],
        )
        self.assertEqual(hours.text, "Our office is open from 9 to 5.\n\n[Call us]\n\n[Directions]")
        # Both pages were read.
        self.assertEqual([r.url.params.get("after") for r in self.meta.requests], [None, "page-2"])

    def test_odd_events_and_templates_are_left_out(self):
        self.assertIsNone(incoming("not a message", {}))
        self.assertIsNone(incoming({**text(), "timestamp": "soon"}, {}))
        self.assertIsNone(incoming({**text(), "from": "not a number"}, {}))
        self.assertIsNone(delivery_update("not a status"))
        self.assertIsNone(delivery_update({**status("wamid.1", "read"), "timestamp": "x" * 400}))
        self.assertIsNone(template_of({"status": "APPROVED", "name": "", "language": "en"}))
        self.assertIsNone(template_of("not a template"))
        # An unknown endpoint is refused like any other error.
        with self.assertRaisesMessage(ConnectorError, "error 803"):
            self.connector.call("GET", "/unknown")

    def test_how_incoming_messages_read(self):
        cases = [
            ({"type": "button", "button": {"text": "Confirm", "payload": "x"}}, "Confirm"),
            (
                {"type": "interactive", "interactive": {"button_reply": {"title": "Yes"}}},
                "Yes",
            ),
            (
                {"type": "interactive", "interactive": {"list_reply": {"title": "Monday"}}},
                "Monday",
            ),
            (
                {"type": "document", "document": {"filename": "passport.pdf"}},
                "[Sent a document, which the CRM does not download. Ask the client to send it "
                "another way if it is needed.]",
            ),
            (
                {"type": "location", "location": {"name": "Office", "address": "1 Main St"}},
                "[Shared a location: Office, 1 Main St.]",
            ),
            ({"type": "location", "location": {}}, "[Shared a location.]"),
            ({"type": "contacts", "contacts": [{}]}, "[Shared a contact card.]"),
            (
                {"type": "system", "system": {"body": "The user changed their number."}},
                "[WhatsApp: The user changed their number.]",
            ),
            ({"type": "reaction", "reaction": {"emoji": "+1"}}, ""),
            (
                {"type": "unsupported"},
                "[Sent a kind of message WhatsApp does not share with the CRM.]",
            ),
        ]
        for message, expected in cases:
            self.assertEqual(describe(message), expected)


class WhatsAppWebhookTests(FakeMetaMixin, TestCase):
    """Turning webhooks on, Meta's address check, and its deliveries end to end."""

    def setUp(self):
        super().setUp()
        self.admin = make_user()
        self.contact = make_contact(self.admin, phone="(202) 555-0144")
        self.connection = connect(self.admin)
        self.client.force_login(self.admin)
        response = self.client.post(reverse("connection_webhooks", args=[self.connection.pk]))
        self.url = response.context["webhook_url"]
        self.verify_token = dict(response.context["details"])["Verify token"]
        self.page = response

    def post(self, payload, secret=APP_SECRET):
        body, headers = signed(payload, secret)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.url, body, content_type="application/json", headers=headers
            )
        for item in WebhookEvent.objects.filter(status=WebhookEvent.Status.QUEUED):
            process_webhook_event.call(item.pk)
        return response

    def check(self, **params):
        query = {"hub.mode": "subscribe", "hub.challenge": "1158201444", **params}
        return self.client.get(self.url, query)

    def test_the_setup_page_shows_the_verify_token_and_meta_can_check_it(self):
        self.assertContains(self.page, "Verify token")
        self.assertContains(self.page, self.verify_token)
        self.assertContains(self.page, "subscribe to the messages field")
        self.assertNotContains(self.page, APP_SECRET)
        answer = self.check(**{"hub.verify_token": self.verify_token})
        self.assertEqual((answer.status_code, answer.content), (200, b"1158201444"))
        for params in (
            {"hub.verify_token": "guessed"},
            {"hub.verify_token": self.verify_token, "hub.challenge": "<script>"},
            {"hub.verify_token": self.verify_token, "hub.mode": "unsubscribe"},
        ):
            self.assertEqual(self.check(**params).status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        # Each address has its own token.
        other = self.client.post(reverse("connection_webhooks", args=[self.connection.pk]))
        self.assertNotEqual(dict(other.context["details"])["Verify token"], self.verify_token)
        self.assertFalse(WebhookEvent.objects.exists())

    def test_messages_and_updates_land_on_the_contact(self):
        # Until the client writes, only a template can go.
        with self.assertRaisesMessage(ValidationError, "only within 24 hours"):
            send_text(self.connection, contact=self.contact, body="Hello", actor=self.admin)
        photo = {
            "from": "12025550144",
            "id": "wamid.in-2",
            "timestamp": str(int(timezone.now().timestamp())),
            "type": "image",
            "image": {"id": "1", "mime_type": "image/jpeg", "caption": "My receipt notice"},
        }
        reaction = {**text(wamid="wamid.in-3"), "type": "reaction", "reaction": {"emoji": "+1"}}
        batch = event(messages=[text(), photo, reaction])
        other_number = event(messages=[text(wamid="wamid.elsewhere")], number_id="999")
        batch["entry"] += other_number["entry"] + [
            "not an entry",
            {"changes": [{"field": "account_update", "value": {}}, {"field": "messages"}]},
        ]
        self.assertEqual(self.post(batch).status_code, 200)
        received = Communication.objects.filter(direction="inbound").order_by("external_id")
        self.assertEqual(
            [(c.external_id, c.contact, c.counterpart) for c in received],
            [
                ("wamid.in-1", self.contact, CLIENT_NUMBER),
                ("wamid.in-2", self.contact, CLIENT_NUMBER),
            ],
        )
        self.assertEqual(received[0].summary, "Can we talk tomorrow?")
        self.assertTrue(received[1].summary.startswith("My receipt notice\n\n[Sent a photo,"))
        # Meta retries with the same body; it is filed once.
        self.post(batch)
        self.assertEqual(WebhookEvent.objects.count(), 1)

        self.assertTrue(can_text_freely(self.connection, self.contact))
        sent = send_text(
            self.connection, contact=self.contact, body="Yes, at 10.", actor=self.admin
        )
        self.assertEqual(sent.status, OutboundRequest.Status.SENT)
        later = timezone.now() + timedelta(seconds=5)
        self.post(
            event(
                statuses=[
                    status("wamid.fictional-1", "delivered"),
                    status("wamid.fictional-1", "read", at=later),
                    status("wamid.fictional-1", "played", at=later),
                    status("wamid.fictional-1", "sent"),
                    status("wamid.another-app", "failed"),
                ]
            )
        )
        reply = Communication.objects.get(direction="outbound")
        self.assertEqual(
            (reply.external_id, reply.delivery_status, reply.summary),
            ("wamid.fictional-1", "read", "Yes, at 10."),
        )

    def test_a_client_known_only_by_username_waits_for_matching(self):
        message = text(wamid="wamid.in-9")
        del message["from"]
        message["from_user_id"] = "US.10000000000000000001"
        profile = {"user_id": "US.10000000000000000001", "profile": {"username": "@ana.example"}}
        self.post(event(messages=[message], contacts=[profile]))
        stored = Communication.objects.get()
        self.assertEqual(stored.counterpart, "WhatsApp user ana.example")
        self.assertIsNone(stored.contact)
        self.assertEqual(stored.counterpart_key, "")
        # A delivery update without the number keeps the stored one.
        self.assertFalse(can_text_freely(self.connection, self.contact))
        Communication.objects.create(
            channel="text",
            direction="outbound",
            occurred_at=timezone.now(),
            counterpart=CLIENT_NUMBER,
            summary="Hello",
            contact=self.contact,
            source_connection=self.connection,
            external_id="wamid.out-1",
            delivery_status="sent",
        )
        self.post(event(statuses=[status("wamid.out-1", "delivered", recipient="")]))
        update = Communication.objects.get(external_id="wamid.out-1")
        self.assertEqual((update.counterpart, update.delivery_status), (CLIENT_NUMBER, "delivered"))

    def test_stop_opts_the_client_out(self):
        self.post(event(messages=[text("STOP")]))
        self.contact.refresh_from_db()
        self.assertIsNotNone(self.contact.texting_opted_out_at)
        template = next(
            t for t in self.connection.connector().message_templates() if t.name == "case_update"
        )
        blocked = send_template(
            self.connection,
            contact=self.contact,
            template=template,
            values={"body_first_name": "Ana"},
            actor=self.admin,
        )
        self.assertEqual(blocked.status, OutboundRequest.Status.BLOCKED)
        self.assertEqual(self.meta.sent, [])

    def test_templates_from_the_contact_page(self):
        page = self.client.get(reverse("contact_detail", args=[self.contact.pk]))
        self.assertContains(page, "Client WhatsApp allows free-form messages only within 24 hours")
        query = {"contact": self.contact.pk, "connection": self.connection.pk}
        page = self.client.get(reverse("communication_send_template"), query)
        for name in ("appointment_reminder", "case_update", "office_hours"):
            self.assertContains(page, name)
        self.assertNotContains(page, "new_service")
        self.assertContains(page, "Appointment on [Value 1 in the header]")
        self.assertContains(page, "For example: Tuesday at 10 am")
        listed = len(self.meta.requests)
        response = self.client.post(
            reverse("communication_send_template"),
            {
                **query,
                "template": "appointment_reminder:en_US",
                "value-header_1": "Tuesday",
                "value-body_1": "Ana",
                "value-body_2": "Ms. Example",
                "value-body_3": "Tuesday, Nov 3 at 10 am",
            },
            follow=True,
        )
        self.assertContains(response, "Message sent through Client WhatsApp.")
        # The list was kept from the page just shown, so only the message was sent.
        self.assertEqual(len(self.meta.requests), listed + 1)
        self.assertEqual(
            self.meta.sent[0]["template"]["components"][1]["parameters"][2],
            {"type": "text", "text": "Tuesday, Nov 3 at 10 am"},
        )
        sent = Communication.objects.get()
        self.assertEqual(
            sent.summary,
            "Appointment on Tuesday\n\nHello Ana, your appointment with Ms. Example is on "
            "Tuesday, Nov 3 at 10 am. Reply here with any questions.\n\nExample Law Office"
            "\n\n[Confirm]",
        )
        self.assertNotIn("Ana", OutboundRequest.objects.get().summary)

    def test_signatures_cover_the_raw_body(self):
        connector = self.connection.connector()
        body, headers = signed(event(messages=[text()]))
        self.assertTrue(connector.verify_webhook(body, headers, APP_SECRET))
        upper = {SIGNATURE_HEADER: headers[SIGNATURE_HEADER].upper().replace("SHA256=", "sha256=")}
        self.assertTrue(connector.verify_webhook(body, upper, APP_SECRET))
        self.assertFalse(connector.verify_webhook(body, {SIGNATURE_HEADER: "abc"}, APP_SECRET))
        self.assertFalse(connector.verify_webhook(body, headers, ""))
        self.assertEqual(self.post(event(messages=[text()]), secret="wrong").status_code, 401)
        for payload in (b"not json", json.dumps({"object": "page"}).encode()):
            digest = hmac.new(APP_SECRET.encode(), payload, hashlib.sha256).hexdigest()
            response = self.client.post(
                self.url,
                payload,
                content_type="application/json",
                headers={SIGNATURE_HEADER: f"sha256={digest}"},
            )
            self.assertEqual(response.status_code, 400)
        self.assertFalse(WebhookEvent.objects.exists())
