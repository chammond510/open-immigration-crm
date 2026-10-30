import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.billing.links import create_links
from crm.billing.models import Invoice, InvoiceLine, Payment, PaymentLink
from crm.billing.services import add_line, issue_invoice
from crm.connect import http
from crm.connect.models import Connection, OutboundRequest, SyncRun, WebhookEvent
from crm.connect.services import acknowledge_data_flows
from crm.connect.tasks import process_webhook_event, run_sync
from crm.connect.testing import ConnectorContract
from crm.models import AuditLog
from crm.tests.factories import make_contact, make_matter, make_user

from . import LawPayConnector, validate_page

SECRET = "fictional-lawpay-secret-key"
OPERATING_PAGE = "https://secure.lawpay.com/pages/fictional-firm/operating"
TRUST_PAGE = "https://secure.lawpay.com/pages/fictional-firm/trust"


def charge(charge_id, reference, amount_cents, account="acct-operating", **extra):
    return {
        "id": charge_id,
        "type": "CHARGE",
        "status": "COMPLETED",
        "account_id": account,
        "amount": amount_cents,
        "currency": "USD",
        "reference": reference,
        "created": "2026-10-30T15:00:00.000Z",
        "completion_timestamp": "2026-10-30T15:00:05.000Z",
        "method": {"type": "card", "name": "Lena Fictional", "email": "lena@example.test"},
        "amount_refunded": 0,
        **extra,
    }


def event(event_id, data, kind="transaction.completed", created="2026-10-30T15:00:06.000Z"):
    return {"id": event_id, "type": kind, "created": created, "data": data}


class FakeLawPay:
    """Answers like the LawPay (8am) gateway API for one fictional merchant."""

    def __init__(self):
        self.requests = []
        self.events = []
        self.transactions = {}

    def __call__(self, request):
        self.requests.append(request)
        if request.headers.get("Authorization") != httpx.BasicAuth(SECRET, "")._auth_header:
            return httpx.Response(401, json={"messages": [{"code": "not_authorized"}]})
        path = request.url.path.removeprefix("/v1")
        if path == "/merchant":
            return httpx.Response(
                200,
                json={
                    "merchant_accounts": [
                        {"id": "acct-operating", "name": "Operating", "trust_account": False},
                        {"id": "acct-trust", "name": "IOLTA", "trust_account": True},
                    ],
                    "ach_accounts": [{"id": "ach-unknown", "name": "eCheck"}],
                },
            )
        if path == "/events":
            page = int(request.url.params["page"])
            size = int(request.url.params["page_size"])
            chunk = self.events[(page - 1) * size : page * size]
            return httpx.Response(
                200, json={"page": page, "total_entries": len(self.events), "results": chunk}
            )
        if path.startswith("/events/"):
            found = next((e for e in self.events if e["id"] == path.rsplit("/", 1)[-1]), None)
            return httpx.Response(200, json=found) if found else httpx.Response(404)
        if path.startswith("/transactions/"):
            found = self.transactions.get(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=found) if found else httpx.Response(404)
        return httpx.Response(404)


class FakeLawPayMixin:
    def setUp(self):
        super().setUp()
        self.lawpay = FakeLawPay()
        transport = http.use_transport(httpx.MockTransport(lambda r: self.lawpay(r)))
        transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)
        patcher = mock.patch.object(http, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)


class LawPayContractTests(FakeLawPayMixin, ConnectorContract, TestCase):
    connector_class = LawPayConnector
    valid_credentials = {"secret_key": SECRET}
    invalid_credentials = {"secret_key": "wrong"}
    connection_config = {"operating_page": OPERATING_PAGE, "trust_page": TRUST_PAGE}
    sync_examples = {"payments": {"since": "2026-10-30T14:00:00+00:00"}}
    outbound_examples = {
        "create_payment_link": {
            "invoice_number": "INV-2026-0001",
            "amount": "250.00",
            "currency": "USD",
            "deposit_account": "operating",
            "email": "lena@example.test",
            "name": "Lena Fictional",
        }
    }
    signature_in_headers = False
    body_is_signed = False
    webhook_uses_secret = False

    def setUp(self):
        super().setUp()
        self.lawpay.events = [event("evt-1", charge("ch-1", "INV-2026-0001", 25000))]

    def make_webhook(self, secret):
        return json.dumps(self.lawpay.events[0]).encode(), {"Content-Type": "application/json"}


class LawPayTestCase(FakeLawPayMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user()
        self.contact = make_contact(self.admin, first_name="Lena", last_name="Fictional")
        self.connection = Connection.objects.create(
            connector_key="lawpay",
            label="LawPay",
            enabled_capabilities=["payments"],
            credentials={"secret_key": SECRET},
            config={"operating_page": OPERATING_PAGE, "trust_page": TRUST_PAGE},
            preview_mode=False,
        )
        acknowledge_data_flows(self.connection, self.admin)
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

    def sync(self):
        run = SyncRun.objects.create(connection=self.connection, capability="payments")
        run_sync.call(run.pk)
        run.refresh_from_db()
        return run


class PaymentLinkTests(LawPayTestCase):
    def test_one_link_per_account_from_the_invoice_page(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse("invoice_detail", args=[self.invoice.pk]))
        self.assertContains(page, "Create payment links")
        self.assertContains(page, "Advances and earned fees get separate links")
        response = self.client.post(
            reverse("payment_links_create", args=[self.invoice.pk]),
            {"connection": self.connection.pk},
            follow=True,
        )
        self.assertContains(response, "Created a payment link")
        links = {link.deposit_account: link for link in PaymentLink.objects.all()}
        trust = urlsplit(links["trust"].url)
        self.assertEqual(f"https://{trust.netloc}{trust.path}", TRUST_PAGE)
        query = {k: v[0] for k, v in parse_qs(trust.query).items()}
        self.assertEqual(
            query,
            {
                "amount": "1500.00",
                "reference": self.invoice.number,
                "name": "Lena Fictional",
                "email": self.contact.email,
                "readOnlyFields": "amount,reference",
            },
        )
        self.assertTrue(links["operating"].url.startswith(f"{OPERATING_PAGE}?amount=500.00"))
        self.assertContains(response, links["trust"].url.replace("&", "&amp;"))
        self.assertEqual(OutboundRequest.objects.filter(status="sent").count(), 2)
        # Asking again makes no duplicates; a payment leaves the old link marked stale.
        again = self.client.post(
            reverse("payment_links_create", args=[self.invoice.pk]),
            {"connection": self.connection.pk},
            follow=True,
        )
        self.assertContains(again, "Links for what is owed already exist")
        self.assertNotContains(again, "Create payment links")
        self.assertEqual(PaymentLink.objects.count(), 2)
        Payment.objects.create(
            invoice=self.invoice,
            amount=Decimal("500.00"),
            method="card",
            deposit_account="trust",
        )
        page = self.client.get(reverse("invoice_detail", args=[self.invoice.pk]))
        self.assertContains(page, "no longer the amount owed", count=1)
        self.assertContains(page, "Create payment link")

    def test_a_missing_page_or_preview_mode_makes_no_link(self):
        self.connection.config = {"operating_page": OPERATING_PAGE}
        self.connection.save()
        requests = create_links(self.connection, self.invoice, self.admin)
        statuses = sorted((r.status, r.error) for r in requests)
        self.assertEqual(statuses[0][0], "failed")
        self.assertIn("trust account's LawPay payment page", statuses[0][1])
        self.assertEqual(PaymentLink.objects.get().deposit_account, "operating")
        Connection.objects.filter(pk=self.connection.pk).update(preview_mode=True)
        self.connection.refresh_from_db()
        requests = create_links(self.connection, self.invoice, self.admin)
        self.assertEqual({r.status for r in requests}, {"previewed"})
        self.assertEqual(PaymentLink.objects.count(), 1)

    def test_payment_page_addresses(self):
        for good in (TRUST_PAGE, "https://secure.affinipay.com/pages/fictional/operating"):
            validate_page(good)
        for bad in ("http://secure.lawpay.com/pages/x", "https://lawpay.com.example.test/pages/x"):
            with self.assertRaises(ValidationError):
                validate_page(bad)


class PaymentSyncTests(LawPayTestCase):
    def test_payments_are_polled_and_recorded_to_the_right_account(self):
        first = self.sync()
        self.assertEqual(first.records_received, 0)
        self.assertFalse(self.lawpay.requests[1:])  # The first poll only marks the start.
        number = self.invoice.number
        self.lawpay.events = [
            event("e1", charge("ch-trust", number, 150000, account="acct-trust")),
            event("e2", charge("ch-op", number, 50000)),
            event("e3", charge("ch-fail", number, 1000, status="FAILED")),
            event(
                "e4",
                {"type": "CHARGE", "status": "AUTHORIZED", "id": "ch-auth"},
                "transaction.authorized",
            ),
        ]
        with mock.patch(
            "crm.connect.tasks.timezone.now", return_value=timezone.now() + timedelta(minutes=10)
        ):
            run = self.sync()
        self.assertEqual(run.status, SyncRun.Status.SUCCEEDED, run.error)
        payments = {p.external_id: p for p in Payment.objects.all()}
        self.assertEqual(set(payments), {"ch-trust", "ch-op"})
        self.assertEqual(
            (payments["ch-trust"].amount, payments["ch-trust"].deposit_account),
            (Decimal("1500.00"), "trust"),
        )
        self.assertEqual(payments["ch-op"].deposit_account, "operating")
        self.assertEqual(payments["ch-op"].method, "card")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, "paid")

    def test_refunds_void_whole_payments_and_flag_partial_ones(self):
        number = self.invoice.number
        connector = LawPayConnector(self.connection)
        whole = charge("ch-1", number, 50000, amount_refunded=50000)
        part = charge("ch-2", number, 150000, account="acct-trust", amount_refunded=20000)
        self.lawpay.transactions = {"ch-1": whole, "ch-2": part}
        refund = {"type": "REFUND", "status": "COMPLETED", "charge_id": "ch-1", "id": "rf-1"}
        records = connector.records_for(event("r1", refund))
        self.assertEqual([(r.external_id, r.status) for r in records], [("ch-1", "refunded")])
        records = connector.records_for(event("r2", {**refund, "charge_id": "ch-2"}))
        self.assertEqual(records[0].status, "partially_refunded")
        self.assertEqual(connector.records_for(event("r3", {**refund, "status": "PENDING"})), [])

    def test_accounts_map_from_settings_then_lawpays_flag(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse("connection_detail", args=[self.connection.pk]))
        self.assertContains(page, "IOLTA (card · trust at LawPay)")
        self.assertContains(page, "eCheck (eCheck)")
        connector = LawPayConnector(self.connection)
        self.assertEqual(connector.deposit_account("acct-trust"), "trust")
        self.assertEqual(connector.deposit_account("acct-operating"), "operating")
        self.assertEqual(connector.deposit_account("ach-unknown"), "")
        self.client.post(
            reverse("connection_config", args=[self.connection.pk]),
            {
                "setting_operating_page": OPERATING_PAGE,
                "setting_trust_page": TRUST_PAGE,
                "setting_trust_accounts": ["ach-unknown"],
                "setting_operating_accounts": [],
            },
        )
        self.connection.refresh_from_db()
        self.assertEqual(LawPayConnector(self.connection).deposit_account("ach-unknown"), "trust")


class WebhookHintTests(LawPayTestCase):
    def test_a_webhook_only_prompts_a_fetch_from_lawpay(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("connection_webhooks", args=[self.connection.pk]))
        url = response.context["webhook_url"]
        self.assertContains(response, "LawPay does not sign webhooks")
        real = event("evt-9", charge("ch-9", self.invoice.number, 50000))
        self.lawpay.events = [real]
        forged = {**real, "data": {**real["data"], "amount": 99999999, "account_id": "acct-trust"}}
        with self.captureOnCommitCallbacks(execute=True):
            answer = self.client.post(url, json.dumps(forged), content_type="application/json")
        self.assertEqual(answer.status_code, 200)
        process_webhook_event.call(WebhookEvent.objects.get().pk)
        payment = Payment.objects.get()
        self.assertEqual(
            (payment.amount, payment.deposit_account), (Decimal("500.00"), "operating")
        )
        self.assertTrue(any(r.url.path == "/v1/events/evt-9" for r in self.lawpay.requests))
        # A made-up event ID records nothing, and a body that is not an event is refused.
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                url, json.dumps({"id": "evt-made-up"}), content_type="application/json"
            )
        process_webhook_event.call(WebhookEvent.objects.get(event_id="evt-made-up").pk)
        self.assertEqual(Payment.objects.count(), 1)
        refused = self.client.post(url, json.dumps({"id": "../x"}), content_type="application/json")
        self.assertEqual(refused.status_code, 401)

    def test_partial_refunds_are_left_for_staff(self):
        number = self.invoice.number
        self.lawpay.events = [event("e1", charge("ch-1", number, 50000))]
        run = SyncRun.objects.create(
            connection=self.connection,
            capability="payments",
            status="succeeded",
            cursor_after={"since": "2026-10-30T14:00:00+00:00"},
        )
        self.assertTrue(run.pk)
        self.sync()
        self.lawpay.transactions = {"ch-1": charge("ch-1", number, 50000, amount_refunded=1000)}
        self.lawpay.events = [
            event("e2", {"type": "REFUND", "status": "COMPLETED", "charge_id": "ch-1", "id": "rf"})
        ]
        self.sync()
        self.assertIsNone(Payment.objects.get().voided_at)
        self.assertTrue(AuditLog.objects.filter(action="payment.partially_refunded").exists())


class PaymentLinkMessageTests(LawPayTestCase):
    def post(self, **data):
        return self.client.post(
            reverse("payment_links_create", args=[self.invoice.pk]),
            {"connection": self.connection.pk, **data},
            follow=True,
        )

    def test_staff_are_told_why_no_link_was_made(self):
        self.client.force_login(self.admin)
        Connection.objects.filter(pk=self.connection.pk).update(preview_mode=True)
        self.assertContains(self.post(), "is in preview mode, so no link was made")
        Connection.objects.filter(pk=self.connection.pk).update(preview_mode=False, config={})
        self.assertContains(self.post(), "No payment link was made: Enter the")
        with mock.patch.object(
            LawPayConnector, "perform", return_value="http://pay.example.test/x"
        ):
            self.assertContains(self.post(), "did not return an HTTPS link")
        self.assertFalse(PaymentLink.objects.exists())
        self.assertEqual(
            self.post(connection="00000000-0000-0000-0000-000000000000").status_code, 403
        )
        Invoice.objects.filter(pk=self.invoice.pk).update(status="paid")
        self.assertContains(self.post(), "Nothing is left to pay on this invoice.")
