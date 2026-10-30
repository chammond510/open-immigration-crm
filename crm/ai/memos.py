"""Prep memos for upcoming consults, drafted from CRM records.

A consult is an open appointment with a contact who has not been retained yet (at
the Inquiry or Consultation stage), or one with "consult" in its title. The worker
drafts a memo for each consult in the next two days through the firm's background
connection, and drafts it again when the records it came from change. A staff member
can ask for a memo at any time through a connection they may use.

The memo goes to the attorney, never to the client, and is marked as an AI draft.
"""

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from crm.models import Contact, WorkItem

from . import features
from .client import AIUnavailable, Message, generate
from .models import AISettings, ConsultMemo
from .records import (
    DATA_RULES,
    as_json,
    clip,
    communication_data,
    contact_data,
    digest,
    intake_answers,
    matter_data,
    note_data,
    when,
    work_data,
)

FEATURE = features.register(
    features.Feature(
        "consult_memos",
        "Consult prep memos",
        "Before each consult in the next two days, drafts an internal memo for the attorney "
        "from the contact's CRM records: who they are, what they seem to want, what to ask, "
        "and what is missing.",
        background=True,
    )
)

WINDOW = timedelta(hours=48)
# At most this many memos are drafted in one background run.
PER_RUN = 10
# A failed memo is tried again in the background after this long.
RETRY_AFTER = timedelta(hours=1)
# A memo still being prepared after this long is treated as abandoned.
STALE_AFTER = timedelta(hours=1)
UNRETAINED = (Contact.Stage.INQUIRY, Contact.Stage.CONSULTATION)

INSTRUCTIONS = "\n\n".join(
    [
        "You help an immigration law firm prepare for consultations. Draft an internal prep "
        "memo for the attorney who will meet this person. It is not for the client.",
        DATA_RULES,
        "Cover, briefly: who the person is and what they seem to want; the known facts that "
        "matter for their immigration options; questions to ask and documents to request; "
        "issues to explore, such as past immigration history, time out of status, or "
        "criminal history, only where the records point to them, and phrased as questions "
        "rather than conclusions; and practical points such as language. Keep it under 400 "
        "words.",
        "Return a headline of at most 12 words, and the memo.",
    ]
)

SCHEMA = {
    "type": "object",
    "properties": {"headline": {"type": "string"}, "memo": {"type": "string"}},
    "required": ["headline", "memo"],
    "additionalProperties": False,
}


def is_consult(item):
    return bool(
        item.kind == WorkItem.Kind.APPOINTMENT
        and item.contact_id
        and not item.is_completed
        and (item.contact.stage in UNRETAINED or "consult" in item.title.lower())
    )


def upcoming_consults(now=None):
    now = now or timezone.now()
    return (
        WorkItem.objects.filter(
            kind=WorkItem.Kind.APPOINTMENT,
            is_completed=False,
            contact__isnull=False,
            scheduled_for__gte=now,
            scheduled_for__lt=now + WINDOW,
        )
        .filter(Q(contact__stage__in=UNRETAINED) | Q(title__icontains="consult"))
        .select_related("contact", "contact__assigned_to", "assigned_to")
        .order_by("scheduled_for")
    )


def memo_records(appointment):
    """Everything the memo is drafted from."""
    contact = appointment.contact
    return {
        "consult": {
            "title": appointment.title,
            "when": when(appointment.scheduled_for),
            "with": str(appointment.assigned_to or ""),
            "details": clip(appointment.description, 1500),
        },
        "contact": contact_data(contact),
        "intake_answers": intake_answers(contact),
        "matters": [
            matter_data(matter)
            for matter in contact.matters.select_related("primary_contact", "assigned_to")[:5]
        ],
        "open_work": [
            work_data(item)
            for item in contact.work_items.filter(is_completed=False)
            .exclude(pk=appointment.pk)
            .select_related("assigned_to")[:8]
        ],
        "recent_communications": [
            communication_data(item) for item in contact.communications.all()[:8]
        ],
        "recent_notes": [note_data(item) for item in contact.activities.all()[:8]],
    }


def is_preparing(memo, now=None):
    now = now or timezone.now()
    return memo.status == ConsultMemo.Status.PREPARING and memo.updated_at > now - STALE_AFTER


def needs_memo(appointment, records_digest, now=None):
    now = now or timezone.now()
    memo = ConsultMemo.objects.filter(appointment=appointment).first()
    if memo is None:
        return True
    if is_preparing(memo, now):
        return False
    if memo.status == ConsultMemo.Status.FAILED or memo.error:
        return memo.updated_at <= now - RETRY_AFTER
    return memo.source_digest != records_digest


def prepare(appointment, *, actor=None, connection=None, records=None):
    """Draft the memo now and return it; raise AIUnavailable if the request did not run.

    Without ``actor`` it is background work, through the firm's background connection.
    If drafting again fails, the earlier memo is kept and the error shown with it.
    """
    records = records or memo_records(appointment)
    memo, _ = ConsultMemo.objects.get_or_create(
        appointment=appointment, defaults={"contact": appointment.contact}
    )
    try:
        reply = generate(
            feature=FEATURE.key,
            system=INSTRUCTIONS,
            messages=[Message("user", as_json(records))],
            schema=SCHEMA,
            schema_name="consult_memo",
            actor=actor,
            connection=connection,
            max_tokens=2000,
        )
        body = clip((reply.data or {}).get("memo"), 20000)
        if not body:
            raise AIUnavailable("The model returned an empty memo.")
    except AIUnavailable as exc:
        memo.status = ConsultMemo.Status.READY if memo.body else ConsultMemo.Status.FAILED
        memo.error = str(exc)[:500]
        memo.save(update_fields=["status", "error", "updated_at"])
        raise
    memo.status = ConsultMemo.Status.READY
    memo.headline = clip((reply.data or {}).get("headline"), 255) or f"Consult: {appointment}"
    memo.body = body
    memo.model = reply.model[:120]
    memo.connection = connection if actor else AISettings.load().background_connection
    memo.requested_by = actor
    memo.source_digest = digest(records)
    memo.prepared_at = timezone.now()
    memo.error = ""
    memo.contact = appointment.contact
    memo.save()
    return memo


def prepare_due(now=None):
    """Draft memos for upcoming consults that need one; return how many were drafted.

    AIUnavailable stops the run, since the next request would fail the same way.
    """
    prepared = 0
    for appointment in upcoming_consults(now):
        records = memo_records(appointment)
        if not needs_memo(appointment, digest(records), now):
            continue
        prepare(appointment, records=records)
        prepared += 1
        if prepared >= PER_RUN:
            break
    return prepared
