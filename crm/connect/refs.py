"""Find and record the CRM record a provider item was stored as."""

from django.contrib.contenttypes.models import ContentType

from .models import ExternalRef


def find(connection, model, external_id):
    """The record stored for this provider item, and its ref; (None, None) if there is none."""
    ref = ExternalRef.objects.filter(
        connection=connection,
        content_type=ContentType.objects.get_for_model(model),
        external_id=external_id,
    ).first()
    if ref is None:
        return None, None
    record = model._default_manager.filter(pk=ref.object_id).first()
    if record is None:
        ref.delete()
        return None, None
    return record, ref


def link(connection, record, external_id, etag="", *, created_by_crm=None):
    """Link a record to a provider item; ``created_by_crm`` marks items the CRM made there."""
    defaults = {"object_id": str(record.pk), "etag": etag}
    if created_by_crm is not None:
        defaults["created_by_crm"] = created_by_crm
    ref, _ = ExternalRef.objects.update_or_create(
        connection=connection,
        content_type=ContentType.objects.get_for_model(type(record)),
        external_id=external_id,
        defaults=defaults,
    )
    return ref
