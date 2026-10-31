"""Store the calls, texts, and emails connectors bring in, and match them to contacts.

A record whose number or address matches no contact, or several, is stored
without one and waits in the Needs matching list. Linking it by hand, or saving
a contact with that number or address, links it and others like it.
"""

import logging
from datetime import timedelta

from django.db.models.signals import post_save

from crm.connect.records import INBOUND, OUTBOUND, CallRecord, EmailRecord, TextRecord
from crm.connect.signals import records_received
from crm.matching import match_contact, match_staff, normalize_email
from crm.models import Contact

from .models import Communication
from .services import record_communication, set_texting_opt_out

logger = logging.getLogger(__name__)

# Carrier keywords (CTIA): these replies opt a number out of texts, or back in.
STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
START_WORDS = {"START", "UNSTOP"}
# How far along a text's delivery is; a status never moves to a lower rank.
STATUS_RANK = {
    "queued": 1,
    "accepted": 1,
    "sending": 2,
    "sent": 3,
    "delivered": 4,
    "undelivered": 4,
    "failed": 4,
    "read": 5,
}
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
        fields["delivery_status"] = record.status[:20]
    else:
        fields["subject"] = record.subject[:255]
        fields["summary"] = record.snippet
    return fields


def keep_stored_values(fields, existing):
    """A later copy of a record, such as a delivery update, adds to what is stored.

    The first time seen is kept, empty values do not erase stored ones, new summary
    paragraphs are added, and a delivery status never moves back, since providers'
    updates can arrive out of order.
    """
    del fields["occurred_at"]
    for name in ("counterpart", "subject", "summary", "duration", "delivery_status"):
        if name in fields and not fields[name]:
            del fields[name]
    status = fields.get("delivery_status")
    if status and STATUS_RANK.get(status, 0) < STATUS_RANK.get(existing.delivery_status, 0):
        del fields["delivery_status"]
    # Providers send parts of a record separately, such as a call's recording link and
    # its recap, so new paragraphs are added to the stored ones.
    # A paragraph already in the stored text, such as a mailbox's snippet of an email
    # the CRM sent, is not added again.
    if fields.get("summary") and existing.summary:
        stored = " ".join(existing.summary.split())
        paragraphs = existing.summary.split("\n\n")
        paragraphs += [
            p
            for p in fields["summary"].split("\n\n")
            if p not in paragraphs and " ".join(p.split()) not in stored
        ]
        fields["summary"] = "\n\n".join(paragraphs)


def store_one(connection, record):
    fields = communication_fields(record)
    existing = Communication.objects.filter(
        source_connection=connection, external_id=record.external_id
    ).first()
    if existing:
        keep_stored_values(fields, existing)
    elif isinstance(record, TextRecord) and record.direction == OUTBOUND and not record.body:
        # A delivery update for a text this CRM has no copy of, such as one another
        # app sent from the same number.
        return None
    elif isinstance(record, EmailRecord) and match_staff(record.remote):
        # Email between staff members is internal, not client correspondence.
        return None
    contact = match_contact(record.remote)
    if not contact and not existing and (connection.config or {}).get("unmatched") == "skip":
        # The connection is set to keep only records from known contacts.
        return None
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
        if record.opt_out == "stop" or keyword in STOP_WORDS:
            set_texting_opt_out(contact, True, reason=f"Replied {keyword or 'STOP'} by text")
        elif record.opt_out == "start" or keyword in START_WORDS:
            set_texting_opt_out(contact, False, reason=f"Replied {keyword or 'START'} by text")
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
