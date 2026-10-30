import calendar
import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from crm.comms.system_email import address_problem
from crm.models import AuditLog, TimeEntry, WorkItem

from .models import (
    BillingSettings,
    Invoice,
    InvoiceLine,
    Payment,
    PaymentPlan,
    PaymentPlanInstallment,
    StaffRate,
    format_money,
    money,
)

OPEN_STATUSES = (Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID)


def record(actor, action, target, detail=""):
    AuditLog.objects.create(
        user=actor,
        action=action,
        target_type=target.__class__.__name__,
        target_id=str(target.pk),
        detail=detail[:500],
    )


def hourly_rate_for(user):
    rate = StaffRate.objects.filter(user=user).values_list("hourly_rate", flat=True).first()
    if rate is not None:
        return rate
    return BillingSettings.load().default_hourly_rate


def billable_hours(entry, increment_minutes):
    """Round a stopped time entry up to the billing increment, in hours."""
    minutes = entry.duration.total_seconds() / 60
    increments = max(1, math.ceil(minutes / increment_minutes - 1e-9))
    return (Decimal(increments * increment_minutes) / Decimal(60)).quantize(Decimal("0.01"))


def unbilled_time(invoice):
    """Stopped time entries for the invoice's matter (or contact) not yet on an invoice."""
    entries = TimeEntry.objects.select_related("user").filter(
        stopped_at__isnull=False, invoice_line__isnull=True
    )
    if invoice.matter_id:
        entries = entries.filter(matter=invoice.matter)
    else:
        entries = entries.filter(
            Q(contact=invoice.contact) | Q(matter__primary_contact=invoice.contact)
        )
    return entries.order_by("started_at")


def require_draft(invoice):
    if not invoice.is_draft:
        raise ValidationError("Only draft invoices can be changed.")


def next_position(invoice):
    return (invoice.lines.aggregate(top=Max("position"))["top"] or 0) + 1


@transaction.atomic
def add_time_to_invoice(invoice, entries):
    """Add each time entry as a line at its staff member's rate, rounded up to the increment."""
    require_draft(invoice)
    settings = BillingSettings.load()
    missing = sorted({str(e.user) for e in entries if hourly_rate_for(e.user) is None})
    if missing:
        raise ValidationError(
            f"Set an hourly rate for {', '.join(missing)} (or a firm default) in billing settings."
        )
    position = next_position(invoice)
    lines = []
    for offset, entry in enumerate(entries):
        started = timezone.localtime(entry.started_at)
        when = f"{started:%b} {started.day}, {started:%Y}"
        person = entry.user.get_full_name() or entry.user.get_username()
        description = f"{when} · {person} · {entry.note or 'Legal services'}"
        lines.append(
            InvoiceLine.objects.create(
                invoice=invoice,
                position=position + offset,
                kind=InvoiceLine.Kind.LEGAL_FEE,
                funds=InvoiceLine.Funds.EARNED,
                description=description[:255],
                quantity=billable_hours(entry, settings.time_increment_minutes),
                unit_price=hourly_rate_for(entry.user),
                time_entry=entry,
            )
        )
    return lines


def add_line(invoice, **fields):
    require_draft(invoice)
    return InvoiceLine.objects.create(invoice=invoice, position=next_position(invoice), **fields)


@transaction.atomic
def issue_invoice(invoice, actor):
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    require_draft(invoice)
    if not invoice.lines.exists() or invoice.subtotal <= 0:
        raise ValidationError("Add at least one line with an amount before issuing.")
    settings = BillingSettings.load()
    invoice.number = BillingSettings.next_invoice_number()
    invoice.issue_date = timezone.localdate()
    invoice.due_date = invoice.issue_date + timedelta(days=settings.payment_terms_days)
    invoice.status = Invoice.Status.ISSUED
    invoice.save(update_fields=["number", "issue_date", "due_date", "status", "updated_at"])
    record(actor, "invoice.issued", invoice, f"{invoice.number} for {invoice.subtotal}")
    return invoice


def refresh_status(invoice):
    if invoice.status not in (*OPEN_STATUSES, Invoice.Status.PAID):
        return invoice
    paid = invoice.amount_paid
    if paid >= invoice.subtotal:
        invoice.status = Invoice.Status.PAID
    elif paid > 0:
        invoice.status = Invoice.Status.PARTIALLY_PAID
    else:
        invoice.status = Invoice.Status.ISSUED
    invoice.save(update_fields=["status", "updated_at"])
    return invoice


@transaction.atomic
def record_payment(invoice, actor, *, amount, **fields):
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status not in OPEN_STATUSES:
        raise ValidationError("Payments can be recorded only on issued, unpaid invoices.")
    amount = money(Decimal(amount))
    if amount <= 0:
        raise ValidationError("Enter a payment amount greater than zero.")
    if amount > invoice.balance:
        raise ValidationError(f"The balance due is {invoice.balance}; record no more than that.")
    payment = Payment.objects.create(invoice=invoice, amount=amount, recorded_by=actor, **fields)
    refresh_status(invoice)
    sync_plan(invoice)
    record(
        actor,
        "payment.recorded",
        payment,
        f"{amount} to {payment.get_deposit_account_display()} for {invoice.number}",
    )
    return payment


@transaction.atomic
def void_payment(payment, actor, reason):
    if payment.voided_at:
        raise ValidationError("This payment is already void.")
    if not reason.strip():
        raise ValidationError("Give a reason for voiding the payment.")
    payment.voided_at = timezone.now()
    payment.void_reason = reason.strip()[:255]
    payment.save(update_fields=["voided_at", "void_reason"])
    refresh_status(payment.invoice)
    sync_plan(payment.invoice)
    record(actor, "payment.voided", payment, payment.void_reason)
    return payment


@transaction.atomic
def void_invoice(invoice, actor, reason):
    """Void an issued invoice with no live payments, freeing its time for re-billing."""
    if invoice.status not in OPEN_STATUSES:
        raise ValidationError("Only issued invoices without payments can be voided.")
    if invoice.payments.filter(voided_at__isnull=True).exists():
        raise ValidationError("Void the invoice's payments first.")
    if not reason.strip():
        raise ValidationError("Give a reason for voiding the invoice.")
    plan = invoice.plan
    if plan is not None:
        cancel_plan(plan, actor)
    invoice.lines.update(time_entry=None)
    invoice.status = Invoice.Status.VOID
    invoice.void_reason = reason.strip()[:255]
    invoice.save(update_fields=["status", "void_reason", "updated_at"])
    record(actor, "invoice.voided", invoice, invoice.void_reason)
    return invoice


def delete_draft(invoice, actor):
    require_draft(invoice)
    record(actor, "invoice.draft_deleted", invoice, str(invoice.contact))
    invoice.delete()


MAX_INSTALLMENTS = 60


def add_months(day, months):
    """The same day of the month ``months`` later, clamped to the month's last day."""
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def installment_dates(first_due, count, frequency):
    if frequency == PaymentPlan.Frequency.MONTHLY:
        return [add_months(first_due, n) for n in range(count)]
    step = 14 if frequency == PaymentPlan.Frequency.BIWEEKLY else 7
    return [first_due + timedelta(days=step * n) for n in range(count)]


def split_amount(total, count):
    """Equal installments, with any leftover cents on the last one."""
    each = money(total / count)
    return [each] * (count - 1) + [money(total - each * (count - 1))]


def reminder_moment(due_date, days_before):
    """9 a.m. on the reminder day."""
    return timezone.make_aware(datetime.combine(due_date - timedelta(days=days_before), time(9)))


def reminder_time(due_date, days_before):
    return max(reminder_moment(due_date, days_before), timezone.now())


@transaction.atomic
def create_plan(
    invoice,
    actor,
    *,
    count,
    first_due_date,
    frequency,
    reminder_days_before=3,
    email_reminders=False,
):
    """Split the invoice's balance into installments, each with a staff reminder work item."""
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status not in OPEN_STATUSES or invoice.balance <= 0:
        raise ValidationError("Payment plans are for issued invoices with a balance due.")
    if invoice.plan is not None:
        raise ValidationError("This invoice already has a payment plan.")
    if not 2 <= count <= MAX_INSTALLMENTS:
        raise ValidationError(f"Choose between 2 and {MAX_INSTALLMENTS} installments.")
    if first_due_date < timezone.localdate():
        raise ValidationError("The first installment cannot be due in the past.")
    if email_reminders and address_problem(invoice.contact):
        raise ValidationError(address_problem(invoice.contact))
    plan = PaymentPlan.objects.create(
        invoice=invoice,
        frequency=frequency,
        reminder_days_before=reminder_days_before,
        email_reminders=email_reminders,
        created_by=actor,
    )
    currency = BillingSettings.load().currency
    dates = installment_dates(first_due_date, count, frequency)
    for sequence, (due, amount) in enumerate(
        zip(dates, split_amount(invoice.balance, count), strict=True), 1
    ):
        reminder = WorkItem.objects.create(
            title=(
                f"Payment reminder: {invoice.number} installment {sequence} of {count}, "
                f"{format_money(amount, currency)} due {due:%b} {due.day}"
            ),
            description=(
                "Remind the client of this payment plan installment. "
                "This reminder completes itself when the installment is paid."
            ),
            kind=WorkItem.Kind.TASK,
            contact=invoice.contact,
            matter=invoice.matter,
            assigned_to=actor,
            scheduled_for=reminder_time(due, reminder_days_before),
        )
        PaymentPlanInstallment.objects.create(
            plan=plan, sequence=sequence, due_date=due, amount=amount, reminder=reminder
        )
    record(
        actor,
        "payment_plan.created",
        invoice,
        f"{count} {plan.get_frequency_display().lower()} installments",
    )
    return plan


def sync_plan(invoice):
    """Match reminders and plan status to what has been paid."""
    plan = invoice.plan
    if plan is None:
        return None
    now = timezone.now()
    for row in plan.schedule():
        reminder = row["installment"].reminder
        if reminder is None:
            continue
        paid = row["state"] == "paid"
        if reminder.is_completed != paid:
            reminder.is_completed = paid
            reminder.completed_at = now if paid else None
            reminder.save(update_fields=["is_completed", "completed_at", "updated_at"])
    plan.status = (
        PaymentPlan.Status.COMPLETED
        if invoice.status == Invoice.Status.PAID
        else PaymentPlan.Status.ACTIVE
    )
    plan.save(update_fields=["status"])
    return plan


def set_email_reminders(plan, actor, on):
    """Turn the client's email reminders on or off for a plan in force."""
    if plan.status != PaymentPlan.Status.ACTIVE:
        raise ValidationError("Only an active payment plan's reminders can be changed.")
    if on and address_problem(plan.invoice.contact):
        raise ValidationError(address_problem(plan.invoice.contact))
    plan.email_reminders = on
    plan.save(update_fields=["email_reminders"])
    record(actor, "payment_plan.email_reminders_" + ("on" if on else "off"), plan.invoice)
    return plan


@transaction.atomic
def cancel_plan(plan, actor):
    """End a plan; its open reminders are removed, and its history is kept."""
    if plan.status == PaymentPlan.Status.CANCELED:
        raise ValidationError("This payment plan is already canceled.")
    WorkItem.objects.filter(payment_plan_installment__plan=plan, is_completed=False).delete()
    plan.status = PaymentPlan.Status.CANCELED
    plan.canceled_at = timezone.now()
    plan.save(update_fields=["status", "canceled_at"])
    record(actor, "payment_plan.canceled", plan.invoice)
    return plan
