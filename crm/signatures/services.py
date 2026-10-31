"""Sending matter documents for signature, and filing what comes back.

Core knows only the standard actions e-signature connectors declare, both run
through the outbound policy:

- ``send_for_signature``, with the payload ``{"title", "filename", "pdf" (base64),
  "signers": [{"name", "email"}], "message"}``. The provider emails each signer and
  returns ``{"id"}``, its ID for the document.
- ``cancel_signature``, with ``{"document_id"}``.

Providers then report progress as ``SignatureRecord``s, which
``crm/signatures/records.py`` applies to the request. Only PDFs are sent, to the
matter's own contacts, by email address on file.
"""

import base64

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone

from crm.connect import policy
from crm.connect.base import Capability
from crm.connect.models import OutboundRequest
from crm.connect.services import usable_connections
from crm.models import Activity, AuditLog

from .models import SignatureRequest

SEND, CANCEL = "send_for_signature", "cancel_signature"
MAX_SIGNERS = 10
PDF = "application/pdf"


def signature_connections(user):
    """E-signature connections ``user`` may send through: the firm's and their own."""
    return usable_connections(user, SEND, Capability.ESIGNATURE)


def signer_choices(matter):
    """The matter's contacts that have an email address, primary contact first."""
    contacts = [matter.primary_contact, *(party.contact for party in matter.parties.all())]
    found, seen = [], set()
    for contact in contacts:
        if contact is None or contact.pk in seen:
            continue
        seen.add(contact.pk)
        try:
            validate_email(contact.email)
        except ValidationError:
            continue
        found.append(contact)
    return found


def not_sent(request, noun):
    if request.status == OutboundRequest.Status.PREVIEWED:
        return f"Nothing was {noun}: the e-signature service is in preview mode."
    return f"Nothing was {noun}: {(request.error or 'the service refused it').rstrip('.')}."


def record(actor, action, target, detail=""):
    AuditLog.objects.create(
        user=actor,
        action=action,
        target_type=target.__class__.__name__,
        target_id=str(target.pk),
        detail=detail[:500],
    )


def send(connection, document, *, signers, title, message, actor):
    """Send a matter PDF for signature; return the new ``SignatureRequest``."""
    title = " ".join(title.split())[:255] or document.title
    if document.mime_type != PDF:
        raise ValidationError("Only PDF documents can be sent for signature.")
    if not 1 <= len(signers) <= MAX_SIGNERS:
        raise ValidationError(f"Choose between 1 and {MAX_SIGNERS} signers.")
    try:
        with document.file.open("rb") as handle:
            pdf = handle.read()
    except (OSError, ValueError):
        raise ValidationError("The document's file could not be read.") from None
    request = policy.send(
        connection,
        SEND,
        {
            "title": title,
            "filename": document.original_filename,
            "pdf": base64.b64encode(pdf).decode(),
            "signers": [{"name": str(c), "email": c.email} for c in signers],
            "message": message.strip()[:1000],
        },
        actor=actor,
        target=document.matter,
        summary=f"Document sent for signature to {len(signers)} signer(s)",
    )
    if request.status != OutboundRequest.Status.SENT or not request.provider_ref:
        raise ValidationError(not_sent(request, "sent"))
    sent = SignatureRequest.objects.create(
        matter=document.matter,
        document=document,
        title=title,
        signers=[
            {"name": str(c), "email": c.email, "contact": str(c.pk), "status": "pending"}
            for c in signers
        ],
        connection=connection,
        external_id=request.provider_ref,
        sent_by=actor,
    )
    Activity.objects.create(
        matter=document.matter,
        kind=Activity.Kind.DOCUMENT,
        subject=f"Sent for signature: {title}",
        body="Signers: " + ", ".join(str(c) for c in signers),
        created_by=actor,
    )
    record(actor, "signature.sent", sent, connection.label)
    return sent


def cancel(signature_request, actor):
    """Withdraw a request that is still out for signature."""
    if not signature_request.is_open or signature_request.connection is None:
        raise ValidationError("Only a request that is out for signature can be cancelled.")
    request = policy.send(
        signature_request.connection,
        CANCEL,
        {"document_id": signature_request.external_id},
        actor=actor,
        target=signature_request.matter,
        summary="Signature request cancelled",
    )
    if request.status != OutboundRequest.Status.SENT:
        raise ValidationError(not_sent(request, "cancelled"))
    finish(signature_request, SignatureRequest.Status.CANCELLED)
    record(actor, "signature.cancelled", signature_request)


def finish(signature_request, status):
    signature_request.status = status
    signature_request.finished_at = timezone.now()
    signature_request.save(update_fields=["status", "finished_at", "signers", "updated_at"])
    if status != SignatureRequest.Status.COMPLETED:
        Activity.objects.create(
            matter=signature_request.matter,
            kind=Activity.Kind.DOCUMENT,
            subject=f"Signature {signature_request.get_status_display().lower()}: "
            f"{signature_request.title}",
        )
