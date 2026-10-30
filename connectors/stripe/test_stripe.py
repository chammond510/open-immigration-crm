import json
import time
from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qs

import httpx
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from crm.billing.links import create_links
from crm.billing.models import Invoice, InvoiceLine, Payment, PaymentLink
from crm.billing.services import add_line, issue_invoice
from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, WebhookEvent
from crm.connect.services import acknowledge_data_flows, disconnect, enable_webhooks
from crm.connect.tasks import process_webhook_event
from crm.connect.testing import ConnectorContract
from crm.tests.factories import make_contact, make_matter, make_user

from . import API_VERSION, SIGNATURE_HEADER, StripeConnector, major_units, minor_units, sign

KEY = "rk_test_fictional"
WEBHOOK_SECRET = "whsec_fictional"


def form(request):
    return {k: v if len(v) > 1 else v[0] for k, v in parse_qs(request.content.decode()).items()}


class FakeStripe:
    def __init__(self):
        self.requests = []
        self.links = 0
        self.endpoints = 0

    def __call__(self, request):
        self.requests.append(request)
        if request.headers.get("Authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"error": {"type": "invalid_request_error"}})
        assert request.headers["Stripe-Version"] == API_VERSION
        path = request.url.path
        if request.method == "POST":
            assert request.headers.get("Idempotency-Key")
        if path == "/v1/payment_links" and request.method == "GET":
            return httpx.Response(200, json={"object": "list", "data": []})
        if path == "/v1/payment_links":
            self.links += 1
            return httpx.Response(
                200,
                json={
                    "id": f"plink_{self.links}",
                    "url": f"https://buy.stripe.com/test_{self.links}",
                },
            )
        if path.startswith("/v1/payment_links/"):
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[1]})
        if path == "/v1/webhook_endpoints":
            self.endpoints += 1
            return httpx.Response(
                200, json={"id": f"we_{self.endpoints}", "secret": WEBHOOK_SECRET}
            )
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(404, json={"error": {"message": "No such thing"}})

    def calls(self, method, path):
        return [r for r in self.requests if r.method == method and r.url.path.startswith(path)]


class FakeStripeMixin:
    def setUp(self):
        super().setUp()
        self.stripe = FakeStripe()
        transport = http.use_transport(httpx.MockTransport(lambda r: self.stripe(r)))
        transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)
        patcher = mock.patch.object(http, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)


def event(kind, found, event_id="evt_1"):
    return {"id": event_id, "type": kind, "created": 1793390000, "data": {"object": found}}


def session(invoice, account="operating", *, amount=50000, paid=True, link="plink_1"):
    return {
        "id": "cs_1",
        "object": "checkout.session",
        "payment_status": "paid" if paid else "unpaid",
        "payment_link": link,
        "payment_intent": "pi_1",
        "amount_total": amount,
        "currency": "usd",
        "customer_details": {"email": "lena@example.test"},
        "metadata": {"crm_invoice": invoice, "crm_deposit_account": account},
    }


def signed(payload, secret=WEBHOOK_SECRET, at=None):
    body = json.dumps(payload).encode()
    return body, {SIGNATURE_HEADER: sign(body, secret, int(at or time.time()))}


class StripeContractTests(FakeStripeMixin, ConnectorContract, TestCase):
    connector_class = StripeConnector
    valid_credentials = {"api_key": KEY}
    invalid_credentials = {"api_key": "rk_test_revoked"}
    outbound_examples = {
        "create_payment_link": {
            "invoice_number": "INV-0001",
            "amount": "125.00",
            "currency": "USD",
            "deposit_account": "operating",
            "email": "lena@example.test",
            "name": "Lena Fictional",
        }
    }

    def make_webhook(self, secret):
        return signed(event("checkout.session.completed", session("INV-0001")), secret)


class StripeTestCase(FakeStripeMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user()
        self.contact = make_contact(self.admin, first_name="Lena", last_name="Fictional")
        self.connection = self.connect("Stripe operating", "operating")
        self.invoice = Invoice.objects.create(
            contact=self.contact, matter=make_matter(self.contact, self.admin)
        )
        add_line(
            self.invoice,
            description="Fictional advance",
            unit_price=Decimal("1500"),
            funds=InvoiceLine.Funds.ADVANCE,
        )
        add_line(self.invoice, description="Fictional fee", unit_price=Decimal("500"))
        self.invoice = issue_invoice(self.invoice, self.admin)
        self.client.force_login(self.admin)

    def connect(self, label, account):
        connection = Connection.objects.create(
            connector_key="stripe",
            label=label,
            enabled_capabilities=["payments"],
            credentials={"api_key": KEY},
            config={"deposit_account": account},
            preview_mode=False,
        )
        acknowledge_data_flows(connection, self.admin)
        return connection

    def links(self, connection):
        return self.client.post(
            reverse("payment_links_create", args=[self.invoice.pk]),
            {"connection": connection.pk},
            follow=True,
        )


class PaymentLinkTests(StripeTestCase):
    def test_a_stripe_account_makes_links_only_for_its_own_account(self):
        response = self.links(self.connection)
        self.assertContains(response, "Created a payment link")
        self.assertContains(response, "cannot take payments into the trust account")
        link = PaymentLink.objects.get()
        self.assertEqual((link.deposit_account, link.amount), ("operating", Decimal("500.00")))
        self.assertEqual(
            link.url, "https://buy.stripe.com/test_1?prefilled_email=lena%40example.test"
        )
        sent = form(self.stripe.calls("POST", "/v1/payment_links")[0])
        self.assertEqual(
            {k: sent[k] for k in sent if "price_data" in k or "metadata" in k},
            {
                "line_items[0][price_data][currency]": "usd",
                "line_items[0][price_data][unit_amount]": "50000",
                "line_items[0][price_data][product_data][name]": f"Invoice {self.invoice.number}",
                "metadata[crm_invoice]": self.invoice.number,
                "metadata[crm_deposit_account]": "operating",
                "payment_intent_data[metadata][crm_invoice]": self.invoice.number,
                "payment_intent_data[metadata][crm_deposit_account]": "operating",
            },
        )
        self.assertNotIn("payment_method_types[]", sent)

        # A second Stripe account, set to trust, makes the trust link.
        trust = self.connect("Stripe trust", "trust")
        self.links(trust)
        self.assertEqual(
            PaymentLink.objects.get(deposit_account="trust").amount, Decimal("1500.00")
        )
        again = self.links(trust)
        self.assertContains(again, "Links for what is owed already exist")

    def test_nothing_owed_into_the_connections_account(self):
        Payment.objects.create(
            invoice=self.invoice,
            amount=Decimal("500.00"),
            method="card",
            deposit_account="operating",
        )
        with self.assertRaisesMessage(
            ValidationError, "takes payments into the operating account only"
        ):
            create_links(self.connection, self.invoice, self.admin)
        with self.assertRaisesMessage(ConnectorError, "does not pay into the trust account"):
            StripeConnector(self.connection).perform(
                "create_payment_link",
                {"invoice_number": "INV-1", "amount": "1", "deposit_account": "trust"},
            )

    def test_keys(self):
        for key, message in (
            ("sk_live_fictional", "not the account's secret key"),
            ("pk_live_fictional", "starts with rk_live_"),
            ("rk_test_revoked", "did not accept the API key"),
        ):
            self.connection.credentials = {"api_key": key}
            with self.assertRaisesMessage(ConnectorError, message):
                StripeConnector(self.connection).test_connection()

    def test_amounts_in_minor_units(self):
        self.assertEqual(minor_units("125.50", "usd"), 12550)
        self.assertEqual(minor_units("500", "JPY"), 500)
        self.assertEqual(major_units(12550, "usd"), "125.50")
        self.assertEqual(major_units(500, "jpy"), "500.00")


class WebhookTests(StripeTestCase):
    def turn_on(self):
        response = self.client.post(
            reverse("connection_webhooks", args=[self.connection.pk]), {"register": "on"}
        )
        self.url = response.context["webhook_url"]
        self.connection.refresh_from_db()
        return response

    def deliver(self, payload, secret=WEBHOOK_SECRET):
        body, headers = signed(payload, secret)
        with self.captureOnCommitCallbacks(execute=True):
            answer = self.client.post(
                self.url, body, content_type="application/json", headers=headers
            )
        for queued in WebhookEvent.objects.filter(status=WebhookEvent.Status.QUEUED):
            process_webhook_event.call(queued.pk)
        return answer

    def test_the_endpoint_is_made_in_stripe_and_its_secret_kept(self):
        page = self.client.get(reverse("connection_detail", args=[self.connection.pk]))
        self.assertContains(page, "Signing secret from the provider")
        response = self.turn_on()
        self.assertContains(response, "signed with its own secret")
        self.assertEqual(self.connection.webhook_secret, WEBHOOK_SECRET)
        made = form(self.stripe.calls("POST", "/v1/webhook_endpoints")[0])
        self.assertEqual(
            made["enabled_events[]"],
            [
                "checkout.session.completed",
                "checkout.session.async_payment_succeeded",
                "checkout.session.async_payment_failed",
                "charge.refunded",
            ],
        )
        self.assertEqual((made["url"], made["api_version"]), (self.url, API_VERSION))
        self.turn_on()
        self.assertEqual(
            self.stripe.calls("DELETE", "/v1/webhook_endpoints")[0].url.path,
            "/v1/webhook_endpoints/we_1",
        )
        disconnect(self.connection, self.admin)
        self.assertEqual(
            self.stripe.calls("DELETE", "/v1/webhook_endpoints")[-1].url.path,
            "/v1/webhook_endpoints/we_2",
        )
        # Set up by hand, the secret is pasted; without either it is refused.
        with self.assertRaisesMessage(ValidationError, "Paste the signing secret"):
            enable_webhooks(self.connection, self.admin)
        enable_webhooks(self.connection, self.admin, "whsec_pasted")
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.webhook_secret, "whsec_pasted")

    def test_signatures(self):
        connector = StripeConnector(self.connection)
        body, headers = signed(event("charge.refunded", {}))
        self.assertTrue(connector.verify_webhook(body, headers, WEBHOOK_SECRET))
        rolled = {SIGNATURE_HEADER: f"{headers[SIGNATURE_HEADER]},v1=other,v0=fake"}
        self.assertTrue(connector.verify_webhook(body, rolled, WEBHOOK_SECRET))
        self.assertFalse(connector.verify_webhook(body + b" ", headers, WEBHOOK_SECRET))
        self.assertFalse(connector.verify_webhook(body, headers, "whsec_other"))
        self.assertFalse(connector.verify_webhook(body, headers, ""))
        stale = signed(event("charge.refunded", {}), at=time.time() - 400)
        self.assertFalse(connector.verify_webhook(*stale, WEBHOOK_SECRET))
        for header in ("", "t=abc,v1=x", "v1=x", "t=1"):
            self.assertFalse(
                connector.verify_webhook(body, {SIGNATURE_HEADER: header}, WEBHOOK_SECRET)
            )

    def test_card_payments_bank_payments_and_refunds(self):
        self.turn_on()
        number = self.invoice.number
        # The firm's other Stripe payments are not recorded or kept.
        other = session("")
        other["metadata"] = {}
        self.deliver(event("checkout.session.completed", other, "evt_0"))
        self.assertEqual(WebhookEvent.objects.get(event_id="evt_0").payload, None)
        self.assertFalse(Payment.objects.exists())

        self.deliver(event("checkout.session.completed", session(number), "evt_1"))
        payment = Payment.objects.get()
        self.assertEqual(
            (payment.amount, payment.deposit_account, payment.method, payment.external_id),
            (Decimal("500.00"), "operating", "card", "pi_1"),
        )
        deactivated = form(self.stripe.calls("POST", "/v1/payment_links/plink_1")[0])
        self.assertEqual(deactivated, {"active": "false"})

        refund = {
            "id": "ch_1",
            "payment_intent": "pi_1",
            "amount": 50000,
            "amount_refunded": 10000,
            "refunded": False,
            "currency": "usd",
            "metadata": {"crm_invoice": number, "crm_deposit_account": "operating"},
        }
        self.deliver(event("charge.refunded", refund, "evt_2"))
        payment.refresh_from_db()
        self.assertIsNone(payment.voided_at)
        self.deliver(
            event(
                "charge.refunded", {**refund, "amount_refunded": 50000, "refunded": True}, "evt_3"
            )
        )
        payment.refresh_from_db()
        self.assertIsNotNone(payment.voided_at)

    def test_a_bank_payment_is_recorded_when_it_clears(self):
        self.turn_on()
        number = self.invoice.number
        pending = session(number, paid=False, link="plink_7")
        pending["payment_intent"] = "pi_7"
        self.deliver(event("checkout.session.completed", pending, "evt_1"))
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(
            form(self.stripe.calls("POST", "/v1/payment_links/plink_7")[-1]), {"active": "false"}
        )
        self.deliver(event("checkout.session.async_payment_failed", pending, "evt_2"))
        self.assertEqual(
            form(self.stripe.calls("POST", "/v1/payment_links/plink_7")[-1]), {"active": "true"}
        )
        self.assertFalse(Payment.objects.filter(voided_at__isnull=True).exists())
        self.deliver(
            event(
                "checkout.session.async_payment_succeeded",
                {**pending, "payment_status": "paid"},
                "evt_3",
            )
        )
        payment = Payment.objects.get(voided_at__isnull=True)
        self.assertEqual((payment.method, payment.amount), ("bank_transfer", Decimal("500.00")))

    def test_refused_and_odd_events(self):
        self.turn_on()
        answer = self.deliver(
            event("checkout.session.completed", session(self.invoice.number)), "whsec_wrong"
        )
        self.assertEqual(answer.status_code, 401)
        connector = StripeConnector(self.connection)
        for body in (b"not json", b"[1]", json.dumps({"id": "nope"}).encode()):
            with self.assertRaises(ConnectorError):
                connector.parse_webhook(body, {})
        self.assertEqual(connector.handle_webhook("payments", {"event": "ignored"}), [])
