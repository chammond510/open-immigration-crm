from django.utils import timezone

from crm.models import AuditLog

from .models import Communication


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
