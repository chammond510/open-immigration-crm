import os
import re
import uuid

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from crm.models import Activity, AuditLog, Document, Matter
from crm.validators import validate_document

from .models import Communication, CommunicationAttachment

UNSAFE_NAME = re.compile(r"[\\/\x00-\x1f\x7f]+")


def record_communication(*, source_connection=None, external_id="", **fields):
    """Create a communication, or update the one a connector already recorded.

    Connectors pass ``source_connection`` and the provider's ``external_id`` so a
    record that syncs twice, or arrives by both sync and webhook, is stored once.
    """
    if source_connection is not None and external_id:
        communication, _ = Communication.objects.update_or_create(
            source_connection=source_connection,
            external_id=external_id,
            defaults=fields,
        )
        return communication
    return Communication.objects.create(
        source_connection=source_connection, external_id=external_id, **fields
    )


def set_texting_opt_out(contact, opted_out, *, actor=None, reason=""):
    """Record that a contact opted out of texts, or gave consent again."""
    if bool(contact.texting_opted_out_at) == opted_out:
        return False
    contact.texting_opted_out_at = timezone.now() if opted_out else None
    contact.save(update_fields=["texting_opted_out_at", "updated_at"])
    AuditLog.objects.create(
        user=actor,
        action="contact.texting_opted_out" if opted_out else "contact.texting_opted_in",
        target_type="Contact",
        target_id=str(contact.pk),
        detail=reason[:500],
    )
    return True


def keep_attachment(communication, attachment):
    """Store a file that came with a communication; return a note for staff if it is not kept.

    It is kept on the same terms as a document staff upload: a PDF, Word document,
    PNG, or JPEG by its contents, under the upload size limit.
    """
    name = UNSAFE_NAME.sub(" ", os.path.basename(attachment.filename or "")).strip()[:255]
    name = name or "attachment"
    upload = ContentFile(attachment.content, name=name)
    try:
        mime_type = validate_document(upload)
    except ValidationError as exc:
        return f"[{name} was not kept. {' '.join(exc.messages)}]"
    stored = CommunicationAttachment(
        communication=communication,
        original_filename=name,
        mime_type=mime_type,
        size_bytes=len(attachment.content),
    )
    # Stored under a name of the CRM's own, never the sender's.
    stored.file.save(uuid.uuid4().hex + os.path.splitext(name)[1].lower(), upload, save=False)
    stored.save()
    return ""


def keep_attachments(communication, attachments):
    """Store a new communication's files, adding a note to it for any not kept."""
    notes = [note for item in attachments if (note := keep_attachment(communication, item))]
    if notes:
        communication.summary = "\n\n".join([communication.summary, *notes]).strip()
        communication.save(update_fields=["summary", "updated_at"])


def delete_attachment_file(sender, instance, **kwargs):
    """Remove a deleted attachment's file, once the deletion is committed."""
    if instance.file:
        storage, name = instance.file.storage, instance.file.name
        transaction.on_commit(lambda: storage.delete(name))


def contact_matters(contact):
    """The matters a contact is the primary contact of, or a party to."""
    return (
        Matter.objects.filter(Q(primary_contact=contact) | Q(parties__contact=contact))
        .distinct()
        .order_by("-created_at")
    )


def save_to_matter(attachment, matter, *, title, category, actor):
    """Save a copy of an attachment as one of the matter's documents, and note it there."""
    contact = attachment.communication.contact
    if contact is None or not contact_matters(contact).filter(pk=matter.pk).exists():
        raise ValidationError("Choose one of the contact's matters.")
    with attachment.file.open("rb") as handle:
        content = handle.read()
    document = Document(
        matter=matter,
        contact=contact,
        title=title.strip()[:255] or attachment.original_filename,
        category=category,
        original_filename=attachment.original_filename,
        mime_type=attachment.mime_type,
        size_bytes=len(content),
        uploaded_by=actor,
    )
    extension = os.path.splitext(attachment.original_filename)[1].lower()
    document.file.save(uuid.uuid4().hex + extension, ContentFile(content), save=False)
    document.save()
    attachment.saved_document = document
    attachment.save(update_fields=["saved_document"])
    Activity.objects.create(
        matter=matter,
        contact=contact,
        kind=Activity.Kind.DOCUMENT,
        subject=f"Document saved from a message: {document.title}",
        created_by=actor,
    )
    return document
