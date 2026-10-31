from django import template
from django.contrib.auth.context_processors import PermWrapper

from crm.connect.base import Capability

from ..catalog import DETAIL_IDS, EVENT_GROUPS, EVENT_TYPES
from ..forms import EventSettingsForm

register = template.Library()


@register.inclusion_tag("events/includes/events_panel.html", takes_context=True)
def events_panel(context, connection):
    """Event choices and recent deliveries for an automation connection."""
    config = connection.config or {}
    chosen = set(config.get("events", []))
    sends = Capability.AUTOMATION in connection.enabled_capabilities
    return {
        "sends_events": sends,
        "connection": connection,
        "user": context["request"].user,
        "perms": PermWrapper(context["request"].user),
        "csrf_token": context.get("csrf_token"),
        "groups": [
            {
                "label": label,
                "events": [
                    {"name": name, "label": description, "checked": name in chosen}
                    for name, description in events.items()
                ],
            }
            for label, events in EVENT_GROUPS.items()
        ],
        "chosen_labels": [EVENT_TYPES[name] for name in EVENT_TYPES if name in chosen],
        "detail_field": EventSettingsForm(initial={"detail": config.get("detail", DETAIL_IDS)})[
            "detail"
        ],
        "deliveries": connection.event_deliveries.all()[:10] if sends else [],
    }
