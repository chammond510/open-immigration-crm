"""Apply e-signature providers' reports to the requests the CRM sent.

A report about a document the CRM did not send is ignored. Once a request is
finished (signed, declined, or cancelled), later reports change nothing, so a
repeated or late delivery cannot reopen it. When everyone has signed, the signed
PDF is filed on the matter beside the original.
"""

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils.text import slugify

from crm.connect.records import SignatureRecord
from crm.connect.signals import records_received
from crm.models import Activity, Document

from .models import SignatureRequest
from .services import PDF, finish


def mark(signature_request, email, status):
    for signer in signature_request.signers:
        if not email or signer["email"].lower() == email.lower():
            signer["status"] = status


def file_signed(signature_request, pdf):
    source = signature_request.document
    name = f"{slugify(signature_request.title)[:80] or 'document'}-signed.pdf"
    document = Document(
        matter=signature_request.matter,
        contact=source.contact if source else signature_request.matter.primary_contact,
        title=f"{signature_request.title} (signed)"[:255],
        category=source.category if source else Document.Category.OTHER,
        original_filename=name,
        mime_type=PDF,
        size_bytes=len(pdf),
    )
    document.file.save(name, ContentFile(pdf), save=False)
    document.save()
    return document


@transaction.atomic
def store(connection, record):
    # Lock only the request's own row: PostgreSQL cannot lock the nullable side of the
    # join that loading its document with it would add.
    signature_request = (
        SignatureRequest.objects.select_for_update()
        .filter(connection=connection, external_id=record.external_id)
        .first()
    )
    if signature_request is None or not signature_request.is_open:
        return
    if record.status == "signed":
        mark(signature_request, record.signer_email, "signed")
        signature_request.save(update_fields=["signers", "updated_at"])
    elif record.status == "declined":
        mark(signature_request, record.signer_email, "declined")
        finish(signature_request, SignatureRequest.Status.DECLINED)
    elif record.status == "cancelled":
        finish(signature_request, SignatureRequest.Status.CANCELLED)
    elif record.status == "completed":
        mark(signature_request, "", "signed")
        signature_request.signed_document = file_signed(signature_request, record.signed_pdf)
        signature_request.save(update_fields=["signed_document"])
        finish(signature_request, SignatureRequest.Status.COMPLETED)
        Activity.objects.create(
            matter=signature_request.matter,
            contact=signature_request.signed_document.contact,
            kind=Activity.Kind.DOCUMENT,
            subject=f"Signed and filed: {signature_request.title}",
        )


def store_records(sender, connection, capability, records, **kwargs):
    for record in records:
        if isinstance(record, SignatureRecord) and not record.problems():
            store(connection, record)


def connect():
    records_received.connect(store_records, dispatch_uid="signatures-store-records")
