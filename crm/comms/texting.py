"""Sending a text through a connected texting service.

Core knows only the ``send_text`` action that texting connectors declare, with
the payload ``{"to": E.164 number, "body": text, "staff_email": sender's email}``.
Every text goes through the outbound policy, which holds it in preview mode, stops
it for a contact who opted out or past the hourly limit, and records the attempt.
A sent text is logged as an outbound communication under the provider's message
ID, so the provider's own copy and later delivery updates land on the same record.

Some messaging services allow free-form texts only for a time after the contact's
last message, the connector's ``reply_window``. Outside it, staff send one of the
service's approved templates through the ``send_template`` action instead.
"""

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils import timezone

from crm.connect import policy
from crm.connect.base import Capability
from crm.connect.models import OutboundRequest

from .models import Communication
from .outbound import connections_for
from .services import record_communication

SEND_TEXT = "send_text"
SEND_TEMPLATE = "send_template"
MAX_LENGTH = 1600
# Providers limit how often templates can be listed, so a connection's list is kept a while.
TEMPLATE_CACHE_SECONDS = 300


def texting_connections(user):
    """Active connections this person can send texts through: the firm's and their own."""
    return connections_for(user, SEND_TEXT, Capability.TEXTING)


def template_connections(user):
    """Active connections this person can send approved templates through."""
    return connections_for(user, SEND_TEMPLATE, Capability.TEXTING)


def approved_templates(connection):
    """The connection's approved templates, listed again after five minutes or a change."""
    key = f"comms-templates:{connection.pk}:{connection.updated_at.timestamp()}"
    templates = cache.get(key)
    if templates is None:
        templates = list(connection.connector().message_templates())
        cache.set(key, templates, TEMPLATE_CACHE_SECONDS)
    return templates


def reply_window(connection):
    """How long after a contact's message free-form texts may be sent, or None for always."""
    cls = connection.connector_class
    return cls.reply_window if cls is not None else None


def reply_window_ends(connection, contact):
    """When the connection's reply window to the contact ends, or None if they never wrote.

    It runs from the contact's latest text to this connection, from their number.
    """
    window = reply_window(connection)
    if not window or not contact.phone_e164:
        return None
    latest = (
        Communication.objects.filter(
            source_connection=connection,
            channel=Communication.Channel.TEXT,
            direction=Communication.Direction.INBOUND,
            counterpart_key=contact.phone_e164,
        )
        .order_by("-occurred_at")
        .values_list("occurred_at", flat=True)
        .first()
    )
    return latest + window if latest else None


def can_text_freely(connection, contact):
    """Whether a free-form text may go to the contact through the connection now."""
    if not reply_window(connection):
        return True
    ends = reply_window_ends(connection, contact)
    return bool(ends and ends > timezone.now())


def window_message(connection, contact):
    hours = int(reply_window(connection).total_seconds() // 3600)
    return (
        f"{connection.label} allows free-form messages only within {hours} hours of "
        f"{contact}'s last message. Send one of its approved templates instead."
    )


def check_recipient(contact):
    if not contact.phone_e164:
        raise ValidationError(f"Add a valid phone number for {contact} first.")


def log_sent(connection, request, *, contact, body, actor, matter):
    """Log a sent text under the provider's message ID."""
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
    record_communication(source_connection=connection, external_id=request.provider_ref, **fields)


def send_text(connection, *, contact, body, actor, matter=None):
    """Send ``body`` to the contact's phone; return the policy's ``OutboundRequest``."""
    body = body.strip()
    if not body:
        raise ValidationError("Write the text first.")
    if len(body) > MAX_LENGTH:
        raise ValidationError(f"Texts can be at most {MAX_LENGTH} characters.")
    check_recipient(contact)
    if not can_text_freely(connection, contact):
        raise ValidationError(window_message(connection, contact))
    request = policy.send(
        connection,
        SEND_TEXT,
        {"to": contact.phone_e164, "body": body, "staff_email": actor.email},
        actor=actor,
        target=contact,
        summary=f"Text from {actor.get_username()} ({len(body)} characters)",
    )
    if request.status == OutboundRequest.Status.SENT:
        log_sent(connection, request, contact=contact, body=body, actor=actor, matter=matter)
    return request


def send_template(connection, *, contact, template, values, actor, matter=None):
    """Send an approved ``template`` with ``values`` filled in; return the ``OutboundRequest``.

    Each value is one line, as messaging services require of template values.
    """
    check_recipient(contact)
    filled = {}
    for item in template.fields:
        value = " ".join(str(values.get(item.key, "")).split())
        if not value:
            raise ValidationError(f"Fill in {item.label}.")
        filled[item.key] = value
    body = template.render(filled)
    if len(body) > MAX_LENGTH:
        raise ValidationError(f"Texts can be at most {MAX_LENGTH} characters.")
    request = policy.send(
        connection,
        SEND_TEMPLATE,
        {
            "to": contact.phone_e164,
            "template": template.id,
            "values": filled,
            "staff_email": actor.email,
        },
        actor=actor,
        target=contact,
        # The template's name is the firm's own; the values may name the client.
        summary=f"Template {template.name} from {actor.get_username()}",
    )
    if request.status == OutboundRequest.Status.SENT:
        log_sent(connection, request, contact=contact, body=body, actor=actor, matter=matter)
    return request
