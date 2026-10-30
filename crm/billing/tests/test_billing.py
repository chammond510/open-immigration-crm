import io
import json
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.billing.models import BillingSettings, Invoice, InvoiceLine, Payment, StaffRate
from crm.billing.services import (
    add_line,
    add_time_to_invoice,
    billable_hours,
    delete_draft,
    hourly_rate_for,
    issue_invoice,
    record_payment,
    unbilled_time,
    void_invoice,
    void_payment,
)
from crm.billing.templatetags.billing_tags import money
from crm.models import AuditLog, TimeEntry
from crm.tests.factories import make_contact, make_matter, make_user


def grant_billing(user):
    user.user_permissions.add(Permission.objects.get(codename="access_billing"))
    return type(user).objects.get(pk=user.pk)


def time_entry(user, matter=None, contact=None, minutes=50, note="Drafted fictional brief"):
    end = timezone.now() - timedelta(hours=1)
    return TimeEntry.objects.create(
        user=user,
        matter=matter,
        contact=contact,
        started_at=end - timedelta(minutes=minutes),
        stopped_at=end,
        note=note,
    )


def payment_fields(**overrides):
    return {
        "method": Payment.Method.CHECK,
        "deposit_account": Payment.Account.OPERATING,
        "reference": "Check 1001",
        **overrides,
    }


class BillingServiceTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.contact = make_contact(self.user)
        self.matter = make_matter(self.contact, self.user)
        self.invoice = Invoice.objects.create(contact=self.contact, matter=self.matter)

    def test_invoice_numbers_are_sequential_and_reset_each_year(self):
        year = timezone.localdate().year
        self.assertEqual(BillingSettings.next_invoice_number(), f"INV-{year}-0001")
        self.assertEqual(BillingSettings.next_invoice_number(), f"INV-{year}-0002")
        BillingSettings.objects.filter(pk=1).update(invoice_sequence_year=year - 1)
        self.assertEqual(BillingSettings.next_invoice_number(), f"INV-{year}-0001")
        settings = BillingSettings.load()
        settings.invoice_prefix = " bill "
        settings.save()
        self.assertEqual(BillingSettings.load().invoice_prefix, "BILL")
        self.assertEqual(str(settings), "Billing settings")

    def test_billable_hours_round_up_to_the_increment(self):
        entry = time_entry(self.user, self.matter, minutes=50)
        self.assertEqual(billable_hours(entry, 6), Decimal("0.90"))
        self.assertEqual(billable_hours(entry, 15), Decimal("1.00"))
        self.assertEqual(
            billable_hours(time_entry(self.user, self.matter, minutes=1), 6), Decimal("0.10")
        )
        self.assertEqual(
            billable_hours(time_entry(self.user, self.matter, minutes=60), 6), Decimal("1.00")
        )

    def test_rates_fall_back_to_the_firm_default(self):
        self.assertIsNone(hourly_rate_for(self.user))
        settings = BillingSettings.load()
        settings.default_hourly_rate = Decimal("200")
        settings.save()
        self.assertEqual(hourly_rate_for(self.user), Decimal("200"))
        StaffRate.objects.create(user=self.user, hourly_rate=Decimal("350"))
        self.assertEqual(hourly_rate_for(self.user), Decimal("350"))

    def test_time_is_billed_once(self):
        entry = time_entry(self.user, self.matter)
        running = TimeEntry.objects.create(user=make_user("assistant"), matter=self.matter)
        self.assertEqual(list(unbilled_time(self.invoice)), [entry])
        with self.assertRaisesMessage(ValidationError, "Set an hourly rate"):
            add_time_to_invoice(self.invoice, [entry])
        StaffRate.objects.create(user=self.user, hourly_rate=Decimal("300"))
        line = add_time_to_invoice(self.invoice, [entry])[0]
        self.assertEqual(
            (line.quantity, line.unit_price, line.amount),
            (Decimal("0.90"), Decimal("300"), Decimal("270.00")),
        )
        self.assertIn("Drafted fictional brief", line.description)
        self.assertIn(self.user.get_username(), line.description)
        self.user.first_name, self.user.last_name = "Dana", "Example"
        self.user.save()
        second = add_time_to_invoice(self.invoice, [time_entry(self.user, self.matter)])[0]
        self.assertIn("Dana Example", second.description)
        self.assertEqual(list(unbilled_time(self.invoice)), [])
        self.assertTrue(running.is_running)
        contact_invoice = Invoice.objects.create(contact=self.contact)
        other = time_entry(self.user, contact=self.contact)
        self.assertEqual(list(unbilled_time(contact_invoice)), [other])

    def test_issue_numbers_dates_and_locks(self):
        with self.assertRaisesMessage(ValidationError, "Add at least one line"):
            issue_invoice(self.invoice, self.user)
        add_line(self.invoice, description="Fictional petition", unit_price=Decimal("1500"))
        invoice = issue_invoice(self.invoice, self.user)
        self.assertEqual(invoice.status, Invoice.Status.ISSUED)
        self.assertTrue(invoice.number.startswith("INV-"))
        self.assertEqual(invoice.due_date - invoice.issue_date, timedelta(days=30))
        self.assertEqual(str(invoice), invoice.number)
        self.assertTrue(AuditLog.objects.filter(action="invoice.issued").exists())
        with self.assertRaisesMessage(ValidationError, "Only draft invoices"):
            add_line(invoice, description="Late addition", unit_price=Decimal("10"))
        with self.assertRaises(ValidationError):
            issue_invoice(invoice, self.user)

    def test_payments_move_status_and_cannot_overpay(self):
        add_line(self.invoice, description="Fee", unit_price=Decimal("1000"))
        add_line(
            self.invoice,
            description="Filing fee",
            unit_price=Decimal("675"),
            kind=InvoiceLine.Kind.FILING_FEE,
            funds=InvoiceLine.Funds.ADVANCE,
        )
        with self.assertRaisesMessage(ValidationError, "issued, unpaid"):
            record_payment(self.invoice, self.user, amount="10", **payment_fields())
        invoice = issue_invoice(self.invoice, self.user)
        self.assertEqual(invoice.subtotal_for(InvoiceLine.Funds.ADVANCE), Decimal("675.00"))
        with self.assertRaisesMessage(ValidationError, "balance due is 1675.00"):
            record_payment(invoice, self.user, amount="2000", **payment_fields())
        with self.assertRaisesMessage(ValidationError, "greater than zero"):
            record_payment(invoice, self.user, amount="0", **payment_fields())
        first = record_payment(
            invoice, self.user, amount="675", **payment_fields(deposit_account="trust")
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.PARTIALLY_PAID)
        self.assertEqual(invoice.balance, Decimal("1000.00"))
        entry = AuditLog.objects.get(action="payment.recorded")
        self.assertIn("Trust account", entry.detail)
        record_payment(invoice, self.user, amount="1000", **payment_fields())
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.PAID)
        self.assertEqual(str(first), f"675.00 on {first.received_on}")

        with self.assertRaisesMessage(ValidationError, "reason"):
            void_payment(first, self.user, "  ")
        void_payment(first, self.user, "Bounced check")
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.PARTIALLY_PAID)
        with self.assertRaisesMessage(ValidationError, "already void"):
            void_payment(first, self.user, "Again")

    def test_voiding_an_invoice_frees_its_time(self):
        StaffRate.objects.create(user=self.user, hourly_rate=Decimal("300"))
        entry = time_entry(self.user, self.matter)
        add_time_to_invoice(self.invoice, [entry])
        invoice = issue_invoice(self.invoice, self.user)
        payment = record_payment(invoice, self.user, amount="100", **payment_fields())
        with self.assertRaisesMessage(ValidationError, "payments first"):
            void_invoice(invoice, self.user, "Billed in error")
        void_payment(payment, self.user, "Refunded")
        with self.assertRaisesMessage(ValidationError, "reason"):
            void_invoice(invoice, self.user, "")
        void_invoice(invoice, self.user, "Billed in error")
        invoice.refresh_from_db()
        self.assertEqual((invoice.status, invoice.balance), (Invoice.Status.VOID, Decimal("0.00")))
        draft = Invoice.objects.create(contact=self.contact, matter=self.matter)
        self.assertEqual(list(unbilled_time(draft)), [entry])
        with self.assertRaises(ValidationError):
            void_invoice(invoice, self.user, "Again")

    def test_drafts_can_be_deleted_and_overdue_is_detected(self):
        self.assertEqual(str(self.invoice), f"Draft invoice for {self.contact}")
        delete_draft(self.invoice, self.user)
        self.assertFalse(Invoice.objects.exists())
        invoice = Invoice.objects.create(contact=self.contact)
        add_line(invoice, description="Fee", unit_price=Decimal("100"))
        issue_invoice(invoice, self.user)
        invoice.refresh_from_db()
        with self.assertRaises(ValidationError):
            delete_draft(invoice, self.user)
        self.assertFalse(invoice.is_overdue)
        Invoice.objects.filter(pk=invoice.pk).update(
            due_date=timezone.localdate() - timedelta(days=1)
        )
        invoice.refresh_from_db()
        self.assertTrue(invoice.is_overdue)

    def test_money_filter(self):
        self.assertEqual(money(Decimal("1234.5")), "$1,234.50")
        self.assertEqual(money(Decimal("-20")), "-$20.00")
        self.assertEqual(money(None), "$0.00")
        self.assertEqual(money(Decimal("10"), "EUR"), "10.00 EUR")


class BillingViewTests(TestCase):
    def setUp(self):
        self.admin = make_user()
        self.biller = grant_billing(make_user("bookkeeper", superuser=False))
        self.staff = make_user("paralegal", superuser=False)
        self.contact = make_contact(self.admin)
        self.matter = make_matter(self.contact, self.admin)
        StaffRate.objects.create(user=self.admin, hourly_rate=Decimal("300"))

    def test_billing_requires_the_permission(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("invoice_list")).status_code, 302)
        page = self.client.get(reverse("contact_detail", args=[self.contact.pk]))
        self.assertNotContains(page, 'href="/billing/"')
        self.assertNotContains(page, "New invoice")
        self.client.force_login(self.biller)
        page = self.client.get(reverse("contact_detail", args=[self.contact.pk]))
        self.assertContains(page, 'href="/billing/"')
        self.assertContains(page, "New invoice")
        self.assertEqual(self.client.get(reverse("invoice_list")).status_code, 200)

    def test_full_invoice_flow(self):
        entry = time_entry(self.admin, self.matter)
        self.client.force_login(self.biller)
        matter_page = self.client.get(reverse("matter_detail", args=[self.matter.pk]))
        self.assertContains(matter_page, "1 unbilled time entry")
        form_page = self.client.get(reverse("invoice_create"), {"matter": self.matter.pk})
        self.assertEqual(form_page.context["form"].initial["contact"], self.contact)
        response = self.client.post(reverse("invoice_create"), {"matter": str(self.matter.pk)})
        invoice = Invoice.objects.get()
        self.assertRedirects(response, reverse("invoice_detail", args=[invoice.pk]))
        self.assertEqual(invoice.contact, self.contact)
        detail = self.client.get(reverse("invoice_detail", args=[invoice.pk]))
        self.assertContains(detail, "Unbilled time")
        self.assertContains(detail, "Drafted fictional brief")

        url = lambda name: reverse(name, args=[invoice.pk])  # noqa: E731
        self.client.post(url("invoice_time_add"), {"entries": [entry.pk]})
        self.client.post(
            url("invoice_line_add"),
            {
                "kind": "filing_fee",
                "funds": "advance",
                "description": "Fictional filing fee",
                "quantity": "1",
                "unit_price": "675",
            },
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.subtotal, Decimal("945.00"))
        preview = self.client.get(url("invoice_print"))
        self.assertContains(preview, "Draft — not issued")
        self.client.post(url("invoice_issue"))
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.ISSUED)
        printed = self.client.get(url("invoice_print"))
        self.assertContains(printed, invoice.number)
        self.assertContains(printed, "Advance, held in trust")
        self.assertContains(printed, "$945.00")

        detail = self.client.get(url("invoice_detail"))
        self.assertContains(detail, "which belong in the trust account")
        self.assertContains(
            detail, "Connect your payment processor to send a payment link instead."
        )
        self.client.post(
            url("payment_record"),
            {
                "received_on": "2026-01-05",
                "amount": "945",
                "method": "check",
                "deposit_account": "trust",
                "reference": "1001",
            },
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.PAID)
        listing = self.client.get(reverse("invoice_list"), {"status": "paid"})
        self.assertContains(listing, invoice.number)
        self.assertContains(
            self.client.get(reverse("invoice_list"), {"q": "zzz"}), "No invoices found"
        )

    def test_guards_and_errors(self):
        self.client.force_login(self.biller)
        invoice = Invoice.objects.create(contact=self.contact)
        url = lambda name: reverse(name, args=[invoice.pk])  # noqa: E731
        self.assertContains(
            self.client.post(url("invoice_issue"), follow=True), "Add at least one line"
        )
        self.assertContains(
            self.client.post(url("invoice_time_add"), follow=True), "Select the time"
        )
        self.assertContains(
            self.client.post(url("invoice_line_add"), {"description": ""}, follow=True),
            "Review the line fields",
        )
        add_line(invoice, description="Fee", unit_price=Decimal("100"))
        issue_invoice(invoice, self.admin)
        self.assertContains(
            self.client.post(url("invoice_line_add"), {"description": "x"}, follow=True),
            "Only draft invoices can be changed",
        )
        self.assertContains(
            self.client.post(url("invoice_delete"), follow=True), "Only draft invoices"
        )
        response = self.client.post(
            url("invoice_notes"), {"memo": "Changed", "notes": "Called client"}
        )
        self.assertContains(self.client.get(response.url), "fixed once the invoice is issued")
        self.client.post(url("invoice_notes"), {"memo": "", "notes": "Called client"})
        invoice.refresh_from_db()
        self.assertEqual(invoice.notes, "Called client")
        self.assertContains(
            self.client.post(url("payment_record"), {"amount": "x"}, follow=True),
            "Review the payment fields",
        )
        self.assertContains(
            self.client.post(
                url("payment_record"),
                {
                    "received_on": "2026-01-05",
                    "amount": "500",
                    "method": "cash",
                    "deposit_account": "operating",
                },
                follow=True,
            ),
            "balance due is 100.00",
        )
        self.assertEqual(self.client.post(url("invoice_void"), {"reason": "x"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("billing_settings")).status_code, 403)

    def test_superuser_voids_and_settings(self):
        self.client.force_login(self.admin)
        invoice = Invoice.objects.create(contact=self.contact)
        add_line(invoice, description="Fee", unit_price=Decimal("100"))
        issue_invoice(invoice, self.admin)
        payment = record_payment(invoice, self.admin, amount="40", **payment_fields())
        url = lambda name: reverse(name, args=[invoice.pk])  # noqa: E731
        self.assertContains(
            self.client.post(url("invoice_void"), {"reason": "Wrong"}, follow=True),
            "payments first",
        )
        self.client.post(
            reverse("payment_void", args=[invoice.pk, payment.pk]), {"reason": "Refunded"}
        )
        self.client.post(url("invoice_void"), {"reason": "Wrong client"})
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.VOID)
        self.assertRedirects(
            self.client.get(url("invoice_print")),
            url("invoice_detail"),
            fetch_redirect_response=False,
        )
        draft = Invoice.objects.create(contact=self.contact)
        self.assertRedirects(
            self.client.post(reverse("invoice_delete", args=[draft.pk])), reverse("invoice_list")
        )

        page = self.client.get(reverse("billing_settings"))
        self.assertContains(page, "Staff hourly rates")
        form = {
            "currency": "usd",
            "invoice_prefix": "INV",
            "default_hourly_rate": "250",
            "payment_terms_days": "15",
            "time_increment_minutes": "6",
            "payment_instructions": "Checks payable to the fictional firm.",
            f"rate_{self.admin.pk}": "325",
            f"rate_{self.biller.pk}": "",
        }
        bad = self.client.post(
            reverse("billing_settings"), {**form, f"rate_{self.admin.pk}": "lots"}
        )
        self.assertContains(bad, "Enter a valid rate")
        self.client.post(reverse("billing_settings"), form)
        settings = BillingSettings.load()
        self.assertEqual((settings.currency, settings.payment_terms_days), ("USD", 15))
        self.assertEqual(StaffRate.objects.get(user=self.admin).hourly_rate, Decimal("325"))
        self.assertTrue(AuditLog.objects.filter(action="billing.settings_changed").exists())

    def test_export_includes_billing(self):
        invoice = Invoice.objects.create(contact=self.contact)
        add_line(invoice, description="Fee", unit_price=Decimal("100"))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "export.json"
            call_command("export_portable_data", str(output), stdout=io.StringIO())
            models = {record["model"] for record in json.loads(output.read_text())}
        self.assertTrue({"billing.invoice", "billing.invoiceline"} <= models)
