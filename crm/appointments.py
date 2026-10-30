"""Store the calendar events and bookings connectors bring in as appointments.

Calendar events are stored only when an attendee is a known contact, so staff
members' personal events stay out of the CRM. Bookings, such as consultations
booked through a scheduling tool, match or create the contact. Each appointment
is linked to its provider item, so re-syncs update it, and an appointment made by
hand within two minutes with the same title is reused rather than duplicated.
"""

import logging
from datetime import timedelta

from crm.connect import refs
from crm.connect.records import BookingRecord, CalendarEventRecord
from crm.connect.signals import records_received
from crm.matching import match_contact, match_staff, normalize_email, normalize_phone

from .models import Activity, Contact, WorkItem

logger = logging.getLogger(__name__)

DUPLICATE_WINDOW = timedelta(minutes=2)


def cancel(item):
    if item and not item.is_completed:
        if not item.title.startswith("Cancelled: "):
            item.title = f"Cancelled: {item.title}"[:255]
        item.is_completed = True
        item.save(update_fields=["title", "is_completed", "completed_at", "updated_at"])


def same_appointment(title, starts_at, contact):
    return (
        WorkItem.objects.filter(
            kind=WorkItem.Kind.APPOINTMENT,
            contact=contact,
            title__iexact=title,
            scheduled_for__gte=starts_at - DUPLICATE_WINDOW,
            scheduled_for__lte=starts_at + DUPLICATE_WINDOW,
        )
        .order_by("pk")
        .first()
    )


def save_appointment(connection, record, *, contact, description, etag=""):
    item, ref = refs.find(connection, WorkItem, record.external_id)
    if record.cancelled:
        cancel(item)
        return item
    if ref and etag and ref.etag == etag:
        return item
    if item is None:
        item = same_appointment(record.title, record.occurred_at, contact) or WorkItem(
            kind=WorkItem.Kind.APPOINTMENT, contact=contact
        )
    item.title = record.title[:255]
    item.scheduled_for = record.occurred_at
    item.description = description
    item.contact = item.contact or contact
    item.assigned_to = match_staff(record.staff_email) or connection.owner or item.assigned_to
    item.save()
    refs.link(connection, item, record.external_id, etag)
    return item


def store_event(connection, record):
    item, _ = refs.find(connection, WorkItem, record.external_id)
    if item is None and record.cancelled:
        return None
    contact = item.contact if item else None
    if contact is None:
        staff = normalize_email(record.staff_email)
        candidates = [e for e in record.attendee_emails if normalize_email(e) != staff]
        contacts = {c for c in map(match_contact, candidates) if c}
        if len(contacts) != 1:
            return None
        contact = contacts.pop()
    return save_appointment(
        connection, record, contact=contact, description=record.location, etag=record.etag
    )


def booking_contact(connection, record):
    contact = match_contact(record.invitee_email) or match_contact(record.invitee_phone)
    if contact:
        return contact
    first, _, last = (record.invitee_name or "").strip().partition(" ")
    contact = Contact.objects.create(
        first_name=(first or "Unknown")[:100],
        last_name=(last or "booking")[:100],
        email=normalize_email(record.invitee_email),
        phone=normalize_phone(record.invitee_phone) or record.invitee_phone[:40],
        stage=Contact.Stage.CONSULTATION,
        assigned_to=connection.owner,
    )
    Activity.objects.create(
        contact=contact,
        kind=Activity.Kind.SYSTEM,
        subject=f"Contact created from a booking through {connection.label}",
    )
    return contact


def store_booking(connection, record):
    item, _ = refs.find(connection, WorkItem, record.external_id)
    if item is None and record.cancelled:
        return None
    contact = item.contact if item and item.contact else booking_contact(connection, record)
    details = [record.location] if record.location else []
    details += [f"{question}: {answer}" for question, answer in record.answers.items()]
    return save_appointment(connection, record, contact=contact, description="\n".join(details))


def store_records(sender, connection, capability, records, **kwargs):
    for record in records:
        try:
            if isinstance(record, CalendarEventRecord):
                store_event(connection, record)
            elif isinstance(record, BookingRecord):
                store_booking(connection, record)
        except Exception:
            # The provider record may hold client data, so log only what identifies it.
            logger.exception(
                "Could not store %s record from connection %s", record.kind, connection.pk
            )


def connect():
    records_received.connect(store_records, dispatch_uid="appointments-store-records")
