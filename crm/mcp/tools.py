"""The tools the MCP server offers. Each runs as the signed-in staff member, with that
person's permissions, through the same forms and services as the web pages."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from crm.api import serializers
from crm.api.forms import NoteForm
from crm.api.http import check_form
from crm.billing.models import Invoice
from crm.comms.forms import CommunicationForm
from crm.comms.models import Communication
from crm.comms.sharing import shareable
from crm.events.context import event_origin
from crm.forms import WorkItemForm
from crm.models import Activity, Contact, Matter, TimeEntry, WorkItem
from crm.notices.forms import StatusCheckForm
from crm.notices.models import CaseNotice
from crm.notices.services import latest_statuses, record_status
from crm.reports import periods
from crm.reports.metrics import build_report
from crm.reports.serializers import report_data
from crm.search import CONTACT_SEARCH_FIELDS, MATTER_SEARCH_FIELDS, words_match
from crm.services import audit

MAX_LIMIT = 50


class ToolError(Exception):
    """A problem the assistant can act on.

    ``message`` is written by this module for the assistant to read, and is the only
    part of the exception returned to it.
    """

    def __init__(self, message):
        super().__init__(message)
        self.message = message


@dataclass
class Context:
    request: object
    user: object
    client_name: str = ""

    def audit(self, action, target):
        audit(self.request, action, target, self.via()[:500])

    def via(self):
        return f"Through MCP client {self.client_name}"

    def origin(self):
        return {"via": "mcp", "client": self.client_name}


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    handler: Callable
    properties: dict = field(default_factory=dict)
    required: tuple = ()
    write: bool = False
    billing: bool = False

    def definition(self):
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": self.properties,
                "required": list(self.required),
                "additionalProperties": False,
            },
            "annotations": {
                "title": self.title,
                "readOnlyHint": not self.write,
                # Write tools only add records or mark work done; nothing is deleted.
                "destructiveHint": False,
                "idempotentHint": not self.write,
                "openWorldHint": False,
            },
        }


# Argument helpers. Each raises ToolError with a message the assistant can fix.


def text(args, name, *, required=False, limit=500):
    value = args.get(name)
    if value is None or value == "":
        if required:
            raise ToolError(f"{name} is required.")
        return ""
    if not isinstance(value, str):
        raise ToolError(f"{name} must be text.")
    value = value.strip()
    if len(value) > limit:
        raise ToolError(f"{name} must be at most {limit} characters.")
    return value


def number(args, name, *, default, low, high):
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ToolError(f"{name} must be a whole number from {low} to {high}.")
    return value


def flag(args, name, default=False):
    value = args.get(name, default)
    if not isinstance(value, bool):
        raise ToolError(f"{name} must be true or false.")
    return value


def choice(args, name, choices):
    value = args.get(name)
    if value in (None, ""):
        return ""
    if value not in choices:
        raise ToolError(f"{name} must be one of: {', '.join(choices)}.")
    return value


def record(args, name, model, *, required=False):
    value = args.get(name)
    if value in (None, ""):
        if required:
            raise ToolError(f"{name} is required.")
        return None
    try:
        key = uuid.UUID(str(value)) if model in (Contact, Matter) else int(value)
        return model.objects.get(pk=key)
    except (ValueError, TypeError, model.DoesNotExist):
        raise ToolError(f"No {model._meta.verbose_name} has the {name} {value}.") from None


def timestamp(args, name):
    value = args.get(name)
    if value in (None, ""):
        return None
    try:
        parsed = parse_datetime(str(value))
    except ValueError:
        parsed = None
    if parsed is None:
        raise ToolError(f"{name} must be an ISO 8601 date and time, such as 2026-03-02T09:00:00Z.")
    return parsed


def contact_or_matter(args):
    contact = record(args, "contact_id", Contact)
    matter = record(args, "matter_id", Matter)
    if not contact and not matter:
        raise ToolError("Give contact_id or matter_id.")
    return contact, matter


# Compact shapes for lists; full records reuse the API's serializers.


def contact_brief(contact, request):
    return {
        "id": str(contact.pk),
        "name": contact.full_name,
        "email": contact.email,
        "phone": contact.phone,
        "stage": contact.stage,
        "web_url": serializers.web_url(request, "contact_detail", contact.pk),
    }


def matter_brief(matter, request):
    return {
        "id": str(matter.pk),
        "matter_number": matter.matter_number,
        "title": matter.title,
        "case_type": matter.case_type,
        "status": matter.status,
        "primary_contact": matter.primary_contact.full_name,
        "receipt_number": matter.receipt_number,
        "web_url": serializers.web_url(request, "matter_detail", matter.pk),
    }


def work_brief(item):
    return {
        "id": item.pk,
        "kind": item.kind,
        "title": item.title,
        "priority": item.priority,
        "scheduled_for": serializers.iso(item.scheduled_for),
        "is_completed": item.is_completed,
        "is_overdue": item.is_overdue,
        "matter": item.matter.matter_number if item.matter_id else None,
        "contact": item.contact.full_name if item.contact_id else None,
        "assigned_to": serializers.user_name(item.assigned_to),
    }


def note_brief(note):
    return {
        "kind": note.kind,
        "subject": note.subject,
        "body": note.body[:2000],
        "occurred_at": serializers.iso(note.occurred_at),
    }


def communication_brief(item):
    return {
        "channel": item.channel,
        "direction": item.direction,
        "occurred_at": serializers.iso(item.occurred_at),
        "counterpart": item.counterpart,
        "subject": item.subject,
        "summary": item.summary[:2000],
    }


def notice_brief(notice):
    return {
        "id": str(notice.pk),
        "matter": notice.matter.matter_number,
        "kind": notice.kind,
        "form_type": notice.form_type,
        "receipt_number": notice.receipt_number,
        "notice_date": serializers.iso(notice.notice_date),
        "response_due_date": serializers.iso(notice.response_due_date),
        "appointment_at": serializers.iso(notice.appointment_at),
    }


def open_work(queryset):
    return [
        work_brief(item)
        for item in queryset.filter(is_completed=False).select_related(
            "matter", "contact", "assigned_to"
        )[:20]
    ]


# Read tools


def search_contacts(ctx, args):
    query = text(args, "query", required=True, limit=100)
    limit = number(args, "limit", default=10, low=1, high=MAX_LIMIT)
    contacts = Contact.objects.filter(words_match(query, CONTACT_SEARCH_FIELDS))[:limit]
    return {"contacts": [contact_brief(c, ctx.request) for c in contacts]}


def search_matters(ctx, args):
    query = text(args, "query", limit=100)
    status = choice(args, "status", Matter.Status.values)
    limit = number(args, "limit", default=10, low=1, high=MAX_LIMIT)
    matters = Matter.objects.select_related("primary_contact")
    if query:
        matters = matters.filter(words_match(query, MATTER_SEARCH_FIELDS))
    if status:
        matters = matters.filter(status=status)
    return {"matters": [matter_brief(m, ctx.request) for m in matters[:limit]]}


def get_contact(ctx, args):
    contact = record(args, "contact_id", Contact, required=True)
    return {
        "contact": serializers.contact(contact, ctx.request),
        "matters": [
            matter_brief(m, ctx.request)
            for m in contact.matters.select_related("primary_contact")[:20]
        ],
        "open_work": open_work(contact.work_items),
        "recent_communications": [
            communication_brief(c) for c in shareable(contact.communications.all())[:5]
        ],
        "recent_notes": [note_brief(n) for n in contact.activities.all()[:5]],
    }


def get_matter(ctx, args):
    matter = record(args, "matter_id", Matter, required=True)
    checklist = list(matter.checklist.all())
    return {
        "matter": serializers.matter(matter, ctx.request),
        "checklist": {
            "done": sum(item.is_completed for item in checklist),
            "total": len(checklist),
            "open_items": [item.title for item in checklist if not item.is_completed][:20],
        },
        "open_work": open_work(matter.work_items),
        "notices": [notice_brief(n) for n in matter.notices.select_related("matter")[:20]],
        "case_status": [
            serializers.status_check(check, ctx.request) for check in latest_statuses(matter)
        ],
        "recent_notes": [note_brief(n) for n in matter.activities.all()[:5]],
        "recent_communications": [
            communication_brief(c) for c in shareable(matter.communications.all())[:5]
        ],
        "saved_authorities": [
            {
                "citation": authority.citation,
                "title": authority.title,
                "court": authority.court,
                "decided": serializers.iso(authority.decided),
                "standing": authority.standing,
                "url": authority.url,
                "note": authority.note[:1000],
            }
            for authority in matter.authorities.all()[:20]
        ],
    }


def list_upcoming_work(ctx, args):
    days = number(args, "days", default=14, low=1, high=90)
    kind = choice(args, "kind", WorkItem.Kind.values)
    now = timezone.now()
    items = WorkItem.objects.filter(
        is_completed=False, scheduled_for__lt=now + timedelta(days=days)
    )
    if not flag(args, "include_overdue", True):
        items = items.filter(scheduled_for__gte=now)
    if flag(args, "assigned_to_me"):
        items = items.filter(assigned_to=ctx.user)
    if kind:
        items = items.filter(kind=kind)
    items = items.select_related("matter", "contact", "assigned_to").order_by("scheduled_for")
    return {"work_items": [work_brief(item) for item in items[:MAX_LIMIT]]}


def list_notices(ctx, args):
    notices = CaseNotice.objects.select_related("matter")
    matter = record(args, "matter_id", Matter)
    if matter:
        notices = notices.filter(matter=matter)
    kind = choice(args, "kind", CaseNotice.Kind.values)
    if kind:
        notices = notices.filter(kind=kind)
    if flag(args, "open_responses_only"):
        notices = notices.filter(
            response_due_date__isnull=False, deadline__is_completed=False
        ).order_by("response_due_date")
    return {"notices": [notice_brief(n) for n in notices[:MAX_LIMIT]]}


def list_communications(ctx, args):
    items = shareable(Communication.objects.all())
    contact = record(args, "contact_id", Contact)
    matter = record(args, "matter_id", Matter)
    if contact:
        items = items.filter(contact=contact)
    if matter:
        items = items.filter(matter=matter)
    channel = choice(args, "channel", Communication.Channel.values)
    if channel:
        items = items.filter(channel=channel)
    limit = number(args, "limit", default=20, low=1, high=MAX_LIMIT)
    return {"communications": [communication_brief(c) for c in items[:limit]]}


def list_time_entries(ctx, args):
    days = number(args, "days", default=30, low=1, high=366)
    entries = TimeEntry.objects.filter(
        stopped_at__isnull=False, started_at__gte=timezone.now() - timedelta(days=days)
    ).select_related("matter", "contact", "user")
    matter = record(args, "matter_id", Matter)
    if matter:
        entries = entries.filter(matter=matter)
    if flag(args, "mine_only", True):
        entries = entries.filter(user=ctx.user)
    rows = list(entries[:200])
    minutes = sum(int((e.stopped_at - e.started_at).total_seconds() // 60) for e in rows)
    return {
        "total_hours": round(minutes / 60, 1),
        "entries": [
            {
                "started_at": serializers.iso(e.started_at),
                "minutes": int((e.stopped_at - e.started_at).total_seconds() // 60),
                "user": serializers.user_name(e.user),
                "matter": e.matter.matter_number if e.matter_id else None,
                "contact": e.contact.full_name if e.contact_id else None,
                "note": e.note,
            }
            for e in rows
        ],
    }


def operating_summary(ctx, args):
    period = periods.resolve(choice(args, "period", [key for key, _ in periods.CHOICES[:-1]]))
    include_billing = ctx.user.has_perm("billing.access_billing")
    return report_data(build_report(period, include_billing=include_billing))


def list_invoices(ctx, args):
    invoices = Invoice.objects.select_related("contact", "matter").prefetch_related(
        "lines", "payments"
    )
    contact = record(args, "contact_id", Contact)
    matter = record(args, "matter_id", Matter)
    if contact:
        invoices = invoices.filter(contact=contact)
    if matter:
        invoices = invoices.filter(matter=matter)
    status = choice(args, "status", Invoice.Status.values)
    if status:
        invoices = invoices.filter(status=status)
    return {
        "invoices": [
            {
                "number": invoice.number or "Draft",
                "status": invoice.status,
                "client": invoice.contact.full_name,
                "matter": invoice.matter.matter_number if invoice.matter_id else None,
                "issue_date": serializers.iso(invoice.issue_date),
                "due_date": serializers.iso(invoice.due_date),
                "total": serializers.money(invoice.subtotal),
                "balance": serializers.money(invoice.balance),
                "web_url": serializers.web_url(ctx.request, "invoice_detail", invoice.pk),
            }
            for invoice in invoices[:MAX_LIMIT]
        ]
    }


# Write tools. Each runs in a transaction and is marked as coming through MCP.


def form_values(form_class, data, writable, **kwargs):
    form, problem = check_form(form_class, data, writable=writable, **kwargs)
    if problem:
        detail = f" {problem['fields']}" if problem.get("fields") else ""
        raise ToolError(f"{problem['message']}{detail}")
    return form


def writing(ctx):
    return event_origin(**ctx.origin())


def create_task(ctx, args):
    contact, matter = contact_or_matter(args)
    data = {
        "title": text(args, "title", required=True, limit=255),
        "description": text(args, "description", limit=5000),
        "kind": choice(args, "kind", WorkItem.Kind.values) or WorkItem.Kind.TASK,
        "priority": choice(args, "priority", WorkItem.Priority.values) or WorkItem.Priority.NORMAL,
        "contact": str(contact.pk) if contact else None,
        "matter": str(matter.pk) if matter else None,
        "scheduled_for": timestamp(args, "due_at"),
    }
    with writing(ctx), transaction.atomic():
        item = form_values(
            WorkItemForm, data, list(data), initial={"assigned_to": ctx.user.pk}
        ).save()
        ctx.audit("work_item.created", item)
    return {"created": work_brief(item)}


def add_note(ctx, args):
    contact, matter = contact_or_matter(args)
    data = {
        "kind": Activity.Kind.NOTE,
        "subject": text(args, "subject", required=True, limit=255),
        "body": text(args, "body", limit=10000),
        "contact": str(contact.pk) if contact else None,
        "matter": str(matter.pk) if matter else None,
    }
    with writing(ctx), transaction.atomic():
        note = form_values(NoteForm, data, list(data)).save(commit=False)
        note.created_by = ctx.user
        note.save()
        ctx.audit("activity.created", note)
    return {"created": serializers.note(note, ctx.request)}


def log_communication(ctx, args):
    contact, matter = contact_or_matter(args)
    data = {
        "channel": choice(args, "channel", Communication.Channel.values),
        "direction": choice(args, "direction", Communication.Direction.values),
        "occurred_at": timestamp(args, "occurred_at") or timezone.now(),
        "duration_minutes": number(args, "duration_minutes", default=0, low=0, high=24 * 60)
        or None,
        "counterpart": text(args, "counterpart", limit=255),
        "subject": text(args, "subject", limit=255),
        "summary": text(args, "summary", limit=10000),
        "contact": str(contact.pk) if contact else None,
        "matter": str(matter.pk) if matter else None,
    }
    if not data["channel"]:
        raise ToolError("channel is required.")
    with writing(ctx), transaction.atomic():
        item = form_values(CommunicationForm, data, list(data)).save(commit=False)
        item.logged_by = ctx.user
        item.save()
        ctx.audit("communication.logged", item)
    return {"created": serializers.communication(item, ctx.request)}


def record_status_check(ctx, args):
    matter = record(args, "matter_id", Matter, required=True)
    data = {
        "receipt_number": text(args, "receipt_number", required=True, limit=20),
        "status_title": text(args, "status_title", required=True, limit=255),
        "status_text": text(args, "status_text", limit=5000),
    }
    with writing(ctx), transaction.atomic():
        form = form_values(StatusCheckForm, data, list(data))
        check = record_status(matter, actor=ctx.user, **form.cleaned_data)
    return {"recorded": serializers.status_check(check, ctx.request)}


def complete_work_item(ctx, args):
    item = record(args, "work_item_id", WorkItem, required=True)
    if item.is_completed:
        return {"completed": work_brief(item), "note": "It was already complete."}
    with writing(ctx), transaction.atomic():
        item.is_completed = True
        item.save(update_fields=["is_completed", "completed_at", "updated_at"])
        ctx.audit("work_item.toggled", item)
    return {"completed": work_brief(item)}


ID = {"type": "string", "description": "A record ID from a search or list tool."}
LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}

TOOLS = [
    Tool(
        "search_contacts",
        "Search contacts",
        "Find contacts by full or partial name, email, phone, or A-number. Every word "
        "must match, so a full name narrows the search.",
        search_contacts,
        {"query": {"type": "string", "description": "Text to search for."}, "limit": LIMIT},
        ("query",),
    ),
    Tool(
        "search_matters",
        "Search matters",
        "Find matters by matter number, title, receipt number, or client name. Every "
        "word must match.",
        search_matters,
        {
            "query": {"type": "string"},
            "status": {"type": "string", "enum": Matter.Status.values},
            "limit": LIMIT,
        },
    ),
    Tool(
        "get_contact",
        "Contact summary",
        "A contact's details, matters, open work, and recent communications and notes.",
        get_contact,
        {"contact_id": ID},
        ("contact_id",),
    ),
    Tool(
        "get_matter",
        "Matter summary",
        "A matter's details, parties, workflow phase, checklist progress, open work, USCIS "
        "notices, latest case status, recent notes and communications, and saved legal "
        "authorities.",
        get_matter,
        {"matter_id": ID},
        ("matter_id",),
    ),
    Tool(
        "list_upcoming_work",
        "Upcoming work",
        "Open tasks, deadlines, and appointments due in the next days, oldest first.",
        list_upcoming_work,
        {
            "days": {"type": "integer", "minimum": 1, "maximum": 90},
            "kind": {"type": "string", "enum": WorkItem.Kind.values},
            "assigned_to_me": {"type": "boolean"},
            "include_overdue": {"type": "boolean"},
        },
    ),
    Tool(
        "list_notices",
        "USCIS notices",
        "USCIS notices, optionally for one matter, one kind, or only open RFE and NOID responses.",
        list_notices,
        {
            "matter_id": ID,
            "kind": {"type": "string", "enum": CaseNotice.Kind.values},
            "open_responses_only": {"type": "boolean"},
        },
    ),
    Tool(
        "list_communications",
        "Communications",
        "Logged calls, texts, emails, and meetings, newest first.",
        list_communications,
        {
            "contact_id": ID,
            "matter_id": ID,
            "channel": {"type": "string", "enum": Communication.Channel.values},
            "limit": LIMIT,
        },
    ),
    Tool(
        "list_time_entries",
        "Time entries",
        "Logged time over the last days, by default only your own.",
        list_time_entries,
        {
            "days": {"type": "integer", "minimum": 1, "maximum": 366},
            "matter_id": ID,
            "mine_only": {"type": "boolean"},
        },
    ),
    Tool(
        "operating_summary",
        "Operating summary",
        "The operating dashboard's figures for a period: intake, matters, deadlines, USCIS "
        "notices, communications, time, and billing for staff with billing access.",
        operating_summary,
        {"period": {"type": "string", "enum": [key for key, _ in periods.CHOICES[:-1]]}},
    ),
    Tool(
        "list_invoices",
        "Invoices",
        "Invoices with totals and balances, optionally for one contact or matter.",
        list_invoices,
        {
            "contact_id": ID,
            "matter_id": ID,
            "status": {"type": "string", "enum": Invoice.Status.values},
        },
        billing=True,
    ),
    Tool(
        "create_task",
        "Create a task",
        "Add a task, deadline, or appointment to a contact or matter, assigned to you.",
        create_task,
        {
            "title": {"type": "string"},
            "matter_id": ID,
            "contact_id": ID,
            "kind": {"type": "string", "enum": WorkItem.Kind.values},
            "priority": {"type": "string", "enum": WorkItem.Priority.values},
            "due_at": {"type": "string", "description": "ISO 8601 date and time."},
            "description": {"type": "string"},
        },
        ("title",),
        write=True,
    ),
    Tool(
        "add_note",
        "Add a note",
        "Add a note to a contact or matter.",
        add_note,
        {
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "matter_id": ID,
            "contact_id": ID,
        },
        ("subject",),
        write=True,
    ),
    Tool(
        "log_communication",
        "Log a communication",
        "Record a call, text, email, or meeting that already happened.",
        log_communication,
        {
            "channel": {"type": "string", "enum": Communication.Channel.values},
            "direction": {"type": "string", "enum": Communication.Direction.values},
            "contact_id": ID,
            "matter_id": ID,
            "occurred_at": {"type": "string", "description": "ISO 8601 date and time."},
            "duration_minutes": {"type": "integer", "minimum": 0, "maximum": 1440},
            "counterpart": {"type": "string"},
            "subject": {"type": "string"},
            "summary": {"type": "string"},
        },
        ("channel",),
        write=True,
    ),
    Tool(
        "record_status_check",
        "Record a case status",
        "Record a USCIS case status reading for a receipt number on a matter.",
        record_status_check,
        {
            "matter_id": ID,
            "receipt_number": {"type": "string"},
            "status_title": {"type": "string"},
            "status_text": {"type": "string"},
        },
        ("matter_id", "receipt_number", "status_title"),
        write=True,
    ),
    Tool(
        "complete_work_item",
        "Complete a work item",
        "Mark a task, deadline, or appointment as done.",
        complete_work_item,
        {"work_item_id": {"type": "string"}},
        ("work_item_id",),
        write=True,
    ),
]

BY_NAME = {tool.name: tool for tool in TOOLS}


def available(user, scopes, write_tools_enabled):
    """The tools this person may use through this token."""
    can_write = write_tools_enabled and "crm:write" in scopes
    can_bill = user.has_perm("billing.access_billing")
    return [
        tool for tool in TOOLS if (not tool.write or can_write) and (not tool.billing or can_bill)
    ]
