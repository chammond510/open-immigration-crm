from decimal import Decimal

from django import template

from crm.roles import BILLING

from ..models import BillingSettings, Invoice, format_money
from ..services import OPEN_STATUSES, unbilled_time

register = template.Library()


@register.filter
def money(value, currency="USD"):
    return format_money(value, currency)


@register.inclusion_tag("billing/includes/billing_panel.html", takes_context=True)
def billing_panel(context, contact=None, matter=None):
    """Invoices and unbilled time for a contact or matter, for staff who can bill."""
    user = context["request"].user
    if not user.has_perm(BILLING):
        return {"visible": False}
    invoices = Invoice.objects.prefetch_related("lines", "payments")
    invoices = invoices.filter(matter=matter) if matter else invoices.filter(contact=contact)
    probe = Invoice(contact=contact or matter.primary_contact, matter=matter)
    unbilled = list(unbilled_time(probe))
    open_invoices = [invoice for invoice in invoices if invoice.status in OPEN_STATUSES]
    return {
        "visible": True,
        "contact": contact,
        "matter": matter,
        "invoices": invoices[:5],
        "balance": sum((invoice.balance for invoice in open_invoices), Decimal("0")),
        "unbilled_count": len(unbilled),
        "unbilled_minutes": sum(int(e.duration.total_seconds() // 60) for e in unbilled),
        "next_installments": [
            (invoice, row)
            for invoice in open_invoices
            for row in [invoice.plan.next_due() if invoice.plan else None]
            if row
        ],
        "currency": BillingSettings.load().currency,
    }
