import base64
import hashlib
import hmac
import json
import urllib.error
from unittest import mock

from django.test import TestCase

from crm.connect.base import ConnectorError
from crm.connect.testing import ConnectorContract

from . import SECRET_PREFIX, WebhooksConnector, check_address, new_secret, sign

FICTIONAL_SECRET = new_secret()


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeReceiver:
    """Stands in for the HTTP opener and keeps every request it is sent."""

    def __init__(self):
        self.requests = []
        self.status = 204
        self.error = None

    def open(self, request, timeout):
        self.requests.append(request)
        if self.error:
            raise self.error
        return FakeResponse(self.status)


class WebhooksContractTests(ConnectorContract, TestCase):
    connector_class = WebhooksConnector
    valid_credentials = {
        "url": "https://hooks.example.test/fictional",
        "signing_secret": FICTIONAL_SECRET,
    }
    invalid_credentials = {
        "url": "http://hooks.example.test/fictional",
        "signing_secret": FICTIONAL_SECRET,
    }
    outbound_examples = {
        "deliver_event": {
            "id": "00000000-0000-0000-0000-000000000001",
            "event": {"type": "contact.created", "timestamp": "2026-03-02T09:00:00+00:00"},
        }
    }

    def setUp(self):
        super().setUp()
        self.receiver = FakeReceiver()
        patcher = mock.patch("connectors.webhooks.opener", self.receiver)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_standard_webhooks_reference_signature(self):
        # The published test vector from the Standard Webhooks specification.
        secret = SECRET_PREFIX + "MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
        self.assertEqual(
            sign(secret, "msg_p5jXN8AQM9LWM0D4loKWxJek", "1614265330", b'{"test": 2432232314}'),
            "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE=",
        )
        self.assertEqual(
            sign("plain-fictional-secret", "msg_1", "1", b"{}"),
            "v1,"
            + base64.b64encode(
                hmac.new(b"plain-fictional-secret", b"msg_1.1.{}", hashlib.sha256).digest()
            ).decode(),
        )

    def test_deliveries_are_signed_json_posts(self):
        connector = WebhooksConnector(self.make_connection())
        reference = connector.perform("deliver_event", self.outbound_examples["deliver_event"])
        self.assertEqual(reference, "HTTP 204")
        request = self.receiver.requests[0]
        headers = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["webhook-id"], "msg_00000000-0000-0000-0000-000000000001")
        expected = sign(
            FICTIONAL_SECRET, headers["webhook-id"], headers["webhook-timestamp"], request.data
        )
        self.assertEqual(headers["webhook-signature"], expected)
        self.assertEqual(json.loads(request.data)["type"], "contact.created")

    def test_connection_test_sends_a_test_event(self):
        WebhooksConnector(self.make_connection()).test_connection()
        event = json.loads(self.receiver.requests[0].data)
        self.assertEqual(event["type"], "webhook.test")
        self.assertTrue(self.receiver.requests[0].get_header("Webhook-id").startswith("msg_test_"))

    def test_receiver_errors_become_safe_messages(self):
        connector = WebhooksConnector(self.make_connection())
        payload = self.outbound_examples["deliver_event"]
        for error, message in (
            (urllib.error.HTTPError("u", 500, "x", {}, None), "answered HTTP 500"),
            (urllib.error.HTTPError("u", 302, "x", {}, None), "do not follow redirects"),
            (urllib.error.URLError("refused"), "could not be reached"),
            (TimeoutError(), "could not be reached"),
        ):
            self.receiver.error = error
            with self.assertRaisesMessage(ConnectorError, message):
                connector.perform("deliver_event", payload)
        with self.assertRaises(ConnectorError):
            connector.perform("send_text", payload)

    def test_addresses(self):
        for url in (
            "https://hooks.example.test/x",
            "http://n8n:5678/webhook/x",
            "http://localhost:5678/x",
            "http://10.0.0.5/x",
            "http://automation.lan/x",
        ):
            check_address(url)
        for url in ("http://hooks.example.test/x", "ftp://n8n/x", "https://", "not a url"):
            with self.assertRaises(ConnectorError):
                check_address(url)

    def test_new_secrets_are_random_standard_secrets(self):
        first, second = new_secret(), new_secret()
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith(SECRET_PREFIX))
        self.assertEqual(len(base64.b64decode(first[len(SECRET_PREFIX) :])), 32)
