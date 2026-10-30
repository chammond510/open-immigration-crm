"""Sending an email from a staff member's connected mailbox.

Core knows only the ``send_email`` action that mailbox connectors declare, with
the payload ``{"to": address, "subject": text, "body": plain text, "staff_email":
sender's email}``; the connector returns the provider's message ID. Every email
goes through the outbound policy, which holds it in preview mode and records the
attempt. A sent email is logged as an outbound communication under that ID, so
the mailbox's own copy, when it syncs, lands on the same record.
"""

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone

from crm.connect import policy
from crm.connect.base import Capability
from crm.connect.models import OutboundRequest

from .models import Communication
from .outbound import connections_for
from .services import record_communication

SEND_EMAIL = "send_email"
MAX_SUBJECT = 255
MAX_BODY = 20_000


def email_connections(user):
    """Active mailbox connections this person can send email through."""
    return connections_for(user, SEND_EMAIL, Capability.MAILBOX)


def send_email(connection, *, contact, subject, body, actor, matter=None):
    """Email the contact; return the policy's ``OutboundRequest``."""
    subject, body = " ".join(subject.split()), body.strip()
    if not subject or not body:
        raise ValidationError("Write a subject and a message.")
    if len(subject) > MAX_SUBJECT or len(body) > MAX_BODY:
        raise ValidationError(
            f"Keep the subject under {MAX_SUBJECT} and the message under {MAX_BODY} characters."
        )
    try:
        validate_email(contact.email)
    except ValidationError:
        raise ValidationError(f"Add a valid email address for {contact} first.") from None
    request = policy.send(
        connection,
        SEND_EMAIL,
        {"to": contact.email, "subject": subject, "body": body, "staff_email": actor.email},
        actor=actor,
        target=contact,
        summary=f"Email from {actor.get_username()} ({len(body)} characters)",
    )
    if request.status == OutboundRequest.Status.SENT:
        record_communication(
            source_connection=connection,
            external_id=request.provider_ref,
            channel=Communication.Channel.EMAIL,
            direction=Communication.Direction.OUTBOUND,
            occurred_at=timezone.now(),
            counterpart=contact.email.lower(),
            subject=subject,
            summary=body,
            contact=contact,
            matter=matter,
            logged_by=actor,
        )
    return request
