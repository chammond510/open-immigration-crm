"""The outbound policy: every send, charge, write, or AI request passes through ``send``."""

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .base import ConnectorError
from .models import Connection, OutboundRequest


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str
    reason: str


class Codes:
    OK = "ok"
    PREVIEW = "preview_mode"
    KILL_SWITCH = "outbound_disabled"
    NOT_INSTALLED = "connector_not_installed"
    INACTIVE = "connection_inactive"
    CAPABILITY_OFF = "capability_not_enabled"
    NOT_ACKNOWLEDGED = "data_flows_not_acknowledged"
    PERSONAL_CONNECTION = "personal_connection"
    ADMIN_ONLY = "admin_only"
    RATE_LIMITED = "rate_limited"


def evaluate(connection, *, capability, actor=None, is_automation=False):
    """Decide whether an outbound action may run. Checks run in a fixed, documented order."""
    if not settings.CONNECTORS_OUTBOUND_ENABLED:
        return Decision(False, Codes.KILL_SWITCH, "Outbound actions are turned off for this site.")
    if connection.connector_class is None:
        return Decision(False, Codes.NOT_INSTALLED, "This connection's connector is not installed.")
    if connection.status != Connection.Status.ACTIVE:
        return Decision(False, Codes.INACTIVE, "This connection is not active.")
    if capability not in connection.enabled_capabilities:
        return Decision(False, Codes.CAPABILITY_OFF, "That capability is off for this connection.")
    if not connection.data_flows_acknowledged:
        return Decision(
            False,
            Codes.NOT_ACKNOWLEDGED,
            "A superuser must review what this connection sends before it is used.",
        )
    is_owner = actor is not None and actor.pk == connection.owner_id
    if connection.owner_id is not None and (is_automation or not is_owner):
        return Decision(
            False,
            Codes.PERSONAL_CONNECTION,
            "A personal connection runs only its owner's own requests, never automations.",
        )
    if connection.outbound_admin_only and not is_automation and not (actor and actor.is_superuser):
        return Decision(
            False, Codes.ADMIN_ONLY, "Only superusers can send through this connection."
        )
    sent_last_hour = OutboundRequest.objects.filter(
        connection=connection,
        status=OutboundRequest.Status.SENT,
        created_at__gte=timezone.now() - timedelta(hours=1),
    ).count()
    if sent_last_hour >= settings.CONNECTORS_OUTBOUND_HOURLY_LIMIT:
        return Decision(False, Codes.RATE_LIMITED, "This connection reached its hourly limit.")
    if connection.preview_mode:
        return Decision(False, Codes.PREVIEW, "Preview mode: recorded but not sent.")
    return Decision(True, Codes.OK, "")


def send(connection, action, payload, *, actor=None, is_automation=False, summary="", target=None):
    """Run an outbound action through the policy and record the attempt.

    ``summary`` is stored and shown to staff, so keep client details out of it.
    """
    cls = connection.connector_class
    capability = cls.outbound_actions.get(action, "") if cls else ""
    if cls is not None and not capability:
        raise ValueError(f"{cls.name} has no outbound action named {action!r}.")
    decision = evaluate(connection, capability=capability, actor=actor, is_automation=is_automation)
    request = OutboundRequest(
        connection=connection,
        connector_key=connection.connector_key,
        capability=capability,
        action=action,
        summary=summary[:255],
        actor=actor,
        is_automation=is_automation,
        target_type=target.__class__.__name__ if target is not None else "",
        target_id=str(target.pk) if target is not None else "",
        decision_code=decision.code,
    )
    if decision.code == Codes.PREVIEW:
        request.status = OutboundRequest.Status.PREVIEWED
    elif not decision.allowed:
        request.status = OutboundRequest.Status.BLOCKED
        request.error = decision.reason
    else:
        try:
            request.provider_ref = str(connection.connector().perform(action, payload))[:255]
            request.status = OutboundRequest.Status.SENT
        except ConnectorError as exc:
            request.status = OutboundRequest.Status.FAILED
            request.error = str(exc)[:500]
        except Exception as exc:
            # Unexpected errors may echo client data, so only the type is stored.
            request.status = OutboundRequest.Status.FAILED
            request.error = f"Unexpected {exc.__class__.__name__}"
    request.save()
    return request
