from django import template

from ..base import CAPABILITY_NOUNS, Capability
from ..models import Connection
from ..services import connection_alerts, visible_connections

register = template.Library()


@register.inclusion_tag("connect/includes/connect_prompt.html", takes_context=True)
def connect_prompt(context, capability, purpose=""):
    """Offer a capability's connectors beside a feature's manual path.

    Usage: {% connect_prompt "phone" "to log calls automatically" %}
    """
    user = context["request"].user
    connected = next(
        (
            connection
            for connection in visible_connections(user).filter(status=Connection.Status.ACTIVE)
            if capability in connection.enabled_capabilities
        ),
        None,
    )
    return {
        "capability": capability,
        "noun": CAPABILITY_NOUNS.get(capability, Capability(capability).label.lower()),
        "purpose": purpose,
        "connected": connected,
    }


@register.inclusion_tag("connect/includes/connection_alerts.html", takes_context=True)
def connection_alert_banner(context):
    return connection_alerts(context["request"].user)


@register.filter
def capability_label(value):
    try:
        return Capability(value).label
    except ValueError:
        return value
