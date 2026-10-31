"""Version 1 of the JSON API. See docs/API.md."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone

from crm.billing.models import Invoice
from crm.comms.forms import CommunicationForm
from crm.comms.models import Communication
from crm.customfields.models import CustomField, active_fields
from crm.forms import ContactForm, MatterForm, WorkItemForm
from crm.models import Activity, Contact, Matter, TimeEntry, WorkItem
from crm.notices.forms import CaseNoticeForm, StatusCheckForm
from crm.notices.models import CaseNotice, CaseStatusCheck, normalize_receipt_number
from crm.notices.services import record_status, save_notice
from crm.reports import periods
from crm.reports.metrics import build_report
from crm.reports.serializers import report_data
from crm.search import CONTACT_SEARCH_FIELDS, MATTER_SEARCH_FIELDS, words_match
from crm.services import add_activity, audit

from . import serializers
from .forms import NoteForm, TimeEntryForm
from .http import (
    ApiError,
    bind,
    bool_param,
    choice_param,
    created,
    datetime_param,
    endpoint,
    paginate,
    read_json,
    user_param,
    uuid_param,
    with_custom_fields,
)

CONTACT_FIELDS = ContactForm._meta.fields
MATTER_FIELDS = MatterForm._meta.fields
WORK_FIELDS = [*WorkItemForm._meta.fields, "is_completed"]
NOTE_FIELDS = ["kind", "subject", "body", "occurred_at", "contact", "matter"]
COMMUNICATION_FIELDS = [*CommunicationForm._meta.fields, "external_id"]
TIME_FIELDS = ["started_at", "stopped_at", "duration_minutes", "note", "contact", "matter"]
NOTICE_FIELDS = CaseNoticeForm._meta.fields
STATUS_FIELDS = ["matter", "receipt_number", "status_title", "status_text", "checked_at"]

ENDPOINTS = {
    "users": "api_users",
    "contacts": "api_contacts",
    "matters": "api_matters",
    "work_items": "api_work_items",
    "notes": "api_notes",
    "communications": "api_communications",
    "time_entries": "api_time_entries",
    "notices": "api_notices",
    "status_checks": "api_status_checks",
    "invoices": "api_invoices",
    "reports": "api_report_summary",
}


def via_api(request):
    return f"Through API token {request.api_token.prefix}…"


def serialize_with(serializer, request):
    return lambda record: serializer(record, request)


def since(request, queryset):
    """Records changed at or after updated_since, oldest change first, for syncing."""
    value = datetime_param(request, "updated_since")
    if value is None:
        return queryset
    return queryset.filter(updated_at__gte=value).order_by("updated_at", "pk")


def linked_filters(request, queryset, *names):
    for name in names:
        value = uuid_param(request, name)
        if value is not None:
            queryset = queryset.filter(**{f"{name}_id": value})
    return queryset


def required_matter(data):
    raw = data.pop("matter", None)
    if not raw:
        problem = "This field is required."
    else:
        try:
            return Matter.objects.select_related("primary_contact").get(pk=raw)
        except (Matter.DoesNotExist, ValidationError, ValueError, TypeError):
            problem = "No matter has that ID."
    raise ApiError(400, "invalid", "Some fields are not valid.", fields={"matter": [problem]})


@endpoint()
def index(request):
    token = request.api_token
    return JsonResponse(
        {
            "version": "v1",
            "user": serializers.user(request.user),
            "token": {
                "name": token.name,
                "prefix": token.prefix,
                "scopes": token.scopes,
                "expires_at": serializers.iso(token.expires_at),
            },
            "endpoints": {
                name: request.build_absolute_uri(reverse(url_name))
                for name, url_name in ENDPOINTS.items()
            },
            "custom_fields": {
                applies_to: [
                    {
                        "key": field.key,
                        "label": field.label,
                        "type": field.field_type,
                        "required": field.required,
                        "choices": field.choice_list,
                    }
                    for field in active_fields(applies_to)
                ]
                for applies_to in CustomField.AppliesTo.values
            },
            "choices": {
                "contact.status": Contact.Status.values,
                "contact.stage": Contact.Stage.values,
                "matter.case_type": Matter.CaseType.values,
                "matter.status": Matter.Status.values,
                "work_item.kind": WorkItem.Kind.values,
                "work_item.priority": WorkItem.Priority.values,
                "note.kind": Activity.Kind.values,
                "communication.channel": Communication.Channel.values,
                "communication.direction": Communication.Direction.values,
                "notice.kind": CaseNotice.Kind.values,
                "invoice.status": Invoice.Status.values,
            },
        }
    )


@endpoint()
def users(request):
    staff = get_user_model().objects.filter(is_active=True, is_staff=True).order_by("username")
    return paginate(request, staff, serializers.user)


# Contacts


@endpoint("contacts", methods=("GET", "POST"))
def contacts(request):
    if request.method == "POST":
        form = bind(
            ContactForm,
            with_custom_fields(read_json(request)),
            writable=CONTACT_FIELDS,
            initial={"assigned_to": request.user.pk},
        )
        contact = form.save()
        add_activity(
            request=request, contact=contact, subject="Contact created", kind=Activity.Kind.SYSTEM
        )
        audit(request, "contact.created", contact, via_api(request))
        return created(serializers.contact(contact, request))
    records = Contact.objects.all()
    query = request.GET.get("q", "").strip()
    if query:
        records = records.filter(words_match(query, CONTACT_SEARCH_FIELDS))
    for name, choices in (("status", Contact.Status.values), ("stage", Contact.Stage.values)):
        value = choice_param(request, name, choices)
        if value:
            records = records.filter(**{name: value})
    assignee = user_param(request, "assigned_to")
    if assignee:
        records = records.filter(assigned_to_id=assignee)
    return paginate(request, since(request, records), serialize_with(serializers.contact, request))


@endpoint("contacts", methods=("GET", "PATCH"))
def contact_detail(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    if request.method == "PATCH":
        before_stage = contact.stage
        contact = bind(
            ContactForm,
            with_custom_fields(read_json(request)),
            writable=CONTACT_FIELDS,
            instance=contact,
        ).save()
        if contact.stage != before_stage:
            add_activity(
                request=request,
                contact=contact,
                subject=f"Pipeline stage changed to {contact.get_stage_display()}",
                kind=Activity.Kind.STATUS,
            )
        audit(request, "contact.updated", contact, via_api(request))
    return JsonResponse(serializers.contact(contact, request))


# Matters


def matter_records():
    return Matter.objects.select_related("primary_contact", "workflow").prefetch_related(
        "parties", "workflow__phases"
    )


@endpoint("matters", methods=("GET", "POST"))
def matters(request):
    if request.method == "POST":
        form = bind(
            MatterForm,
            with_custom_fields(read_json(request)),
            writable=MATTER_FIELDS,
            initial={"assigned_to": request.user.pk},
        )
        matter = form.save()
        add_activity(
            request=request,
            matter=matter,
            contact=matter.primary_contact,
            subject=f"Matter {matter.matter_number} created",
            kind=Activity.Kind.SYSTEM,
        )
        audit(request, "matter.created", matter, via_api(request))
        return created(serializers.matter(matter_records().get(pk=matter.pk), request))
    records = matter_records()
    query = request.GET.get("q", "").strip()
    if query:
        records = records.filter(words_match(query, MATTER_SEARCH_FIELDS))
    for name, choices in (("status", Matter.Status.values), ("case_type", Matter.CaseType.values)):
        value = choice_param(request, name, choices)
        if value:
            records = records.filter(**{name: value})
    contact = uuid_param(request, "contact")
    if contact:
        records = records.filter(
            Q(primary_contact_id=contact) | Q(parties__contact_id=contact)
        ).distinct()
    assignee = user_param(request, "assigned_to")
    if assignee:
        records = records.filter(assigned_to_id=assignee)
    return paginate(request, since(request, records), serialize_with(serializers.matter, request))


@endpoint("matters", methods=("GET", "PATCH"))
def matter_detail(request, pk):
    matter = get_object_or_404(matter_records(), pk=pk)
    if request.method == "PATCH":
        before_status = matter.status
        matter = bind(
            MatterForm,
            with_custom_fields(read_json(request)),
            writable=MATTER_FIELDS,
            instance=matter,
        ).save()
        if matter.status != before_status:
            add_activity(
                request=request,
                matter=matter,
                contact=matter.primary_contact,
                subject=f"Matter status changed to {matter.get_status_display()}",
                kind=Activity.Kind.STATUS,
            )
        audit(request, "matter.updated", matter, via_api(request))
        matter = matter_records().get(pk=matter.pk)
    return JsonResponse(serializers.matter(matter, request))


# Work items


def pop_completed(data):
    if "is_completed" not in data:
        return None
    value = data.pop("is_completed")
    if not isinstance(value, bool):
        raise ApiError(
            400,
            "invalid",
            "Some fields are not valid.",
            fields={"is_completed": ["Use true or false."]},
        )
    return value


@endpoint("work", methods=("GET", "POST"))
def work_items(request):
    if request.method == "POST":
        data = read_json(request)
        completed = pop_completed(data)
        item = bind(
            WorkItemForm, data, writable=WORK_FIELDS, initial={"assigned_to": request.user.pk}
        ).save(commit=False)
        item.is_completed = bool(completed)
        item.save()
        audit(request, "work_item.created", item, via_api(request))
        return created(serializers.work_item(item, request))
    records = linked_filters(request, WorkItem.objects.all(), "matter", "contact")
    kind = choice_param(request, "kind", WorkItem.Kind.values)
    if kind:
        records = records.filter(kind=kind)
    completed = bool_param(request, "completed")
    if completed is not None:
        records = records.filter(is_completed=completed)
    assignee = user_param(request, "assigned_to")
    if assignee:
        records = records.filter(assigned_to_id=assignee)
    after, before = (
        datetime_param(request, "scheduled_after"),
        datetime_param(request, "scheduled_before"),
    )
    if after:
        records = records.filter(scheduled_for__gte=after)
    if before:
        records = records.filter(scheduled_for__lt=before)
    return paginate(
        request, since(request, records), serialize_with(serializers.work_item, request)
    )


@endpoint("work", methods=("GET", "PATCH"))
def work_item_detail(request, pk):
    item = get_object_or_404(WorkItem, pk=pk)
    if request.method == "PATCH":
        data = read_json(request)
        completed = pop_completed(data)
        item = bind(WorkItemForm, data, writable=WORK_FIELDS, instance=item).save(commit=False)
        if completed is not None:
            item.is_completed = completed
        item.save()
        audit(request, "work_item.updated", item, via_api(request))
    return JsonResponse(serializers.work_item(item, request))


# Notes


@endpoint("notes", methods=("GET", "POST"))
def notes(request):
    if request.method == "POST":
        note = bind(NoteForm, read_json(request), writable=NOTE_FIELDS).save(commit=False)
        note.created_by = request.user
        note.save()
        audit(request, "activity.created", note, via_api(request))
        return created(serializers.note(note, request))
    records = linked_filters(request, Activity.objects.all(), "matter", "contact")
    kind = choice_param(request, "kind", Activity.Kind.values)
    if kind:
        records = records.filter(kind=kind)
    return paginate(request, records, serialize_with(serializers.note, request))


@endpoint("notes")
def note_detail(request, pk):
    return JsonResponse(serializers.note(get_object_or_404(Activity, pk=pk), request))


# Communications


@endpoint("communications", methods=("GET", "POST"))
def communications(request):
    if request.method == "POST":
        data = read_json(request)
        external_id = data.pop("external_id", "") or ""
        if not isinstance(external_id, str) or len(external_id) > 255:
            raise ApiError(
                400,
                "invalid",
                "Some fields are not valid.",
                fields={"external_id": ["Use text of at most 255 characters."]},
            )
        if external_id:
            # Retried requests carrying the same ID return the first record.
            existing = Communication.objects.filter(
                source_connection__isnull=True, external_id=external_id
            ).first()
            if existing:
                return JsonResponse(serializers.communication(existing, request))
        communication = bind(CommunicationForm, data, writable=COMMUNICATION_FIELDS).save(
            commit=False
        )
        communication.logged_by = request.user
        communication.external_id = external_id
        communication.save()
        audit(request, "communication.logged", communication, via_api(request))
        return created(serializers.communication(communication, request))
    records = linked_filters(request, Communication.objects.all(), "matter", "contact")
    channel = choice_param(request, "channel", Communication.Channel.values)
    if channel:
        records = records.filter(channel=channel)
    return paginate(
        request, since(request, records), serialize_with(serializers.communication, request)
    )


@endpoint("communications")
def communication_detail(request, pk):
    record = get_object_or_404(Communication, pk=pk)
    return JsonResponse(serializers.communication(record, request))


# Time entries


@endpoint("time", methods=("GET", "POST"))
def time_entries(request):
    if request.method == "POST":
        entry = bind(TimeEntryForm, read_json(request), writable=TIME_FIELDS).save(commit=False)
        entry.user = request.user
        entry.save()
        audit(request, "time_entry.logged", entry, via_api(request))
        return created(serializers.time_entry(entry, request))
    records = linked_filters(request, TimeEntry.objects.all(), "matter", "contact")
    person = user_param(request, "user")
    if person:
        records = records.filter(user_id=person)
    after, before = (
        datetime_param(request, "started_after"),
        datetime_param(request, "started_before"),
    )
    if after:
        records = records.filter(started_at__gte=after)
    if before:
        records = records.filter(started_at__lt=before)
    return paginate(request, records, serialize_with(serializers.time_entry, request))


@endpoint("time")
def time_entry_detail(request, pk):
    entry = get_object_or_404(TimeEntry, pk=pk)
    return JsonResponse(serializers.time_entry(entry, request))


# USCIS notices and status checks


@endpoint("notices", methods=("GET", "POST"))
def notices(request):
    if request.method == "POST":
        data = read_json(request)
        matter = required_matter(data)
        form = bind(
            CaseNoticeForm,
            data,
            writable=NOTICE_FIELDS,
            matter=matter,
            initial={"notice_date": timezone.localdate()},
        )
        notice = save_notice(form.save(commit=False), request.user)
        return created(serializers.notice(notice, request))
    records = linked_filters(request, CaseNotice.objects.all(), "matter")
    kind = choice_param(request, "kind", CaseNotice.Kind.values)
    if kind:
        records = records.filter(kind=kind)
    receipt = normalize_receipt_number(request.GET.get("receipt_number", ""))
    if receipt:
        records = records.filter(receipt_number=receipt)
    if bool_param(request, "open_responses"):
        records = records.filter(
            response_due_date__isnull=False, deadline__is_completed=False
        ).order_by("response_due_date")
    return paginate(request, since(request, records), serialize_with(serializers.notice, request))


@endpoint("notices", methods=("GET", "PATCH"))
def notice_detail(request, pk):
    notice = get_object_or_404(CaseNotice.objects.select_related("matter"), pk=pk)
    if request.method == "PATCH":
        form = bind(
            CaseNoticeForm,
            read_json(request),
            writable=NOTICE_FIELDS,
            instance=notice,
            matter=notice.matter,
        )
        notice = save_notice(form.save(commit=False), request.user)
    return JsonResponse(serializers.notice(notice, request))


@endpoint("notices", methods=("GET", "POST"))
def status_checks(request):
    if request.method == "POST":
        data = read_json(request)
        matter = required_matter(data)
        form = bind(StatusCheckForm, data, writable=STATUS_FIELDS)
        check = record_status(matter, actor=request.user, **form.cleaned_data)
        return created(serializers.status_check(check, request))
    records = linked_filters(request, CaseStatusCheck.objects.all(), "matter")
    receipt = normalize_receipt_number(request.GET.get("receipt_number", ""))
    if receipt:
        records = records.filter(receipt_number=receipt)
    return paginate(request, records, serialize_with(serializers.status_check, request))


# Billing (read-only)


def invoice_records():
    return Invoice.objects.prefetch_related("lines", "payments")


@endpoint("billing")
def invoices(request):
    records = linked_filters(request, invoice_records(), "matter", "contact")
    status = choice_param(request, "status", Invoice.Status.values)
    if status:
        records = records.filter(status=status)
    return paginate(request, since(request, records), serialize_with(serializers.invoice, request))


@endpoint("billing")
def invoice_detail(request, pk):
    record = get_object_or_404(invoice_records(), pk=pk)
    return JsonResponse(serializers.invoice(record, request))


# Reports (read-only)


@endpoint("reports")
def report_summary(request):
    """The operating dashboard's figures; billing is included only with billing access."""
    period = periods.resolve(
        request.GET.get("period", ""), request.GET.get("start", ""), request.GET.get("end", "")
    )
    include_billing = request.api_token.has_scope("billing:read") and request.user.has_perm(
        "billing.access_billing"
    )
    report = build_report(
        period, include_billing=include_billing, source_key=request.GET.get("by", "")
    )
    return JsonResponse(report_data(report))
