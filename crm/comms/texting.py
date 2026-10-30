"""Sending a text through a connected texting service.

Core knows only the ``send_text`` action that texting connectors declare, with
the payload ``{"to": E.164 number, "body": text, "staff_email": sender's email}``.
Every text goes through the outbound policy, which holds it in preview mode, stops
it for a contact who opted out or past the hourly limit, and records the attempt.
A sent text is logged as an outbound communication under the provider's message
ID, so the provider's own copy and later delivery updates land on the same record.
"""

from django.core.exceptions import ValidationError
from django.utils import timezone

from crm.connect import policy
from crm.connect.base import Capability
from crm.connect.models import Connection, OutboundRequest
from crm.connect.services import visible_connections

from .models import Communication
from .services import record_communication

SEND_TEXT = "send_text"
MAX_LENGTH = 1600


def texting_connections(user):
    """Active connections this person can send texts through: the firm's and their own."""
    found = []
    for connection in visible_connections(user).filter(status=Connection.Status.ACTIVE):
        cls = connection.connector_class
        if (
            cls is not None
            and Capability.TEXTING in connection.enabled_capabilities
            and cls.outbound_actions.get(SEND_TEXT) == Capability.TEXTING
            and connection.owner_id in (None, user.pk)
        ):
            found.append(connection)
    return found


def send_text(connection, *, contact, body, actor, matter=None):
    """Send ``body`` to the contact's phone; return the policy's ``OutboundRequest``."""
    body = body.strip()
    if not body:
        raise ValidationError("Write the text first.")
    if len(body) > MAX_LENGTH:
        raise ValidationError(f"Texts can be at most {MAX_LENGTH} characters.")
    if not contact.phone_e164:
        raise ValidationError(f"Add a valid phone number for {contact} first.")
    request = policy.send(
        connection,
        SEND_TEXT,
        {"to": contact.phone_e164, "body": body, "staff_email": actor.email},
        actor=actor,
        target=contact,
        summary=f"Text from {actor.get_username()} ({len(body)} characters)",
    )
    if request.status == OutboundRequest.Status.SENT:
        fields = {
            "channel": Communication.Channel.TEXT,
            "direction": Communication.Direction.OUTBOUND,
            "occurred_at": timezone.now(),
            "counterpart": contact.phone_e164,
            "summary": body,
            "delivery_status": "sent",
            "contact": contact,
            "matter": matter,
            "logged_by": actor,
        }
        # A delivery update may already have arrived; it is newer than "sent".
        if Communication.objects.filter(
            source_connection=connection, external_id=request.provider_ref
        ).exclude(delivery_status=""):
            del fields["delivery_status"]
        record_communication(
            source_connection=connection, external_id=request.provider_ref, **fields
        )
    return request
