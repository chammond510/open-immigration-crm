"""Operating figures built only from CRM data: intake, matters, deadlines, USCIS
activity, communications, time, and (for staff with billing access) money."""

from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Count, Min
from django.utils import timezone

from crm.billing.models import BillingSettings, Invoice, Payment, money
from crm.comms.models import Communication
from crm.customfields.models import CustomField
from crm.models import Contact, Matter, TimeEntry, WorkItem
from crm.notices.models import CaseNotice, CaseStatusCheck
from crm.workflows.models import MatterPhase

from .models import StageChange

RETAINED_STAGES = (Contact.Stage.RETAINED, Contact.Stage.ACTIVE)
NOT_RECORDED = "Not recorded"
AGING_BUCKETS = [
    ("current", "Not yet due"),
    ("1_30", "1–30 days late"),
    ("31_60", "31–60 days late"),
    ("61_90", "61–90 days late"),
    ("90_plus", "Over 90 days late"),
]


def pct(part, whole):
    return round(100 * part / whole) if whole else None


def with_shares(rows, key="count"):
    """Add each row's share of the largest value, for the page's bars."""
    largest = max((row[key] for row in rows), default=0)
    for row in rows:
        row["share"] = pct(row[key], largest) or 0
    return rows


def in_period(queryset, field, period):
    return queryset.filter(**{f"{field}__gte": period.start_at, f"{field}__lt": period.end_at})


def on_days(queryset, field, period):
    return queryset.filter(**{f"{field}__gte": period.start, f"{field}__lte": period.end})


def first_entries(period, stages):
    """Contact IDs whose first entry into any of ``stages`` falls in the period."""
    firsts = (
        StageChange.objects.filter(to_stage__in=stages)
        .values("contact_id")
        .annotate(first=Min("changed_at"))
        .filter(first__gte=period.start_at, first__lt=period.end_at)
    )
    return [row["contact_id"] for row in firsts]


def hours(seconds):
    return (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.1"))


def entry_seconds(entries):
    return sum((stop - start).total_seconds() for _, start, stop in entries)


def headline(period, *, include_billing):
    """The figures compared with the previous period."""
    leads = in_period(Contact.objects, "created_at", period).count()
    consults = len(first_entries(period, [Contact.Stage.CONSULTATION]))
    retained = len(first_entries(period, RETAINED_STAGES))
    figures = {
        "new_leads": leads,
        "consultations": consults,
        "retained": retained,
        "matters_opened": in_period(Matter.objects, "created_at", period).count(),
        "hours": hours(
            entry_seconds(
                in_period(TimeEntry.objects, "started_at", period)
                .filter(stopped_at__isnull=False)
                .values_list("user_id", "started_at", "stopped_at")
            )
        ),
    }
    if include_billing:
        figures["invoiced"] = money(
            sum(
                (invoice.subtotal for invoice in issued_invoices(period)),
                Decimal("0"),
            )
        )
        figures["collected"] = money(
            sum((payment.amount for payment in payments_received(period)), Decimal("0"))
        )
    return figures


HEADLINE_LABELS = {
    "new_leads": "New leads",
    "consultations": "Consultations",
    "retained": "Retained",
    "matters_opened": "Matters opened",
    "hours": "Hours logged",
    "invoiced": "Invoiced",
    "collected": "Collected",
}


def issued_invoices(period):
    return (
        on_days(Invoice.objects, "issue_date", period)
        .exclude(status=Invoice.Status.VOID)
        .prefetch_related("lines")
    )


def payments_received(period):
    return on_days(Payment.objects, "received_on", period).filter(voided_at__isnull=True)


def lead_sources(period, field):
    """New leads and retentions in the period, grouped by a choice custom field."""
    leads = Counter(
        (values or {}).get(field.key) or NOT_RECORDED
        for values in in_period(Contact.objects, "created_at", period).values_list(
            "custom_fields", flat=True
        )
    )
    retained = Counter(
        (values or {}).get(field.key) or NOT_RECORDED
        for values in Contact.objects.filter(
            pk__in=first_entries(period, RETAINED_STAGES)
        ).values_list("custom_fields", flat=True)
    )
    names = [*field.choice_list, *sorted(set(leads) | set(retained))]
    rows = []
    for name in dict.fromkeys(names):
        if leads[name] or retained[name]:
            rows.append({"value": name, "leads": leads[name], "retained": retained[name]})
    return sorted(rows, key=lambda row: (-row["leads"], -row["retained"], row["value"]))


def matters_section(period):
    open_matters = Matter.objects.exclude(status=Matter.Status.CLOSED)
    statuses = dict(open_matters.values_list("status").annotate(total=Count("pk")))
    case_types = dict(open_matters.values_list("case_type").annotate(total=Count("pk")))
    phases = (
        MatterPhase.objects.filter(
            status=MatterPhase.Status.IN_PROGRESS, workflow__matter__in=open_matters
        )
        .values("workflow__name", "name", "position")
        .annotate(total=Count("pk"))
        .order_by("workflow__name", "position", "name")
    )
    return {
        "open": open_matters.count(),
        "opened": in_period(Matter.objects, "created_at", period).count(),
        "closed": in_period(Matter.objects, "closed_at", period).count(),
        "by_status": with_shares(
            [
                {"key": key, "label": label, "count": statuses.get(key, 0)}
                for key, label in Matter.Status.choices
                if key != Matter.Status.CLOSED
            ]
        ),
        "by_case_type": with_shares(
            sorted(
                (
                    {"key": key, "label": label, "count": case_types[key]}
                    for key, label in Matter.CaseType.choices
                    if case_types.get(key)
                ),
                key=lambda row: -row["count"],
            )
        ),
        "by_phase": [
            {"workflow": row["workflow__name"], "phase": row["name"], "count": row["total"]}
            for row in phases
        ],
    }


def deadlines_section(period):
    now = timezone.now()
    open_work = WorkItem.objects.filter(is_completed=False)
    due_soon = (
        open_work.filter(
            kind=WorkItem.Kind.DEADLINE,
            scheduled_for__gte=now,
            scheduled_for__lt=now + timedelta(days=7),
        )
        .select_related("matter", "contact")
        .order_by("scheduled_for")
    )
    responses = (
        CaseNotice.objects.filter(
            kind__in=[CaseNotice.Kind.RFE, CaseNotice.Kind.NOID],
            response_due_date__isnull=False,
            deadline__is_completed=False,
        )
        .select_related("matter")
        .order_by("response_due_date")
    )
    return {
        "overdue": open_work.filter(scheduled_for__lt=now).count(),
        "due_this_week": due_soon.count(),
        "due_soon": list(due_soon[:8]),
        "completed": in_period(WorkItem.objects, "completed_at", period).count(),
        "open_responses": responses.count(),
        "responses": list(responses[:5]),
    }


def uscis_section(period):
    kinds = dict(
        on_days(CaseNotice.objects, "notice_date", period)
        .values_list("kind")
        .annotate(total=Count("pk"))
    )
    return {
        "notices": [
            {"key": key, "label": label, "count": kinds[key]}
            for key, label in CaseNotice.Kind.choices
            if kinds.get(key)
        ],
        "status_changes": in_period(
            CaseStatusCheck.objects.filter(changed=True), "checked_at", period
        ).count(),
    }


def communications_section(period):
    channels = dict(
        in_period(Communication.objects, "occurred_at", period)
        .values_list("channel")
        .annotate(total=Count("pk"))
    )
    return with_shares(
        [
            {"key": key, "label": label, "count": channels.get(key, 0)}
            for key, label in Communication.Channel.choices
        ]
    )


def time_section(period):
    entries = list(
        in_period(TimeEntry.objects, "started_at", period)
        .filter(stopped_at__isnull=False)
        .values_list("user_id", "started_at", "stopped_at")
    )
    by_user = defaultdict(float)
    for user_id, start, stop in entries:
        by_user[user_id] += (stop - start).total_seconds()
    people = get_user_model().objects.in_bulk(list(by_user))
    unbilled = TimeEntry.objects.filter(
        stopped_at__isnull=False, invoice_line__isnull=True
    ).values_list("user_id", "started_at", "stopped_at")
    return {
        "hours": hours(entry_seconds(entries)),
        "by_user": with_shares(
            sorted(
                (
                    {
                        "name": people[user_id].get_full_name() or people[user_id].username,
                        "hours": hours(seconds),
                    }
                    for user_id, seconds in by_user.items()
                ),
                key=lambda row: -row["hours"],
            ),
            key="hours",
        ),
        "unbilled_hours": hours(entry_seconds(unbilled)),
    }


def aging_bucket(due_date, today):
    late = (today - due_date).days if due_date else 0
    if late <= 0:
        return "current"
    if late <= 30:
        return "1_30"
    if late <= 60:
        return "31_60"
    return "61_90" if late <= 90 else "90_plus"


def billing_section(period):
    today = timezone.localdate()
    currency = BillingSettings.load().currency
    payments = list(payments_received(period))
    collected = {account: Decimal("0") for account in Payment.Account.values}
    for payment in payments:
        collected[payment.deposit_account] += payment.amount
    buckets = {key: Decimal("0") for key, _ in AGING_BUCKETS}
    open_invoices = Invoice.objects.filter(
        status__in=[Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID]
    ).prefetch_related("lines", "payments")
    for invoice in open_invoices:
        paid = sum((p.amount for p in invoice.payments.all() if not p.voided_at), Decimal("0"))
        buckets[aging_bucket(invoice.due_date, today)] += invoice.subtotal - paid
    outstanding = sum(buckets.values(), Decimal("0"))
    return {
        "currency": currency,
        "invoiced": money(sum((i.subtotal for i in issued_invoices(period)), Decimal("0"))),
        "invoice_count": issued_invoices(period).count(),
        "collected": money(sum(collected.values(), Decimal("0"))),
        "collected_operating": money(collected[Payment.Account.OPERATING]),
        "collected_trust": money(collected[Payment.Account.TRUST]),
        "payment_count": len(payments),
        "outstanding": money(outstanding),
        "overdue": money(outstanding - buckets["current"]),
        "aging": with_shares(
            [
                {"key": key, "label": label, "amount": money(buckets[key])}
                for key, label in AGING_BUCKETS
            ],
            key="amount",
        ),
    }


def source_field(key=""):
    """The choice custom field leads are grouped by: the one named, or the first active one."""
    fields = CustomField.objects.filter(
        applies_to=CustomField.AppliesTo.CONTACT,
        field_type=CustomField.FieldType.CHOICE,
        is_active=True,
    )
    return fields.filter(key=key).first() if key else fields.first()


def build_report(period, *, include_billing, source_key=""):
    current = headline(period, include_billing=include_billing)
    previous = headline(period.previous(), include_billing=include_billing)
    field = source_field(source_key)
    return {
        "period": period,
        "previous": period.previous(),
        "headline": [
            {
                "key": key,
                "label": HEADLINE_LABELS[key],
                "value": value,
                "previous": previous[key],
                "change": value - previous[key],
            }
            for key, value in current.items()
        ],
        "funnel": with_shares(
            [
                {"key": "new_leads", "label": "New leads", "count": current["new_leads"]},
                {
                    "key": "consultations",
                    "label": "Consultations",
                    "count": current["consultations"],
                },
                {"key": "retained", "label": "Retained", "count": current["retained"]},
                {
                    "key": "lost",
                    "label": "Not retained",
                    "count": len(first_entries(period, [Contact.Stage.LOST])),
                },
            ]
        ),
        "retained_per_consult": pct(current["retained"], current["consultations"]),
        "source_field": field,
        "sources": lead_sources(period, field) if field else [],
        "matters": matters_section(period),
        "deadlines": deadlines_section(period),
        "uscis": uscis_section(period),
        "communications": communications_section(period),
        "time": time_section(period),
        "billing": billing_section(period) if include_billing else None,
    }
