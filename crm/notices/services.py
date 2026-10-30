from datetime import datetime, time

from django.db import transaction
from django.utils import timezone

from crm.models import Activity, AuditLog, WorkItem

from .models import CaseNotice, CaseStatusCheck, normalize_receipt_number, validate_receipt_number

RESPONSE_PRIORITY = {
    CaseNotice.Kind.RFE: WorkItem.Priority.HIGH,
    CaseNotice.Kind.NOID: WorkItem.Priority.URGENT,
}
# Responses are tracked to the end of the business day they are due.
DEADLINE_TIME = time(17)


def record(actor, action, target, detail=""):
    AuditLog.objects.create(
        user=actor,
        action=action,
        target_type=target.__class__.__name__,
        target_id=str(target.pk),
        detail=detail[:500],
    )


def notice_label(notice):
    parts = [notice.matter.matter_number, notice.form_type, notice.receipt_number]
    return " · ".join(part for part in parts if part)


def sync_item(notice, attr, *, when, **fields):
    """Create, move, or remove the work item a notice date drives.

    Completed items are left as they are.
    """
    item = getattr(notice, attr)
    if when is None:
        if item is not None and not item.is_completed:
            setattr(notice, attr, None)
            notice.save(update_fields=[attr])
            item.delete()
        return
    if item is None:
        item = WorkItem.objects.create(scheduled_for=when, **fields)
        setattr(notice, attr, item)
        notice.save(update_fields=[attr])
    elif not item.is_completed:
        for name, value in {"scheduled_for": when, **fields}.items():
            setattr(item, name, value)
        item.save()


def sync_work_items(notice, actor):
    matter = notice.matter
    owner = matter.assigned_to or actor
    due = notice.response_due_date
    sync_item(
        notice,
        "deadline",
        when=timezone.make_aware(datetime.combine(due, DEADLINE_TIME)) if due else None,
        title=f"{notice.get_kind_display()} response due · {notice_label(notice)}",
        description="Created from a USCIS notice. Changing the notice's due date moves this item.",
        kind=WorkItem.Kind.DEADLINE,
        priority=RESPONSE_PRIORITY.get(notice.kind, WorkItem.Priority.HIGH),
        contact=notice.contact,
        matter=matter,
        assigned_to=owner,
    )
    sync_item(
        notice,
        "appointment",
        when=notice.appointment_at,
        title=f"{notice.get_kind_display()} · {notice_label(notice)}",
        description=notice.appointment_location or "Created from a USCIS notice.",
        kind=WorkItem.Kind.APPOINTMENT,
        priority=WorkItem.Priority.NORMAL,
        contact=notice.contact,
        matter=matter,
        assigned_to=owner,
    )


def apply_to_matter(notice):
    """Fill the matter's receipt number and dates from a notice, never overwriting them."""
    matter = notice.matter
    changed = []
    if notice.kind == CaseNotice.Kind.RECEIPT:
        if notice.receipt_number and not matter.receipt_number:
            matter.receipt_number = notice.receipt_number
            changed.append("receipt_number")
        if notice.uscis_received_date and not matter.filing_date:
            matter.filing_date = notice.uscis_received_date
            changed.append("filing_date")
    if notice.priority_date and not matter.priority_date:
        matter.priority_date = notice.priority_date
        changed.append("priority_date")
    if changed:
        matter.save(update_fields=[*changed, "updated_at"])
    return changed


@transaction.atomic
def save_notice(notice, actor):
    """Save a new or edited notice and keep its deadline and appointment in Work."""
    creating = notice._state.adding
    notice.receipt_number = normalize_receipt_number(notice.receipt_number)
    if notice.contact_id is None:
        notice.contact = notice.matter.primary_contact
    if creating:
        notice.created_by = actor
    notice.save()
    sync_work_items(notice, actor)
    if creating:
        filled = [name.replace("_", " ") for name in apply_to_matter(notice)]
        body = str(notice)
        if filled:
            body += f"\nFilled in the matter's {', '.join(filled)}."
        Activity.objects.create(
            matter=notice.matter,
            contact=notice.contact,
            kind=Activity.Kind.SYSTEM,
            subject=f"{notice.get_kind_display()} logged",
            body=body,
            created_by=actor,
        )
        record(actor, "notice.logged", notice, str(notice))
    else:
        record(actor, "notice.updated", notice, str(notice))
    return notice


@transaction.atomic
def delete_notice(notice, actor):
    """Delete a notice and any of its work items that are still open."""
    open_items = [
        item for item in (notice.deadline, notice.appointment) if item and not item.is_completed
    ]
    record(actor, "notice.deleted", notice, str(notice))
    notice.delete()
    for item in open_items:
        item.delete()


@transaction.atomic
def record_status(
    matter,
    *,
    receipt_number,
    status_title,
    status_text="",
    checked_at=None,
    actor=None,
    source_connection=None,
):
    """Record a case status reading; a change from the last reading is noted on the matter."""
    receipt_number = normalize_receipt_number(receipt_number)
    validate_receipt_number(receipt_number)
    previous = CaseStatusCheck.objects.filter(receipt_number=receipt_number).first()
    check = CaseStatusCheck.objects.create(
        matter=matter,
        receipt_number=receipt_number,
        status_title=status_title.strip(),
        status_text=status_text.strip(),
        checked_at=checked_at or timezone.now(),
        changed=previous is not None and previous.status_title != status_title.strip(),
        recorded_by=actor,
        source_connection=source_connection,
    )
    if check.changed or previous is None:
        Activity.objects.create(
            matter=matter,
            contact=matter.primary_contact,
            kind=Activity.Kind.STATUS,
            subject=f"USCIS status for {receipt_number}: {check.status_title}",
            body=check.status_text,
            created_by=actor,
        )
    record(actor, "case_status.recorded", check, check.status_title)
    return check


def latest_statuses(matter):
    """The most recent status reading for each receipt number on the matter."""
    latest = {}
    for check in matter.status_checks.all():
        latest.setdefault(check.receipt_number, check)
    return list(latest.values())
