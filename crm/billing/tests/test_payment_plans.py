from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.billing.models import Invoice, PaymentPlan
from crm.billing.services import (
    add_line,
    add_months,
    cancel_plan,
    create_plan,
    installment_dates,
    issue_invoice,
    record_payment,
    split_amount,
    void_invoice,
    void_payment,
)
from crm.models import AuditLog, WorkItem
from crm.tests.factories import make_contact, make_matter, make_user


def payment(**overrides):
    return {"method": "check", "deposit_account": "operating", **overrides}


class ScheduleMathTests(TestCase):
    def test_add_months_clamps_to_month_end(self):
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(add_months(date(2028, 1, 31), 1), date(2028, 2, 29))
        self.assertEqual(add_months(date(2026, 11, 15), 3), date(2027, 2, 15))

    def test_installment_dates(self):
        first = date(2026, 1, 31)
        self.assertEqual(
            installment_dates(first, 3, "monthly"),
            [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)],
        )
        self.assertEqual(installment_dates(first, 2, "biweekly")[1], date(2026, 2, 14))
        self.assertEqual(installment_dates(first, 2, "weekly")[1], date(2026, 2, 7))

    def test_split_puts_leftover_cents_last(self):
        self.assertEqual(
            split_amount(Decimal("100.00"), 3),
            [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")],
        )
        self.assertEqual(sum(split_amount(Decimal("1675.00"), 7)), Decimal("1675.00"))


class PaymentPlanServiceTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.contact = make_contact(self.user)
        self.matter = make_matter(self.contact, self.user)
        self.invoice = Invoice.objects.create(contact=self.contact, matter=self.matter)
        add_line(self.invoice, description="Fictional petition", unit_price=Decimal("1200"))
        self.invoice = issue_invoice(self.invoice, self.user)
        self.first_due = timezone.localdate() + timedelta(days=10)

    def plan(self, **overrides):
        options = {"count": 4, "first_due_date": self.first_due, "frequency": "monthly"}
        return create_plan(self.invoice, self.user, **{**options, **overrides})

    def test_plan_creates_installments_and_reminders(self):
        plan = self.plan()
        installments = list(plan.installments.all())
        self.assertEqual([i.amount for i in installments], [Decimal("300.00")] * 4)
        self.assertEqual(installments[0].due_date, self.first_due)
        reminder = installments[0].reminder
        self.assertEqual(reminder.contact, self.contact)
        self.assertEqual(reminder.matter, self.matter)
        self.assertIn("installment 1 of 4, $300.00 due", reminder.title)
        self.assertEqual(
            timezone.localtime(reminder.scheduled_for).date(), self.first_due - timedelta(days=3)
        )
        self.assertEqual(self.invoice.plan, plan)
        self.assertEqual(str(plan), f"Payment plan for {self.invoice.number}")
        self.assertTrue(AuditLog.objects.filter(action="payment_plan.created").exists())

    def test_plan_rules(self):
        draft = Invoice.objects.create(contact=self.contact)
        with self.assertRaisesMessage(ValidationError, "issued invoices with a balance"):
            create_plan(
                draft, self.user, count=2, first_due_date=self.first_due, frequency="weekly"
            )
        with self.assertRaisesMessage(ValidationError, "between 2 and 60"):
            self.plan(count=1)
        with self.assertRaisesMessage(ValidationError, "in the past"):
            self.plan(first_due_date=timezone.localdate() - timedelta(days=1))
        self.plan()
        with self.assertRaisesMessage(ValidationError, "already has a payment plan"):
            self.plan()
        with transaction.atomic(), self.assertRaises(IntegrityError):
            PaymentPlan.objects.create(invoice=self.invoice, frequency="weekly")

    def test_payments_apply_oldest_first_and_complete_reminders(self):
        plan = self.plan()
        record_payment(self.invoice, self.user, amount="450", **payment())
        states = [row["state"] for row in plan.schedule()]
        self.assertEqual(states, ["paid", "partially_paid", "due", "due"])
        self.assertEqual(plan.schedule()[1]["applied"], Decimal("150.00"))
        reminders = [i.reminder for i in plan.installments.select_related("reminder")]
        self.assertEqual([r.is_completed for r in reminders], [True, False, False, False])
        self.assertEqual(plan.next_due()["installment"].sequence, 2)

        last = record_payment(self.invoice, self.user, amount="750", **payment())
        plan.refresh_from_db()
        self.assertEqual(plan.status, PaymentPlan.Status.COMPLETED)
        self.assertIsNone(plan.next_due())
        void_payment(last, self.user, "Bounced")
        plan.refresh_from_db()
        self.assertEqual(plan.status, PaymentPlan.Status.ACTIVE)
        self.assertFalse(WorkItem.objects.get(pk=reminders[3].pk).is_completed)

    def test_overdue_installments(self):
        plan = self.plan()
        plan.installments.filter(sequence=1).update(
            due_date=timezone.localdate() - timedelta(days=2)
        )
        self.assertEqual(plan.schedule()[0]["state"], "overdue")

    def test_cancel_removes_open_reminders_and_allows_a_new_plan(self):
        plan = self.plan()
        record_payment(self.invoice, self.user, amount="300", **payment())
        cancel_plan(plan, self.user)
        plan.refresh_from_db()
        self.assertEqual(plan.status, PaymentPlan.Status.CANCELED)
        self.assertEqual(WorkItem.objects.filter(title__startswith="Payment reminder").count(), 1)
        with self.assertRaisesMessage(ValidationError, "already canceled"):
            cancel_plan(plan, self.user)
        self.assertIsNone(self.invoice.plan)
        replacement = self.plan(count=3)
        self.assertEqual(
            [i.amount for i in replacement.installments.all()], [Decimal("300.00")] * 3
        )

    def test_voiding_the_invoice_cancels_its_plan(self):
        plan = self.plan()
        void_invoice(self.invoice, self.user, "Billed in error")
        plan.refresh_from_db()
        self.assertEqual(plan.status, PaymentPlan.Status.CANCELED)
        self.assertFalse(WorkItem.objects.filter(title__startswith="Payment reminder").exists())


class PaymentPlanViewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.contact = make_contact(self.user)
        self.invoice = Invoice.objects.create(contact=self.contact)
        add_line(self.invoice, description="Fictional consultation", unit_price=Decimal("500"))
        self.invoice = issue_invoice(self.invoice, self.user)
        self.client.force_login(self.user)
        self.url = reverse("invoice_detail", args=[self.invoice.pk])

    def test_create_view_print_and_cancel(self):
        page = self.client.get(self.url)
        self.assertContains(page, "Create payment plan")
        first_due = (timezone.localdate() + timedelta(days=5)).isoformat()
        response = self.client.post(
            reverse("plan_create", args=[self.invoice.pk]),
            {
                "count": "2",
                "frequency": "biweekly",
                "first_due_date": first_due,
                "reminder_days_before": "2",
            },
            follow=True,
        )
        self.assertContains(response, "Payment plan created with 2 installments")
        self.assertContains(response, "Every two weeks installments")
        printed = self.client.get(reverse("invoice_print", args=[self.invoice.pk]))
        self.assertContains(printed, "Payment schedule")
        self.assertContains(printed, "$250.00")
        contact_page = self.client.get(reverse("contact_detail", args=[self.contact.pk]))
        self.assertContains(contact_page, "Payment plan: next installment $250.00")
        work = self.client.get(reverse("work_list"))
        self.assertContains(work, "Payment reminder")

        response = self.client.post(reverse("plan_cancel", args=[self.invoice.pk]), follow=True)
        self.assertContains(response, "Payment plan canceled")
        self.assertContains(response, "Create payment plan")
        response = self.client.post(reverse("plan_cancel", args=[self.invoice.pk]), follow=True)
        self.assertContains(response, "has no payment plan")

    def test_invalid_input(self):
        response = self.client.post(
            reverse("plan_create", args=[self.invoice.pk]), {"count": "x"}, follow=True
        )
        self.assertContains(response, "Review the payment plan fields")
        response = self.client.post(
            reverse("plan_create", args=[self.invoice.pk]),
            {
                "count": "2",
                "frequency": "weekly",
                "first_due_date": "2020-01-01",
                "reminder_days_before": "3",
            },
            follow=True,
        )
        self.assertContains(response, "cannot be due in the past")
