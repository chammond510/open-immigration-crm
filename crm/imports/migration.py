"""Records from a migration connector, such as another practice system, made into an import.

A sync of the migration capability brings every contact and matter the connection can
read. Core leaves out what was imported from that connection before, and turns the
rest into an import that someone who imports reviews and confirms like a spreadsheet: the
same matching, the same preview, the same report. A matter's row carries its client;
contacts that are no matter's client get rows of their own. Each row's IDs at the
provider are kept with the file, so the import can link what it saves, and the next
run brings only what is new.
"""

import csv
import io
import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from crm.connect.base import Capability
from crm.connect.models import ExternalRef
from crm.connect.records import ContactRecord, MatterRecord
from crm.connect.signals import records_received
from crm.matching import match_staff
from crm.models import Contact, Matter

from .models import SpreadsheetImport
from .spreadsheet import MAX_ROWS, STANDARD_COLUMNS, suggest_mapping

logger = logging.getLogger(__name__)

COLUMNS = {column.key: column.label for column in STANDARD_COLUMNS}
CONTACT_FIELDS = (
    "first_name",
    "last_name",
    "email",
    "phone",
    "date_of_birth",
    "address_line1",
    "address_line2",
    "city",
    "region",
    "postal_code",
    "country",
    "contact_status",
    "stage",
)
MATTER_FIELDS = ("matter_title", "case_type", "matter_status", "matter_description", "assigned_to")
HEADERS = [COLUMNS[key] for key in (*CONTACT_FIELDS, *MATTER_FIELDS)]
COMPANY = "A company; the CRM's contacts are people, so add it by hand if needed."


def imported_ids(connection, model):
    return set(
        ExternalRef.objects.filter(
            connection=connection, content_type=ContentType.objects.get_for_model(model)
        ).values_list("external_id", flat=True)
    )


def contact_cells(record, matters):
    """A contact's columns; a client's status and stage follow its matters."""
    cells = {
        "first_name": record.first_name,
        "last_name": record.last_name,
        "email": record.email,
        "phone": record.phone,
        "date_of_birth": record.date_of_birth.isoformat() if record.date_of_birth else "",
        "address_line1": record.address_line1,
        "address_line2": record.address_line2,
        "city": record.city,
        "region": record.region,
        "postal_code": record.postal_code,
        "country": record.country,
    }
    if matters:
        current = any(matter.status != "closed" for matter in matters)
        cells["contact_status"] = Contact.Status.CLIENT if current else Contact.Status.FORMER
        cells["stage"] = Contact.Stage.ACTIVE if current else Contact.Stage.CLOSED
    return cells


def matter_cells(record, provider, staff):
    lines = []
    if record.number:
        lines.append(f"Matter {record.number} in {provider}.")
    if record.opened_on:
        lines.append(f"Opened {record.opened_on.isoformat()}.")
    if record.practice_area:
        lines.append(f"Practice area: {record.practice_area}.")
    if record.description:
        lines.append(record.description)
    if record.responsible_email not in staff:
        staff[record.responsible_email] = match_staff(record.responsible_email)
    person = staff[record.responsible_email]
    return {
        "matter_title": record.title,
        "case_type": record.case_type,
        "matter_status": record.status,
        "matter_description": "\n".join(lines),
        # A responsible person who is not active staff here is left to the default.
        "assigned_to": person.email if person else "",
    }


def build_import(connection, records):
    """The import for a sync's records, or None when every record was imported before."""
    provider = connection.connector_class.name if connection.connector_class else "the source"
    contacts = {r.external_id: r for r in records if isinstance(r, ContactRecord)}
    all_matters = [r for r in records if isinstance(r, MatterRecord)]
    done_matters = imported_ids(connection, Matter)
    done_contacts = imported_ids(connection, Contact)
    matters = [m for m in all_matters if m.external_id not in done_matters]
    by_client = {}
    for matter in all_matters:
        by_client.setdefault(matter.client, []).append(matter)

    rows, refs, staff = [], {}, {}

    def add(cells, ref):
        rows.append([cells.get(key, "") for key in (*CONTACT_FIELDS, *MATTER_FIELDS)])
        # Row 1 is the header row.
        refs[str(len(rows) + 1)] = ref

    for matter in matters:
        client = contacts.get(matter.client)
        ref = {"contact": matter.client, "matter": matter.external_id}
        ref["parties"] = [list(party) for party in matter.parties]
        cells = matter_cells(matter, provider, staff)
        if client is None:
            ref["skip"] = "Its client was not among the records fetched."
        elif client.company:
            ref["skip"] = "Its client is a company; the CRM's contacts are people."
            cells["first_name"] = client.company
        else:
            cells.update(contact_cells(client, by_client.get(client.external_id)))
        add(cells, ref)
    clients = {matter.client for matter in matters}
    for record in contacts.values():
        if record.external_id in done_contacts or record.external_id in clients:
            continue
        ref = {"contact": record.external_id}
        if record.company:
            ref["skip"] = COMPANY
            add({"first_name": record.company}, ref)
        else:
            add(contact_cells(record, by_client.get(record.external_id)), ref)
    if all(ref.get("skip") for ref in refs.values()):
        # Only rows that can never be imported, such as companies: nothing to review.
        return None

    later = max(0, len(rows) - MAX_ROWS)
    rows = rows[:MAX_ROWS]
    refs = {number: ref for number, ref in refs.items() if int(number) <= MAX_ROWS + 1}
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADERS)
    writer.writerows(rows)
    fetched = timezone.localtime().strftime("%b %-d, %Y %-I:%M %p")
    with transaction.atomic():
        # A newer fetch replaces one that was never confirmed.
        connection.imports.filter(status=SpreadsheetImport.Status.PREPARING).delete()
        return SpreadsheetImport.objects.create(
            filename=f"{provider}, fetched {fetched}"[:255],
            connection=connection,
            content=buffer.getvalue(),
            headers=HEADERS,
            row_count=len(rows),
            mapping=suggest_mapping(HEADERS),
            options={
                "date_order": "ymd",
                "contact_status": Contact.Status.PROSPECT,
                "stage": Contact.Stage.INQUIRY,
                "matter_status": Matter.Status.OPEN,
                "assigned_to": None,
                "imported_before": len(all_matters) - len(matters),
                "later": later,
            },
            refs=refs,
        )


def store_records(sender, connection, capability, records, source, **kwargs):
    if capability != Capability.MIGRATION:
        return
    spreadsheet = build_import(connection, records)
    if spreadsheet:
        logger.info("Records from a migration connection are ready to review.")


def connect():
    records_received.connect(store_records, dispatch_uid="imports-store-migration-records")
