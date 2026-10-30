"""Follow-up tasks suggested from new communications.

The worker reviews the calls, texts, emails, and meetings logged since its last run,
grouped by matter (or by contact when there is no matter), and asks the firm's
background connection what the firm needs to do next. Each suggestion waits for a
staff member, who accepts it by creating the task, after changing it if they like,
or dismisses it. Nothing is created until then.

A suggestion much like an open task, or like one made or dismissed recently, is not
made again. Suggestions nobody reviews expire after two weeks.
"""

from datetime import datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone

from crm.comms.models import Communication
from crm.models import WorkItem

from . import features
from .client import Message, generate
from .models import TaskSuggestion
from .records import (
    DATA_RULES,
    as_json,
    clip,
    communication_data,
    contact_data,
    matter_data,
    similar,
    work_data,
)

FEATURE = features.register(
    features.Feature(
        "task_suggestions",
        "Task suggestions",
        "Reviews new calls, texts, emails, and meetings and suggests follow-up tasks. Staff "
        "accept or dismiss each one; nothing is created until they accept it.",
        background=True,
    )
)

# On the first run, communications logged this far back are reviewed.
FIRST_LOOKBACK = timedelta(days=2)
# At most this many communications are reviewed in one run; the rest wait for the next.
PER_RUN = 30
PER_GROUP = 3
EXPIRES_AFTER = timedelta(days=14)
REMEMBER_DISMISSED = timedelta(days=30)
# Accepted tasks are due at this time of day, firm time.
DUE_AT = time(9, 0)

INSTRUCTIONS = "\n\n".join(
    [
        "You help an immigration law firm keep up with follow-ups. From the new "
        "communications below, suggest the tasks the firm's staff should do next.",
        DATA_RULES,
        "Suggest only concrete next steps the firm itself must take, not work that is up "
        "to the client or anyone else. Skip anything the open work already covers. Suggest "
        f"at most {PER_GROUP}, and return an empty list when nothing is needed.",
        "For each: a short task title in the imperative; the reason, naming the "
        "communication; a priority; the number of days from today it should be done in, "
        "0 for today; a short restatement of the work, used to spot duplicates; and the "
        "number of the communication it comes from.",
    ]
)

SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "priority": {"type": "string", "enum": WorkItem.Priority.values},
                    "due_in_days": {"type": "integer"},
                    "intent": {"type": "string"},
                    "communication": {"type": "integer"},
                },
                "required": [
                    "title",
                    "reason",
                    "priority",
                    "due_in_days",
                    "intent",
                    "communication",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["suggestions"],
    "additionalProperties": False,
}


def expire(now=None):
    now = now or timezone.now()
    return TaskSuggestion.objects.filter(
        status=TaskSuggestion.Status.PENDING, expires_at__lte=now
    ).update(status=TaskSuggestion.Status.EXPIRED, decided_at=now)


def new_communications(since, until):
    """Communications to review, oldest first, and where the next run starts."""
    items = list(
        Communication.objects.filter(
            created_at__gt=since, created_at__lte=until, contact__isnull=False
        )
        .exclude(subject="", summary="")
        .select_related("contact", "contact__assigned_to", "matter", "matter__assigned_to")
        .order_by("created_at", "pk")[: PER_RUN + 1]
    )
    if len(items) > PER_RUN:
        items = items[:PER_RUN]
        return items, items[-1].created_at
    return items, until


def grouped(items):
    groups = {}
    for item in items:
        key = ("matter", item.matter_id) if item.matter_id else ("contact", item.contact_id)
        groups.setdefault(key, []).append(item)
    return list(groups.values())


def open_work_for(contact, matter):
    work = (matter or contact).work_items.filter(is_completed=False)
    return list(work.select_related("assigned_to")[:10])


def is_repeat(intent, title, contact, matter, open_work, now):
    """Whether this suggestion repeats open work or a recent suggestion."""
    if any(similar(intent, item.title) or similar(title, item.title) for item in open_work):
        return True
    earlier = TaskSuggestion.objects.filter(contact=contact, matter=matter).filter(
        created_at__gte=now - REMEMBER_DISMISSED
    )
    return any(
        similar(intent, suggestion.intent) or similar(title, suggestion.title)
        for suggestion in earlier.only("intent", "title")
    )


def suggest_for(group, now):
    """Ask about one matter's or contact's new communications; return the suggestions made."""
    first = group[0]
    contact, matter = first.contact, first.matter
    open_work = open_work_for(contact, matter)
    records = {
        "today": timezone.localdate(now).isoformat(),
        "contact": contact_data(contact),
        "matter": matter_data(matter) if matter else None,
        "open_work": [work_data(item) for item in open_work],
        "new_communications": [
            {"number": index, **communication_data(item)}
            for index, item in enumerate(group[-8:], start=1)
        ],
    }
    reply = generate(
        feature=FEATURE.key,
        system=INSTRUCTIONS,
        messages=[Message("user", as_json(records))],
        schema=SCHEMA,
        schema_name="task_suggestions",
        max_tokens=1500,
    )
    made = []
    for proposed in ((reply.data or {}).get("suggestions") or [])[:PER_GROUP]:
        if not isinstance(proposed, dict):
            continue
        title = clip(proposed.get("title"), 255)
        intent = clip(proposed.get("intent") or title, 255)
        if not title or is_repeat(intent, title, contact, matter, open_work, now):
            continue
        number = proposed.get("communication")
        source = group[-8:][number - 1] if isinstance(number, int) and 0 < number <= 8 else None
        days = proposed.get("due_in_days")
        days = min(max(days, 0), 60) if isinstance(days, int) else 1
        priority = proposed.get("priority")
        made.append(
            TaskSuggestion.objects.create(
                contact=contact,
                matter=matter,
                source_communication=source,
                title=title,
                reason=clip(proposed.get("reason"), 2000),
                priority=priority
                if priority in WorkItem.Priority.values
                else WorkItem.Priority.NORMAL,
                due_date=timezone.localdate(now) + timedelta(days=days),
                assigned_to=(matter.assigned_to if matter else None) or contact.assigned_to,
                intent=intent,
                model=reply.model[:120],
                expires_at=now + EXPIRES_AFTER,
            )
        )
    return made


def suggest(since, now=None):
    """Review communications logged after ``since``; return (suggestions made, new cursor).

    AIUnavailable stops the run before the cursor moves, so nothing is skipped.
    """
    now = now or timezone.now()
    expire(now)
    items, cursor = new_communications(since or now - FIRST_LOOKBACK, now)
    made = 0
    for group in grouped(items):
        made += len(suggest_for(group, now))
    return made, cursor


def pending_for(user=None, contact=None, matter=None):
    suggestions = TaskSuggestion.objects.filter(
        status=TaskSuggestion.Status.PENDING, expires_at__gt=timezone.now()
    ).select_related("contact", "matter", "assigned_to", "source_communication")
    if matter is not None:
        return suggestions.filter(matter=matter)
    if contact is not None:
        return suggestions.filter(contact=contact)
    if user is not None:
        return suggestions.filter(Q(assigned_to=user) | Q(assigned_to__isnull=True))
    return suggestions


def task_values(suggestion):
    """The new task's starting values; the staff member can change any of them."""
    source = suggestion.source_communication
    lines = ["Suggested by AI from new communications. Review before relying on it."]
    if source:
        lines.append(
            f"From: {source.get_channel_display()} on "
            f"{timezone.localtime(source.occurred_at):%b %-d, %Y}"
            f"{f' · {source.subject}' if source.subject else ''}"
        )
    if suggestion.reason:
        lines.append(f"Why: {suggestion.reason}")
    due = None
    if suggestion.due_date:
        due = timezone.make_aware(datetime.combine(suggestion.due_date, DUE_AT))
    return {
        "kind": WorkItem.Kind.TASK,
        "title": suggestion.title,
        "description": "\n".join(lines),
        "priority": suggestion.priority,
        "contact": suggestion.contact,
        "matter": suggestion.matter,
        "assigned_to": suggestion.assigned_to,
        "scheduled_for": due,
    }
