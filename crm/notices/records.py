"""Record the case status readings a case status connector reports on their matters.

A reading that changes a receipt's status creates a review task for the matter's
staff. The task only asks staff to review the change: the CRM does not interpret
it or tell the client. A receipt number the source could not answer for is kept
as a problem on the matter until a status is recorded for it.
"""

import logging
from dataclasses import replace

from django.db import transaction
from django.utils import timezone

from crm.connect.records import CaseStatusRecord
from crm.connect.signals import records_received
from crm.models import Matter, WorkItem

from .alerts import higher_priority, review_for
from .models import CaseStatusCheck, CaseStatusProblem, normalize_receipt_number
from .services import record_status
from .tracking import receipt_matters

logger = logging.getLogger(__name__)

REVIEW_NOTE = "Review this update before telling the client what it means."


def matter_for(receipt_number, owners=None):
    """The one matter with this receipt number on it or on one of its notices."""
    owners = receipt_matters() if owners is None else owners
    ids = owners.get(receipt_number, set())
    return Matter.objects.filter(pk=next(iter(ids))).first() if len(ids) == 1 else None


def reviewer(matter):
    for user in (matter.assigned_to, matter.primary_contact.assigned_to):
        if user is not None and user.is_active:
            return user
    return None


def open_review_task(receipt_number):
    """The open review task from an earlier change to this receipt, if any."""
    task_ids = CaseStatusCheck.objects.filter(
        receipt_number=receipt_number, review_task__isnull=False
    ).values("review_task")
    return WorkItem.objects.filter(pk__in=task_ids, is_completed=False).order_by("-pk").first()


def request_review(check, previous_title):
    """Create a review task for a changed status, or update the one still open."""
    review = review_for(check.status_title)
    if review is None:
        return None
    matter = check.matter
    checked = timezone.localtime(check.checked_at)
    line = (
        f"{checked:%b} {checked.day}: receipt {check.receipt_number} changed "
        f'from "{previous_title}" to "{check.status_title}".'
    )
    task = open_review_task(check.receipt_number)
    if task is None:
        task = WorkItem(
            kind=WorkItem.Kind.TASK,
            matter=matter,
            contact=matter.primary_contact,
            assigned_to=reviewer(matter),
            scheduled_for=timezone.now(),
            priority=review.priority,
            description=f"{line}\n\n{REVIEW_NOTE}",
        )
    else:
        task.priority = higher_priority(task.priority, review.priority)
        task.description = f"{line}\n{task.description}"
    task.title = f"{review.title} · {matter.matter_number} · {check.receipt_number}"[:255]
    task.save()
    check.review_task = task
    check.save(update_fields=["review_task"])
    return task


@transaction.atomic
def store_reading(matter, record, connection):
    previous = CaseStatusCheck.objects.filter(receipt_number=record.receipt_number).first()
    check = record_status(
        matter,
        receipt_number=record.receipt_number,
        status_title=record.status_title,
        status_text=record.status_text,
        checked_at=record.occurred_at,
        source_connection=connection,
    )
    if check.changed:
        request_review(check, previous.status_title)
    return check


def store_problem(matter, record, connection):
    problem, created = CaseStatusProblem.objects.get_or_create(
        receipt_number=record.receipt_number,
        defaults={
            "matter": matter,
            "kind": record.problem,
            "first_seen_at": record.occurred_at,
            "last_seen_at": record.occurred_at,
        },
    )
    if not created and problem.kind != record.problem:
        problem.first_seen_at = record.occurred_at
    problem.matter = matter
    problem.kind = record.problem
    problem.detail = record.status_text[:2000]
    problem.last_seen_at = record.occurred_at
    problem.source_connection = connection
    problem.save()
    return problem


def store_records(sender, connection, capability, records, **kwargs):
    owners = None
    for record in records:
        if not isinstance(record, CaseStatusRecord):
            continue
        try:
            owners = receipt_matters() if owners is None else owners
            record = replace(record, receipt_number=normalize_receipt_number(record.receipt_number))
            matter = matter_for(record.receipt_number, owners)
            if matter is None:
                continue
            if record.problem:
                store_problem(matter, record, connection)
            else:
                store_reading(matter, record, connection)
        except Exception:
            # The record holds a receipt number, so log only what identifies the connection.
            logger.exception(
                "Could not store a case status record from connection %s", connection.pk
            )


def connect():
    records_received.connect(store_records, dispatch_uid="notices-store-records")
