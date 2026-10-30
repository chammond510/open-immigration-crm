"""Leads from lead-capture connections, such as the firm's website form.

A lead is matched to a contact by email or phone. A new person becomes a contact at
the Inquiry stage, with the lead source filled from the connection's settings, as
for bookings. Either way, the submission is kept as an intake note on the contact:
the message, the form's other answers, and its tracking values. An existing contact
is never changed otherwise.
"""

import logging

from crm.connect.records import LeadRecord
from crm.connect.signals import records_received
from crm.matching import match_contact, normalize_email, normalize_phone

from .appointments import lead_source
from .models import Activity, Contact

logger = logging.getLogger(__name__)


def lead_contact(connection, record):
    contact = match_contact(record.email) or match_contact(record.phone)
    if contact:
        return contact, False
    first, last = record.first_name.strip(), record.last_name.strip()
    if not (first or last):
        first, _, last = record.name.strip().partition(" ")
    contact = Contact.objects.create(
        first_name=(first or "Unknown")[:100],
        last_name=(last or "lead")[:100],
        email=normalize_email(record.email),
        phone=normalize_phone(record.phone) or record.phone[:40],
        status=Contact.Status.LEAD,
        stage=Contact.Stage.INQUIRY,
        assigned_to=connection.owner,
        custom_fields=lead_source(connection, record),
    )
    return contact, True


def store_lead(connection, record):
    contact, created = lead_contact(connection, record)
    lines = [record.message.strip()] if record.message.strip() else []
    lines += [f"{question}: {answer}" for question, answer in record.answers.items()]
    tracking = "; ".join(f"{k}: {v}" for k, v in sorted(record.tracking.items()) if v)
    if tracking:
        lines.append(f"Tracking: {tracking}")
    form = f" ({record.form})" if record.form else ""
    Activity.objects.create(
        contact=contact,
        kind=Activity.Kind.INTAKE,
        subject=(
            f"New lead from {connection.label}{form}"
            if created
            else f"Form submission through {connection.label}{form}"
        )[:255],
        body="\n".join(lines)[:10000],
        occurred_at=record.occurred_at,
    )
    return contact


def store_records(sender, connection, capability, records, **kwargs):
    for record in records:
        if not isinstance(record, LeadRecord):
            continue
        try:
            store_lead(connection, record)
        except Exception:
            # The record holds a person's details, so log only what identifies it.
            logger.exception("Could not store a lead from connection %s", connection.pk)


def connect():
    records_received.connect(store_records, dispatch_uid="leads-store-records")
