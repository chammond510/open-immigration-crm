from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from crm.billing.links import amounts_due, create_links, link_connections
from crm.billing.models import Invoice, InvoiceLine, Payment
from crm.billing.services import add_line, issue_invoice, record_payment, void_payment
from crm.tests.factories import make_contact, make_matter, make_user


class AmountsDueTests(TestCase):
    def setUp(self):
        self.user = make_user()
        contact = make_contact(self.user)
        self.invoice = Invoice.objects.create(
            contact=contact, matter=make_matter(contact, self.user)
        )

    def line(self, amount, funds):
        add_line(
            self.invoice,
            description=f"Fictional {funds} line",
            unit_price=Decimal(amount),
            funds=funds,
        )

    def pay(self, amount, account):
        return record_payment(
            self.invoice,
            self.user,
            amount=Decimal(amount),
            method=Payment.Method.CARD,
            deposit_account=account,
        )

    def test_advances_and_earned_fees_are_owed_into_their_own_accounts(self):
        self.line("1500.00", InvoiceLine.Funds.ADVANCE)
        self.line("500.00", InvoiceLine.Funds.EARNED)
        self.assertEqual(amounts_due(self.invoice), {})
        self.invoice = issue_invoice(self.invoice, self.user)
        self.assertEqual(
            amounts_due(self.invoice),
            {
                Payment.Account.TRUST: Decimal("1500.00"),
                Payment.Account.OPERATING: Decimal("500.00"),
            },
        )
        trust = self.pay("1000.00", Payment.Account.TRUST)
        self.pay("500.00", Payment.Account.OPERATING)
        self.assertEqual(amounts_due(self.invoice), {Payment.Account.TRUST: Decimal("500.00")})
        void_payment(trust, self.user, "Fictional bounce")
        self.assertEqual(amounts_due(self.invoice), {Payment.Account.TRUST: Decimal("1500.00")})

    def test_nothing_due_means_no_links(self):
        self.line("200.00", InvoiceLine.Funds.EARNED)
        self.invoice = issue_invoice(self.invoice, self.user)
        self.pay("200.00", Payment.Account.OPERATING)
        self.invoice.refresh_from_db()
        self.assertEqual(amounts_due(self.invoice), {})
        self.assertEqual(link_connections(), [])
        with self.assertRaisesMessage(ValidationError, "Nothing is left to pay"):
            create_links(None, self.invoice, self.user)
