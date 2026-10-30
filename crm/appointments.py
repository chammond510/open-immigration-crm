"""Appointments and calendars: events and bookings coming in, appointments going out.

Calendar events are stored only when an attendee is a known contact, so staff
members' personal events stay out of the CRM. Bookings, such as consultations
booked through a scheduling tool, match or create the contact. Each appointment
is linked to its provider item, so re-syncs update it, and an appointment made by
hand within two minutes with the same title is reused rather than duplicated.

Going out, an appointment assigned to a staff member is put on each calendar they
connected that declares the ``save_event`` action, and kept in step: moved when it
moves, and removed when it is deleted or given to someone else. The worker does
this through the outbound policy. The event has no attendees, so the calendar
sends no invitations. Only events the CRM created are changed: an appointment that
came from a calendar is updated from it, never written back, so its attendees are
never changed without notice. Changes that came from a calendar are not sent back.
"""

import contextvars
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django_tasks import task

from crm.connect import policy, refs
from crm.connect.base import Capability
from crm.connect.models import Connection, ExternalRef
from crm.connect.records import BookingRecord, CalendarEventRecord
from crm.connect.signals import records_received
from crm.matching import match_contact, match_staff, normalize_email, normalize_phone

from .models import Activity, Contact, WorkItem

logger = logging.getLogger(__name__)

DUPLICATE_WINDOW = timedelta(minutes=2)
SAVE_EVENT, DELETE_EVENT = "save_event", "delete_event"
# Appointments have a start time only; calendars show them this long.
APPOINTMENT_LENGTH = timedelta(hours=1)
# Set while storing what calendars sent, so those changes are not sent back.
receiving = contextvars.ContextVar("receiving_calendar_records", default=False)


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
    item.description = description or item.description
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
    if item is None:
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
    token = receiving.set(True)
    try:
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
    finally:
        receiving.reset(token)


def calendar_connections(user):
    """The active calendars ``user`` connected that take the CRM's appointments."""
    if user is None:
        return []
    return [
        connection
        for connection in Connection.objects.filter(owner=user, status=Connection.Status.ACTIVE)
        if connection.connector_class is not None
        and Capability.CALENDAR in connection.enabled_capabilities
        and connection.connector_class.outbound_actions.get(SAVE_EVENT) == Capability.CALENDAR
    ]


def event_refs(item):
    """Links from this appointment to events on calendars that take appointments."""
    found = ExternalRef.objects.filter(
        content_type__app_label="crm", content_type__model="workitem", object_id=str(item.pk)
    ).select_related("connection")
    return [
        ref
        for ref in found
        if ref.connection.connector_class is not None
        and DELETE_EVENT in ref.connection.connector_class.outbound_actions
    ]


def event_payload(item, event_id):
    lines = [item.description] if item.description else []
    if item.contact_id:
        lines.append(f"Contact: {item.contact}")
    if item.matter_id:
        lines.append(f"Matter: {item.matter.matter_number}")
    lines.append("From Open Immigration CRM.")
    return {
        "event_id": event_id,
        "title": item.title,
        "starts_at": item.scheduled_for.isoformat(),
        "ends_at": (item.scheduled_for + APPOINTMENT_LENGTH).isoformat(),
        "time_zone": settings.TIME_ZONE,
        "description": "\n".join(lines),
        "crm_id": str(item.pk),
    }


@task()
def push_appointment(item_id):
    """Put an appointment on its staff member's calendars, or take it off the others."""
    item = (
        WorkItem.objects.select_related("contact", "matter")
        .filter(pk=item_id, kind=WorkItem.Kind.APPOINTMENT)
        .first()
    )
    if item is None:
        return
    wanted = {c.pk: c for c in calendar_connections(item.assigned_to)} if item.scheduled_for else {}
    linked = {}
    for ref in event_refs(item):
        if ref.connection_id in wanted:
            linked[ref.connection_id] = ref
        elif ref.created_by_crm:
            remove_event(ref.connection, ref.external_id, item.pk)
            ref.delete()
    if item.is_completed:
        # A finished appointment stays on the calendar as it was.
        return
    for connection in wanted.values():
        ref = linked.get(connection.pk)
        if ref is not None and not ref.created_by_crm:
            # The appointment came from this calendar, which already has it.
            continue
        outbound = policy.send(
            connection,
            SAVE_EVENT,
            event_payload(item, ref.external_id if ref else ""),
            is_automation=True,
            target=item,
            summary="Appointment put on the staff member's calendar",
        )
        if outbound.provider_ref and (ref is None or ref.external_id != outbound.provider_ref):
            if ref is not None:
                ref.delete()
            refs.link(connection, item, outbound.provider_ref, created_by_crm=True)


def remove_event(connection, event_id, item_pk):
    # The event is on the owner's calendar for their appointment, so removing it is
    # keeping their own calendar in step.
    target = WorkItem(pk=item_pk, assigned_to_id=connection.owner_id)
    policy.send(
        connection,
        DELETE_EVENT,
        {"event_id": event_id},
        is_automation=True,
        target=target,
        summary="Appointment taken off the staff member's calendar",
    )


@task()
def remove_appointment_event(connection_id, event_id, item_pk):
    connection = Connection.objects.filter(pk=connection_id).first()
    if connection is not None:
        remove_event(connection, event_id, item_pk)


def appointment_saved(sender, instance, raw=False, **kwargs):
    if raw or receiving.get() or instance.kind != WorkItem.Kind.APPOINTMENT:
        return
    if calendar_connections(instance.assigned_to) or event_refs(instance):
        transaction.on_commit(lambda: push_appointment.enqueue(instance.pk))


def appointment_deleted(sender, instance, **kwargs):
    if instance.kind != WorkItem.Kind.APPOINTMENT:
        return
    # Django clears the primary key after deleting, before the task is queued.
    item_pk = instance.pk
    for ref in event_refs(instance):
        if ref.created_by_crm:
            transaction.on_commit(
                lambda ref=ref: remove_appointment_event.enqueue(
                    str(ref.connection_id), ref.external_id, item_pk
                )
            )
        ref.delete()


def connect():
    records_received.connect(store_records, dispatch_uid="appointments-store-records")
    post_save.connect(appointment_saved, sender=WorkItem, dispatch_uid="appointments-push")
    post_delete.connect(appointment_deleted, sender=WorkItem, dispatch_uid="appointments-remove")
