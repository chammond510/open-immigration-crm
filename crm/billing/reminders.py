"""Email reminders to clients before payment plan installments are due.

A plan created with email reminders on has each installment's reminder emailed to
the invoice contact at 9 a.m. on the staff reminder's day, through the firm's
system email connection. The worker claims each due installment once, so an
installment is never emailed twice, and nothing is sent once its due date has
passed or it is paid. If an email cannot be sent, or is held in preview mode, the
staff reminder says so, so staff can remind the client themselves.
"""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django_tasks import task

from crm.comms import system_email
from crm.models import FirmProfile

from .models import BillingSettings, PaymentPlan, PaymentPlanInstallment, format_money
from .services import reminder_moment

# The longest a plan's reminder can come before the due date (its form's limit).
MAX_DAYS_BEFORE = 30


def queue_due(sender=None, now=None, **kwargs):
    """Claim each installment whose email reminder is due and queue it; return how many."""
    now = now or timezone.now()
    if system_email.sender() is None:
        return 0
    today = timezone.localdate(now)
    candidates = PaymentPlanInstallment.objects.filter(
        plan__status=PaymentPlan.Status.ACTIVE,
        plan__email_reminders=True,
        reminder_emailed_at__isnull=True,
        due_date__gte=today,
        due_date__lte=today + timedelta(days=MAX_DAYS_BEFORE),
    ).select_related("plan")
    queued, states = 0, {}
    for installment in candidates:
        plan = installment.plan
        if reminder_moment(installment.due_date, plan.reminder_days_before) > now:
            continue
        if plan.pk not in states:
            states[plan.pk] = {row["installment"].pk: row["state"] for row in plan.schedule()}
        if states[plan.pk].get(installment.pk) == "paid":
            continue
        with transaction.atomic():
            claimed = PaymentPlanInstallment.objects.filter(
                pk=installment.pk, reminder_emailed_at__isnull=True
            ).update(reminder_emailed_at=now)
            if claimed:
                transaction.on_commit(lambda pk=installment.pk: send_reminder.enqueue(pk))
                queued += 1
    return queued


def note_on_staff_reminder(installment, text):
    reminder = installment.reminder
    if reminder is not None:
        reminder.description = f"{reminder.description}\n\n{text}".strip()
        reminder.save(update_fields=["description", "updated_at"])


def reminder_email(installment, owed):
    plan = installment.plan
    invoice = plan.invoice
    firm = FirmProfile.load().name
    amount = format_money(owed, BillingSettings.load().currency)
    due = f"{installment.due_date:%B} {installment.due_date.day}, {installment.due_date:%Y}"
    count = plan.installments.count()
    subject = f"Payment reminder: {amount} due {due}"
    body = (
        f"Hello {invoice.contact.first_name},\n\n"
        f"This is a reminder from {firm} that installment {installment.sequence} of {count} "
        f"on invoice {invoice.number}, {amount}, is due on {due}.\n\n"
        "If you have already paid, thank you, and please disregard this email. If you have "
        "questions, please contact our office.\n"
    )
    summary = (
        f"Payment reminder for invoice {invoice.number}: installment {installment.sequence} "
        f"of {count}, {amount} due {due}."
    )
    return subject, body, summary


@task()
def send_reminder(installment_id):
    installment = (
        PaymentPlanInstallment.objects.select_related(
            "plan__invoice__contact", "plan__invoice__matter", "reminder"
        )
        .filter(pk=installment_id)
        .first()
    )
    if installment is None:
        return
    plan = installment.plan
    row = next(row for row in plan.schedule() if row["installment"].pk == installment.pk)
    still_due = plan.status == PaymentPlan.Status.ACTIVE and row["state"] != "paid"
    if not (still_due and plan.email_reminders):
        # Changed since it was claimed: release it, so it is not shown as emailed.
        PaymentPlanInstallment.objects.filter(pk=installment.pk).update(reminder_emailed_at=None)
        return
    subject, body, summary = reminder_email(installment, installment.amount - row["applied"])
    try:
        sent = system_email.send_system_email(
            plan.invoice.contact,
            subject=subject,
            body=body,
            log_summary=summary,
            purpose="Payment reminder",
            matter=plan.invoice.matter,
        )
    except ValidationError as exc:
        reason = " ".join(exc.messages).rstrip(".")
    else:
        reason = system_email.not_sent_reason(sent)
    if reason:
        installment.reminder_email_error = reason[:255]
        installment.save(update_fields=["reminder_email_error"])
        note_on_staff_reminder(installment, f"The client's email reminder was not sent: {reason}.")
