"""Store the calls, texts, and emails connectors bring in, and match them to contacts.

A record whose number or address matches no contact, or several, is stored
without one and waits in the Needs matching list. Linking it by hand, or saving
a contact with that number or address, links it and others like it.
"""

import logging
from datetime import timedelta

from django.db.models.signals import post_save

from crm.connect.records import INBOUND, CallRecord, EmailRecord, TextRecord
from crm.connect.signals import records_received
from crm.matching import match_contact, match_staff, normalize_email
from crm.models import Contact

from .models import Communication
from .services import record_communication, set_texting_opt_out

logger = logging.getLogger(__name__)

# Carrier keywords (CTIA): these replies opt a number out of texts, or back in.
STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
START_WORDS = {"START", "UNSTOP"}
CHANNELS = {
    CallRecord: Communication.Channel.CALL,
    TextRecord: Communication.Channel.TEXT,
    EmailRecord: Communication.Channel.EMAIL,
}


def communication_fields(record):
    fields = {
        "channel": CHANNELS[type(record)],
        "direction": record.direction,
        "occurred_at": record.occurred_at,
        "counterpart": record.remote[:255],
    }
    if isinstance(record, CallRecord):
        if record.duration_seconds:
            fields["duration"] = timedelta(seconds=record.duration_seconds)
        fields["subject"] = f"Call {record.outcome}".strip() if record.outcome else ""
        notes = [record.summary] if record.summary else []
        if record.recording_url:
            notes.append(f"Recording: {record.recording_url}")
        fields["summary"] = "\n\n".join(notes)
    elif isinstance(record, TextRecord):
        fields["summary"] = record.body
    else:
        fields["subject"] = record.subject[:255]
        fields["summary"] = record.snippet
    return fields


def store_one(connection, record):
    fields = communication_fields(record)
    existing = Communication.objects.filter(
        source_connection=connection, external_id=record.external_id
    ).first()
    contact = match_contact(record.remote)
    # Never undo a link staff made by hand when the provider sends the record again.
    if contact or not (existing and existing.contact_id):
        fields["contact"] = contact
    staff = match_staff(record.staff_email) or connection.owner
    if staff or not existing:
        fields["logged_by"] = staff
    communication = record_communication(
        source_connection=connection, external_id=record.external_id, **fields
    )
    contact = communication.contact
    if isinstance(record, TextRecord) and record.direction == INBOUND and contact:
        keyword = record.body.strip().upper()
        if keyword in STOP_WORDS:
            set_texting_opt_out(contact, True, reason=f"Replied {keyword} by text")
        elif keyword in START_WORDS:
            set_texting_opt_out(contact, False, reason=f"Replied {keyword} by text")
    return communication


def store_records(sender, connection, capability, records, **kwargs):
    for record in records:
        if type(record) not in CHANNELS:
            continue
        try:
            store_one(connection, record)
        except Exception:
            # The provider record may hold client data, so log only what identifies it.
            logger.exception(
                "Could not store %s record from connection %s", record.kind, connection.pk
            )


def link_unmatched(contact):
    """Link unmatched communications whose number or address now uniquely means this contact."""
    keys = [key for key in (contact.phone_e164, normalize_email(contact.email)) if key]
    keys = [key for key in keys if match_contact(key) == contact]
    if not keys:
        return 0
    return Communication.objects.filter(
        contact__isnull=True, matter__isnull=True, counterpart_key__in=keys
    ).update(contact=contact)


def contact_saved(sender, instance, raw=False, **kwargs):
    if not raw:
        link_unmatched(instance)


def connect():
    records_received.connect(store_records, dispatch_uid="comms-store-records")
    post_save.connect(contact_saved, sender=Contact, dispatch_uid="comms-link-unmatched")
