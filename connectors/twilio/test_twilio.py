from unittest import mock
from urllib.parse import parse_qs, urlencode

import httpx
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from crm.comms.models import Communication
from crm.comms.texting import send_text
from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, OutboundRequest, WebhookEvent
from crm.connect.services import acknowledge_data_flows, create_connection
from crm.connect.tasks import process_webhook_event
from crm.connect.testing import ConnectorContract
from crm.tests.factories import make_contact, make_user

from . import EMPTY_TWIML, SIGNATURE_HEADER, TwilioConnector, signature, validate_sender

ACCOUNT = "AC" + "0" * 31 + "1"
TOKEN = "fictional-auth-token"
SERVICE = "MG" + "0" * 31 + "2"
FIRM_NUMBER = "+12025550143"
CLIENT_NUMBER = "+12025550144"
CREDENTIALS = {"account_sid": ACCOUNT, "auth_token": TOKEN}


class FakeTwilio:
    """Answers like Twilio's REST API for one fictional account and number."""

    def __init__(self):
        self.requests = []
        self.unsubscribed = set()
        self.numbers = {FIRM_NUMBER: "PN" + "0" * 31 + "3"}
        self.sent = 0

    def __call__(self, request):
        self.requests.append(request)
        auth = request.headers.get("Authorization", "")
        if auth != httpx.BasicAuth(ACCOUNT, TOKEN)._auth_header:
            return httpx.Response(401, json={"code": 20003, "message": "Authenticate"})
        path = request.url.path
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if path.endswith("/IncomingPhoneNumbers.json"):
            number = request.url.params.get("PhoneNumber")
            found = [{"sid": self.numbers[number]}] if number in self.numbers else []
            return httpx.Response(200, json={"incoming_phone_numbers": found})
        if path.endswith("/Messages.json"):
            if form["To"] in self.unsubscribed:
                message = f"Attempt to send to unsubscribed recipient {form['To']}"
                return httpx.Response(400, json={"code": 21610, "message": message})
            self.sent += 1
            return httpx.Response(201, json={"sid": f"SM{self.sent:032d}", "status": "queued"})
        return httpx.Response(200, json={"sid": path.rsplit("/", 1)[-1]})

    def last_form(self):
        return {k: v[0] for k, v in parse_qs(self.requests[-1].content.decode()).items()}


class FakeTwilioMixin:
    def setUp(self):
        super().setUp()
        self.twilio = FakeTwilio()
        transport = http.use_transport(httpx.MockTransport(lambda r: self.twilio(r)))
        transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)
        patcher = mock.patch.object(http, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)


def signed(url, params, token=TOKEN):
    body = urlencode(params).encode()
    return body, {SIGNATURE_HEADER: signature(url, parse_qs(body.decode()), token)}


def inbound(body="Thank you", sid="SM" + "9" * 32, **extra):
    return {
        "MessageSid": sid,
        "SmsSid": sid,
        "AccountSid": ACCOUNT,
        "From": CLIENT_NUMBER,
        "To": FIRM_NUMBER,
        "Body": body,
        "SmsStatus": "received",
        "NumMedia": "0",
        **extra,
    }


def connect(admin, sender=FIRM_NUMBER):
    connection = create_connection(
        "twilio",
        label="Front desk texts",
        created_by=admin,
        capabilities=["texting"],
        credentials=dict(CREDENTIALS),
        config={"sender": sender},
    )
    acknowledge_data_flows(connection, admin)
    Connection.objects.filter(pk=connection.pk).update(preview_mode=False)
    connection.refresh_from_db()
    return connection


class TwilioContractTests(FakeTwilioMixin, ConnectorContract, TestCase):
    connector_class = TwilioConnector
    valid_credentials = CREDENTIALS
    invalid_credentials = {**CREDENTIALS, "auth_token": "wrong"}
    connection_config = {"sender": FIRM_NUMBER}
    outbound_examples = {"send_text": {"to": CLIENT_NUMBER, "body": "Fictional reminder"}}
    webhook_secret = TOKEN

    def make_webhook(self, secret):
        return signed(self.webhook_url, inbound(), secret)


class TwilioConnectorTests(FakeTwilioMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user()
        self.contact = make_contact(self.admin, phone="202-555-0144")

    def connect(self, sender=FIRM_NUMBER):
        return connect(self.admin, sender)

    def test_texts_are_sent_from_the_number_or_service(self):
        connection = self.connect()
        request = send_text(
            connection, contact=self.contact, body="Fictional reminder", actor=self.admin
        )
        self.assertEqual(request.status, OutboundRequest.Status.SENT)
        self.assertEqual(
            self.twilio.last_form(),
            {"To": CLIENT_NUMBER, "Body": "Fictional reminder", "From": FIRM_NUMBER},
        )
        self.assertTrue(
            self.twilio.requests[-1].url.path.endswith(f"/Accounts/{ACCOUNT}/Messages.json")
        )
        self.assertEqual(Communication.objects.get().external_id, request.provider_ref)
        service = self.connect(SERVICE)
        send_text(service, contact=self.contact, body="Fictional reminder", actor=self.admin)
        self.assertEqual(self.twilio.last_form()["MessagingServiceSid"], SERVICE)
        self.assertNotIn("From", self.twilio.last_form())

    def test_refusals_are_explained_without_the_number(self):
        self.twilio.unsubscribed.add(CLIENT_NUMBER)
        request = send_text(self.connect(), contact=self.contact, body="Hi", actor=self.admin)
        self.assertEqual(request.status, OutboundRequest.Status.FAILED)
        self.assertIn("replied STOP to the firm's Twilio number", request.error)
        self.assertNotIn("555", request.error)
        self.assertFalse(Communication.objects.exists())

    def test_connection_test_checks_the_account_and_sender(self):
        connection = self.connect()
        TwilioConnector(connection).test_connection()
        self.twilio.numbers.clear()
        with self.assertRaisesMessage(ConnectorError, "not in this Twilio account"):
            TwilioConnector(connection).test_connection()
        TwilioConnector(self.connect(SERVICE)).test_connection()
        self.assertTrue(str(self.twilio.requests[-1].url).endswith(f"/v1/Services/{SERVICE}"))
        connection.credentials = {**CREDENTIALS, "account_sid": "not-a-sid"}
        with self.assertRaisesMessage(ConnectorError, "should start with AC"):
            TwilioConnector(connection).test_connection()

    def test_sender_setting(self):
        for good in (FIRM_NUMBER, SERVICE):
            validate_sender(good)
        for bad in ("202-555-0143", "MG123", "", "+0123"):
            with self.assertRaises(ValidationError):
                validate_sender(bad)

    def test_signatures_cover_the_address_and_every_parameter(self):
        connector = TwilioConnector(self.connect())
        url = "https://crm.example.test/hooks/fictional-token/"
        body, headers = signed(url, inbound())
        self.assertTrue(connector.verify_webhook(body, headers, TOKEN, url=url))
        self.assertFalse(connector.verify_webhook(body, headers, TOKEN, url=url + "x/"))
        tampered = body.replace(b"Thank+you", b"Send+money")
        self.assertFalse(connector.verify_webhook(tampered, headers, TOKEN, url=url))
        self.assertFalse(connector.verify_webhook(b"\xff", headers, TOKEN, url=url))
        other = signed(url, inbound(AccountSid="AC" + "9" * 32))[0]
        with self.assertRaisesMessage(ConnectorError, "different account"):
            connector.parse_webhook(other, {})
        with self.assertRaisesMessage(ConnectorError, "no message ID"):
            connector.parse_webhook(b"AccountSid=" + ACCOUNT.encode(), {})


class TwilioWebhookTests(FakeTwilioMixin, TestCase):
    """Turning webhooks on from the connection page, then Twilio's deliveries end to end."""

    def setUp(self):
        super().setUp()
        self.admin = make_user()
        self.contact = make_contact(self.admin, phone="(202) 555-0144")
        self.connection = connect(self.admin)
        self.client.force_login(self.admin)

    def turn_on(self, register=True):
        response = self.client.post(
            reverse("connection_webhooks", args=[self.connection.pk]),
            {"register": "on"} if register else {},
        )
        self.connection.refresh_from_db()
        return response

    def post(self, params, url=None):
        url = url or self.url
        body, headers = signed(url, params)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                url,
                body,
                content_type="application/x-www-form-urlencoded",
                headers=headers,
            )
        for event in WebhookEvent.objects.filter(status=WebhookEvent.Status.QUEUED):
            process_webhook_event.call(event.pk)
        return response

    def test_registration_points_the_number_here(self):
        response = self.turn_on()
        self.url = response.context["webhook_url"]
        self.assertContains(response, "Set up in Twilio: texts to +12025550143")
        self.assertNotContains(response, TOKEN)
        self.assertEqual(self.twilio.last_form(), {"SmsUrl": self.url, "SmsMethod": "POST"})
        self.assertEqual(self.connection.webhook_secret, TOKEN)
        self.assertEqual(self.connection.credentials["webhook_url"], self.url)

        # Incoming texts are filed on the contact, and Twilio gets empty TwiML back.
        answer = self.post(inbound())
        self.assertEqual(answer["Content-Type"], "text/xml")
        self.assertEqual(answer.content.decode(), EMPTY_TWIML)
        text = Communication.objects.get()
        self.assertEqual(
            (text.contact, text.direction, text.summary), (self.contact, "inbound", "Thank you")
        )

        # STOP opts the contact out.
        self.post(inbound("Stop", sid="SM" + "8" * 32, OptOutType="STOP"))
        self.contact.refresh_from_db()
        self.assertIsNotNone(self.contact.texting_opted_out_at)

        # Texts sent from the CRM ask for delivery updates, which then arrive.
        self.contact.texting_opted_out_at = None
        self.contact.save()
        sent = send_text(self.connection, contact=self.contact, body="Reminder", actor=self.admin)
        self.assertEqual(self.twilio.last_form()["StatusCallback"], self.url)
        update = {
            "MessageSid": sent.provider_ref,
            "AccountSid": ACCOUNT,
            "From": FIRM_NUMBER,
            "To": CLIENT_NUMBER,
            "MessageStatus": "delivered",
            "SmsStatus": "delivered",
        }
        self.post(update)
        self.post({**update, "MessageStatus": "sent", "SmsStatus": "sent"})
        reminder = Communication.objects.get(external_id=sent.provider_ref)
        self.assertEqual((reminder.delivery_status, reminder.summary), ("delivered", "Reminder"))

        # A retried delivery is acknowledged once, and a forged one is refused.
        self.assertEqual(self.post(update).content.decode(), EMPTY_TWIML)
        self.assertEqual(
            WebhookEvent.objects.filter(event_id__startswith=sent.provider_ref).count(), 2
        )
        forged_body, _ = signed(self.url, inbound("Forged"))
        refused = self.client.post(
            self.url,
            forged_body,
            content_type="application/x-www-form-urlencoded",
            headers={SIGNATURE_HEADER: "forged"},
        )
        self.assertEqual(refused.status_code, 401)

    def test_a_messaging_service_is_set_up_too(self):
        self.connection.config = {"sender": SERVICE}
        self.connection.save()
        response = self.turn_on()
        form = self.twilio.last_form()
        self.assertEqual(form["InboundRequestUrl"], response.context["webhook_url"])
        self.assertEqual(form["StatusCallback"], response.context["webhook_url"])

    def test_manual_setup_and_failed_registration(self):
        response = self.turn_on(register=False)
        self.url = response.context["webhook_url"]
        self.assertNotIn("webhook_url", self.connection.credentials)
        self.assertContains(response, "paste this address as the incoming message webhook")
        self.post(inbound())
        self.assertEqual(Communication.objects.count(), 1)
        self.twilio.numbers.clear()
        failed = self.turn_on()
        self.assertContains(failed, "could not be set up automatically")
        self.assertContains(failed, "not in this Twilio account")
