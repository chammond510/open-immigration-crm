from django import template
from django.contrib.auth.context_processors import PermWrapper
from django.utils import timezone

from ..emailing import email_connections
from ..forms import CommunicationForm
from ..models import Communication
from ..texting import (
    MAX_LENGTH,
    can_text_freely,
    reply_window,
    reply_window_ends,
    template_connections,
    texting_connections,
)

register = template.Library()

PANEL_LIMIT = 10


def reply_windows(user, connections, contact):
    """For connections with a reply window: whether it is open to the contact, and until when."""
    if not contact or not contact.phone_e164:
        return []
    templates = {c.pk for c in template_connections(user)}
    windows = []
    for connection in connections:
        window = reply_window(connection)
        if window:
            windows.append(
                {
                    "connection": connection,
                    "hours": int(window.total_seconds() // 3600),
                    "ends": reply_window_ends(connection, contact),
                    "open": can_text_freely(connection, contact),
                    "templates": connection.pk in templates,
                }
            )
    return windows


@register.inclusion_tag("comms/includes/communications_panel.html", takes_context=True)
def communications_panel(context, contact=None, matter=None):
    """Recent communications and an inline log form for a contact or matter page."""
    owner = matter or contact
    communications = (
        owner.communications.select_related("logged_by", "source_connection").prefetch_related(
            "translations", "attachments__saved_document__matter"
        )
        if owner
        else Communication.objects.none()
    )
    text_to = contact or (matter.primary_contact if matter else None)
    form = CommunicationForm(
        initial={
            "occurred_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "channel": Communication.Channel.CALL,
            "contact": text_to,
            "matter": matter,
        },
        auto_id="id_comm_%s",
    )
    text_connections = texting_connections(context["user"])
    return {
        "request": context["request"],
        "user": context["user"],
        "perms": PermWrapper(context["user"]),
        "contact": contact,
        "matter": matter,
        "communications": communications[:PANEL_LIMIT],
        "total": communications.count(),
        "form": form,
        "text_to": text_to,
        "text_connections": text_connections,
        "free_text_connections": [
            c for c in text_connections if not text_to or can_text_freely(c, text_to)
        ],
        "text_windows": reply_windows(context["user"], text_connections, text_to),
        "text_max_length": MAX_LENGTH,
        "email_connections": email_connections(context["user"]),
    }
