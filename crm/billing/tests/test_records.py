from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from crm.billing.models import Invoice
from crm.billing.records import store_one
from crm.billing.services import add_line, issue_invoice
from crm.connect.models import Connection
from crm.connect.records import PaymentRecord
from crm.models import AuditLog
from crm.tests.factories import make_contact, make_user


class PaymentCurrencyTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.connection = Connection.objects.create(connector_key="example", label="Fictional card")
        self.invoice = Invoice.objects.create(contact=make_contact(self.user))
        add_line(self.invoice, description="Fictional fee", unit_price=Decimal("100"))
        self.invoice = issue_invoice(self.invoice, self.user)

    def record(self, currency):
        store_one(
            self.connection,
            PaymentRecord(
                external_id="fictional-charge",
                occurred_at=timezone.now(),
                amount="100.00",
                currency=currency,
                reference=self.invoice.number,
                deposit_account="operating",
            ),
        )

    def test_foreign_or_missing_currency_is_held_for_review(self):
        for currency in ("EUR", ""):
            with self.subTest(currency=currency):
                self.record(currency)
                self.invoice.refresh_from_db()
                self.assertEqual(self.invoice.status, Invoice.Status.ISSUED)
                self.assertEqual(self.invoice.balance, Decimal("100.00"))
                self.assertFalse(self.invoice.payments.exists())
        self.assertEqual(AuditLog.objects.filter(action="payment.currency_mismatch").count(), 2)

    def test_matching_currency_is_case_insensitive_and_idempotent(self):
        self.record("usd")
        self.record("USD")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)
        self.assertEqual(self.invoice.payments.count(), 1)
