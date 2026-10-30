"""Email the CRM sends itself, such as secure intake links and payment plan reminders.

Core knows only the ``send_system_email`` action that system email connectors
declare, with the payload ``{"to": address, "subject": text, "body": plain text,
"sender_name": the firm's name}``; the connector returns the message ID. The firm
has one sender: its oldest active, firm-wide connection with the System email
capability. Every email goes through the outbound policy, which holds it in
preview mode and records the attempt. A sent email is logged on the contact with
a summary the caller writes, so a secret such as an intake link is never kept.
"""

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone

from crm.connect import policy
from crm.connect.base import Capability
from crm.connect.models import Connection, OutboundRequest
from crm.models import FirmProfile

from .models import Communication
from .services import record_communication

SEND_SYSTEM_EMAIL = "send_system_email"
MAX_SUBJECT = 255
MAX_BODY = 20_000


def sender():
    """The connection the CRM's own email goes through, or None."""
    for connection in Connection.objects.filter(
        status=Connection.Status.ACTIVE, owner__isnull=True
    ).order_by("created_at"):
        cls = connection.connector_class
        if (
            cls is not None
            and Capability.SYSTEM_EMAIL in connection.enabled_capabilities
            and cls.outbound_actions.get(SEND_SYSTEM_EMAIL) == Capability.SYSTEM_EMAIL
        ):
            return connection
    return None


def address_problem(contact):
    """Why the CRM cannot email this contact, or an empty string."""
    try:
        validate_email(contact.email)
    except ValidationError:
        return f"Add a valid email address for {contact} first."
    return ""


def send_system_email(contact, *, subject, body, log_summary, purpose, actor=None, matter=None):
    """Email the contact from the firm; return the policy's ``OutboundRequest``.

    ``purpose`` names the kind of email, such as "Intake link", for the policy's
    record; ``log_summary`` is what the contact's communication log shows.
    """
    connection = sender()
    if connection is None:
        raise ValidationError("Connect an email delivery service first.")
    problem = address_problem(contact)
    if problem:
        raise ValidationError(problem)
    subject, body = " ".join(subject.split())[:MAX_SUBJECT], body.strip()[:MAX_BODY]
    request = policy.send(
        connection,
        SEND_SYSTEM_EMAIL,
        {
            "to": contact.email,
            "subject": subject,
            "body": body,
            "sender_name": FirmProfile.load().name,
        },
        actor=actor,
        is_automation=actor is None,
        target=contact,
        summary=f"{purpose} email",
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
            summary=log_summary,
            contact=contact,
            matter=matter,
            logged_by=actor,
        )
    return request


def not_sent_reason(request):
    """Why an email was not sent, in words for staff; empty when it was."""
    if request.status == OutboundRequest.Status.SENT:
        return ""
    if request.status == OutboundRequest.Status.PREVIEWED:
        return "the email delivery service is in preview mode"
    return (request.error or "it could not be sent").rstrip(".")
