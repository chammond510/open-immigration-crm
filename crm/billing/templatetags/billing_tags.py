from decimal import Decimal

from django import template

from ..models import BillingSettings, Invoice
from ..services import OPEN_STATUSES, unbilled_time

register = template.Library()


@register.filter
def money(value, currency="USD"):
    amount = Decimal(value or 0).quantize(Decimal("0.01"))
    sign = "-" if amount < 0 else ""
    text = f"{abs(amount):,.2f}"
    return f"{sign}${text}" if currency == "USD" else f"{sign}{text} {currency}"


@register.inclusion_tag("billing/includes/billing_panel.html", takes_context=True)
def billing_panel(context, contact=None, matter=None):
    """Invoices and unbilled time for a contact or matter, for staff who can bill."""
    user = context["request"].user
    if not user.has_perm("billing.access_billing"):
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
        "currency": BillingSettings.load().currency,
    }
