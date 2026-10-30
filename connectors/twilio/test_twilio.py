from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlencode

import httpx
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.comms.models import Communication
from crm.comms.texting import send_text
from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, OutboundRequest, SyncRun, WebhookEvent
from crm.connect.services import acknowledge_data_flows, create_connection
from crm.connect.tasks import process_webhook_event, run_sync
from crm.connect.testing import ConnectorContract
from crm.tests.factories import make_contact, make_user

from . import EMPTY_TWIML, SIGNATURE_HEADER, TwilioConnector, signature, validate_sender

ACCOUNT = "AC" + "0" * 31 + "1"
TOKEN = "fictional-auth-token"
SERVICE = "MG" + "0" * 31 + "2"
FIRM_NUMBER = "+12025550143"
CLIENT_NUMBER = "+12025550144"
OTHER_LINE = "+12025550160"
STAFF_CELL = "+12025550150"
CREDENTIALS = {"account_sid": ACCOUNT, "auth_token": TOKEN}


class FakeTwilio:
    """Answers like Twilio's REST API for one fictional account and number."""

    def __init__(self):
        self.requests = []
        self.unsubscribed = set()
        self.numbers = {FIRM_NUMBER: "PN" + "0" * 31 + "3", OTHER_LINE: "PN" + "0" * 31 + "4"}
        self.sent = 0
        self.calls = []
        self.recordings = []

    def __call__(self, request):
        self.requests.append(request)
        auth = request.headers.get("Authorization", "")
        if auth != httpx.BasicAuth(ACCOUNT, TOKEN)._auth_header:
            return httpx.Response(401, json={"code": 20003, "message": "Authenticate"})
        path = request.url.path
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if path.endswith("/IncomingPhoneNumbers.json"):
            number = request.url.params.get("PhoneNumber")
            listed = [{"sid": sid, "phone_number": n} for n, sid in self.numbers.items()]
            found = [n for n in listed if not number or n["phone_number"] == number]
            return httpx.Response(200, json={"incoming_phone_numbers": found})
        if path.endswith("/Calls.json"):
            # Two pages, to follow next_page_uri.
            if request.url.params.get("PageToken"):
                return httpx.Response(200, json={"calls": self.calls[1:], "next_page_uri": None})
            following = f"/2010-04-01/Accounts/{ACCOUNT}/Calls.json?PageSize=1&PageToken=PA1"
            return httpx.Response(
                200,
                json={
                    "calls": self.calls[:1],
                    "next_page_uri": following if len(self.calls) > 1 else None,
                },
            )
        if path.endswith("/Recordings.json"):
            return httpx.Response(200, json={"recordings": self.recordings})
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


def leg(sid, direction, sender, to, *, status="completed", duration="125", parent=None):
    return {
        "sid": sid.ljust(34, "0"),
        "parent_call_sid": parent.ljust(34, "0") if parent else None,
        "direction": direction,
        "from": sender,
        "to": to,
        "status": status,
        "duration": duration if status == "completed" else None,
        "start_time": "Fri, 30 Oct 2026 15:00:00 +0000",
        "date_created": "Fri, 30 Oct 2026 14:59:58 +0000",
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
    sync_examples = {"phone": {}}
    webhook_secret = TOKEN

    def setUp(self):
        super().setUp()
        self.twilio.calls = [leg("CA1", "inbound", CLIENT_NUMBER, FIRM_NUMBER)]

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


class TwilioCallTests(FakeTwilioMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user()
        self.contact = make_contact(self.admin, phone="202-555-0144")
        self.connection = create_connection(
            "twilio",
            label="Firm phone",
            created_by=self.admin,
            capabilities=["phone"],
            credentials=dict(CREDENTIALS),
        )
        acknowledge_data_flows(self.connection, self.admin)
        self.twilio.calls = [
            # Answered on a staff phone: the inbound parent and the forwarding child.
            leg("CA1", "inbound", CLIENT_NUMBER, FIRM_NUMBER),
            leg("CA1B", "outbound-dial", FIRM_NUMBER, STAFF_CELL, parent="CA1"),
            # Rang through to the staff phone, which did not answer.
            leg("CA2", "inbound", CLIENT_NUMBER, FIRM_NUMBER, duration="20"),
            leg("CA2B", "outbound-dial", FIRM_NUMBER, STAFF_CELL, status="no-answer", parent="CA2"),
            # Placed from a browser: the outside party is only on the child leg.
            leg("CA3", "inbound", "client:associate", ""),
            leg("CA3B", "outbound-dial", FIRM_NUMBER, CLIENT_NUMBER, parent="CA3"),
            # Placed through the API, unanswered, on another of the firm's numbers.
            leg("CA4", "outbound-api", OTHER_LINE, "+12025550170", status="no-answer"),
            # Still going: filed by a later sync.
            leg("CA5", "inbound", CLIENT_NUMBER, FIRM_NUMBER, status="in-progress"),
        ]
        self.twilio.recordings = [
            {"sid": "RE" + "1" * 32, "call_sid": "CA1".ljust(34, "0"), "duration": "151"}
        ]

    def sync(self):
        run = SyncRun.objects.create(connection=self.connection, capability="phone")
        run_sync.call(run.pk)
        run.refresh_from_db()
        return run

    def test_each_call_is_filed_once_from_its_legs(self):
        run = self.sync()
        self.assertEqual(run.status, SyncRun.Status.SUCCEEDED, run.error)
        calls = {c.external_id[:4].rstrip("0"): c for c in Communication.objects.all()}
        self.assertEqual(set(calls), {"CA1", "CA2", "CA3", "CA4"})
        answered, missed, browser, api = calls["CA1"], calls["CA2"], calls["CA3"], calls["CA4"]
        self.assertEqual(
            (answered.contact, answered.direction, answered.duration.total_seconds()),
            (self.contact, "inbound", 125),
        )
        self.assertIn("Twilio recording RE1", answered.summary)
        self.assertIn("(2:31)", answered.summary)
        self.assertEqual((missed.contact, missed.direction), (self.contact, "inbound"))
        self.assertEqual((missed.subject, answered.subject), ("Call missed", ""))
        self.assertEqual((browser.contact, browser.direction), (self.contact, "outbound"))
        self.assertEqual(
            (api.counterpart, api.direction, api.subject),
            ("+12025550170", "outbound", "Call no answer"),
        )
        self.assertEqual(run.cursor_after, {"since": "Fri, 30 Oct 2026 15:00:00 +0000"})
        params = [r.url.params for r in self.twilio.requests if r.url.path.endswith("/Calls.json")]
        first_day = (timezone.now() - timedelta(days=7, hours=2)).strftime("%Y-%m-%d")
        self.assertEqual(params[0]["StartTime>"], first_day)
        self.assertTrue(params[1].get("PageToken"))
        # Later syncs go back over the last two hours from where the last one stopped.
        self.twilio.requests = []
        self.sync()
        later = next(r for r in self.twilio.requests if r.url.path.endswith("/Calls.json"))
        self.assertEqual(later.url.params["StartTime>"], "2026-10-30")

        # The call that was still going is filed once it has ended.
        self.twilio.calls[-1] = leg("CA5", "inbound", CLIENT_NUMBER, FIRM_NUMBER)
        self.sync()
        self.assertEqual(Communication.objects.count(), 5)

    def test_only_the_chosen_numbers(self):
        self.connection.config = {"call_numbers": [OTHER_LINE]}
        self.connection.save()
        self.sync()
        self.assertEqual([c.external_id[:3] for c in Communication.objects.all()], ["CA4"])
        self.client.force_login(self.admin)
        detail = self.client.get(reverse("connection_detail", args=[self.connection.pk]))
        self.assertContains(detail, "Log calls on these numbers")
        self.assertContains(detail, OTHER_LINE)

    def test_calls_alone_need_no_sender(self):
        TwilioConnector(self.connection).test_connection()
        texting = connect(self.admin, sender=FIRM_NUMBER)
        texting.config = {}
        with self.assertRaisesMessage(ConnectorError, "number or Messaging Service to send texts"):
            TwilioConnector(texting).test_connection()
        with self.assertRaisesMessage(ConnectorError, "number or Messaging Service to send texts"):
            TwilioConnector(texting).perform("send_text", {"to": CLIENT_NUMBER, "body": "x"})
