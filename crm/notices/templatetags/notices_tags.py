from django import template

from ..forms import StatusCheckForm
from ..services import latest_statuses

register = template.Library()


@register.inclusion_tag("notices/includes/notices_panel.html", takes_context=True)
def notices_panel(context, matter):
    """A matter's USCIS notices, latest status per receipt, and a status form."""
    notices = list(matter.notices.select_related("deadline", "appointment", "document"))
    receipts = sorted(
        {n.receipt_number for n in notices if n.receipt_number}
        | ({matter.receipt_number} if matter.receipt_number else set())
    )
    return {
        "request": context["request"],
        "user": context["user"],
        "matter": matter,
        "notices": notices,
        "statuses": latest_statuses(matter),
        "receipts": receipts,
        "status_form": StatusCheckForm(initial={"receipt_number": receipts[0] if receipts else ""}),
    }
