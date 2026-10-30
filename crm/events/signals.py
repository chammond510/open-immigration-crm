"""Model signals that emit CRM events, so every path (web pages, the API,
connectors, the admin) is covered without each one remembering to emit."""

from django.db.models.signals import post_init, post_save

from crm.billing.models import Invoice, Payment
from crm.comms.models import Communication
from crm.models import Activity, Contact, IntakeSubmission, Matter, WorkItem
from crm.notices.models import CaseNotice, CaseStatusCheck

from .services import emit

# Fields whose change is its own event; their loaded values are kept for comparison.
TRACKED = {Matter: "status", WorkItem: "is_completed", Invoice: "status"}


def remember(sender, instance, **kwargs):
    # Read from __dict__ so a deferred field is never loaded just for this.
    instance._events_before = instance.__dict__.get(TRACKED[sender])


def previous_and_current(sender, instance):
    field = TRACKED[sender]
    before = getattr(instance, "_events_before", None)
    after = getattr(instance, field)
    instance._events_before = after
    return before, after


def contact_saved(sender, instance, created, raw=False, **kwargs):
    if not raw:
        emit("contact.created" if created else "contact.updated", instance)


def matter_saved(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    before, after = previous_and_current(sender, instance)
    emit("matter.created" if created else "matter.updated", instance)
    if not created and before is not None and before != after:
        emit(
            "matter.status_changed",
            instance,
            changes={"status": {"from": before, "to": after}},
        )


def work_item_saved(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    before, after = previous_and_current(sender, instance)
    if created:
        emit("work_item.created", instance)
    if after and (created or not before):
        emit("work_item.completed", instance)


def activity_saved(sender, instance, created, raw=False, **kwargs):
    if created and not raw and instance.kind == Activity.Kind.NOTE:
        emit("note.created", instance)


def communication_saved(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        emit("communication.logged", instance)


def notice_saved(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        emit("notice.logged", instance)


def status_check_saved(sender, instance, created, raw=False, **kwargs):
    if not created or raw:
        return
    first = (
        not CaseStatusCheck.objects.filter(
            matter_id=instance.matter_id, receipt_number=instance.receipt_number
        )
        .exclude(pk=instance.pk)
        .exists()
    )
    if instance.changed or first:
        emit("case_status.changed", instance)


def submission_saved(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        emit("intake.submitted", instance)


def invoice_saved(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    before, after = previous_and_current(sender, instance)
    if after == Invoice.Status.ISSUED and before == Invoice.Status.DRAFT:
        emit("invoice.issued", instance)


def payment_saved(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        emit("payment.recorded", instance)


def connect():
    for model in TRACKED:
        post_init.connect(remember, sender=model, dispatch_uid=f"events-remember-{model.__name__}")
    for model, receiver in (
        (Contact, contact_saved),
        (Matter, matter_saved),
        (WorkItem, work_item_saved),
        (Activity, activity_saved),
        (Communication, communication_saved),
        (CaseNotice, notice_saved),
        (CaseStatusCheck, status_check_saved),
        (IntakeSubmission, submission_saved),
        (Invoice, invoice_saved),
        (Payment, payment_saved),
    ):
        post_save.connect(receiver, sender=model, dispatch_uid=f"events-{model.__name__}")
