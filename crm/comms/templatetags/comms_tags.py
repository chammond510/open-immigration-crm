from django import template
from django.utils import timezone

from ..forms import CommunicationForm
from ..models import Communication

register = template.Library()

PANEL_LIMIT = 10


@register.inclusion_tag("comms/includes/communications_panel.html", takes_context=True)
def communications_panel(context, contact=None, matter=None):
    """Recent communications and an inline log form for a contact or matter page."""
    owner = matter or contact
    communications = (
        owner.communications.select_related("logged_by", "source_connection")
        if owner
        else Communication.objects.none()
    )
    form = CommunicationForm(
        initial={
            "occurred_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "channel": Communication.Channel.CALL,
            "contact": contact or (matter.primary_contact if matter else None),
            "matter": matter,
        },
        auto_id="id_comm_%s",
    )
    return {
        "request": context["request"],
        "user": context["user"],
        "contact": contact,
        "matter": matter,
        "communications": communications[:PANEL_LIMIT],
        "total": communications.count(),
        "form": form,
    }
