"""Tracking the packages a firm sends, such as filings to USCIS.

A shipping connector asks which tracking numbers are due for a check through
``lookups.due("shipping", limit=..., carrier=...)``: those of its carrier on
shipments that are not yet delivered or returned, sent in the last 60 days, and
not checked in the last four hours, never-checked first. It reports each as a
``ShipmentRecord``. A delivery is noted on the matter; a problem or a return is
noted too, with a task for the matter's staff to look into it. Notes and tasks use
the CRM's words; the carrier's own are kept on the shipment for nine months after
its last check. Staff can also enter and update shipments by hand.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from crm.connect import lookups
from crm.connect.base import Capability
from crm.connect.records import ShipmentRecord
from crm.connect.signals import records_received, worker_ticked
from crm.models import Activity, WorkItem

from .models import Shipment, normalize_tracking_number

logger = logging.getLogger(__name__)

RECHECK_AFTER = timedelta(hours=4)
TRACK_FOR = timedelta(days=60)
KEEP_CARRIER_DETAILS = timedelta(days=270)
PROBLEMS = {
    "not_found": "The carrier has no record of this tracking number yet.",
    "unavailable": "The carrier could not be asked about this tracking number just now.",
    "not_shared": "The carrier would not share this package's tracking with the firm's "
    "account, so it is no longer checked. Update it by hand.",
}


def tracked(now=None, carrier=None):
    """Shipments a connection should keep tracking."""
    now = now or timezone.now()
    found = Shipment.objects.filter(track=True).exclude(tracking_number="")
    found = found.exclude(status__in=Shipment.SETTLED)
    found = found.filter(sent_on__gte=timezone.localdate(now) - TRACK_FOR)
    return found.filter(carrier=carrier) if carrier else found


def shipments_to_check(*, limit, carrier=None, now=None):
    """Tracking numbers due for a check, never-checked first, then the longest unchecked."""
    now = now or timezone.now()
    due = (
        tracked(now, carrier)
        .exclude(checked_at__gt=now - RECHECK_AFTER)
        .order_by(F("checked_at").asc(nulls_first=True), "sent_on")
        .values_list("tracking_number", flat=True)
    )
    numbers = []
    for number in due:
        if number not in numbers:
            numbers.append(number)
        if len(numbers) == limit:
            break
    return numbers


def note(shipment, subject, body):
    Activity.objects.create(
        matter=shipment.matter,
        contact=shipment.matter.primary_contact,
        kind=Activity.Kind.SYSTEM,
        subject=subject[:255],
        body=body,
    )


def follow_up(shipment, title):
    matter = shipment.matter
    WorkItem.objects.create(
        kind=WorkItem.Kind.TASK,
        title=title[:255],
        description=f"{shipment.get_carrier_display()} {shipment.tracking_number}: "
        f"{shipment.get_status_display()}. The carrier's latest update is on the matter's "
        "Shipments panel.",
        priority=WorkItem.Priority.HIGH,
        matter=matter,
        contact=matter.primary_contact,
        assigned_to=matter.assigned_to or matter.primary_contact.assigned_to,
    )


def record_reading(record):
    """Apply one tracking reading to the shipments with that number; return how many."""
    number = normalize_tracking_number(record.tracking_number)
    shipments = Shipment.objects.select_for_update().select_related("matter__primary_contact")
    updated = 0
    for shipment in shipments.filter(carrier=record.carrier, tracking_number=number).exclude(
        status__in=Shipment.SETTLED
    ):
        shipment.checked_at = record.occurred_at
        if record.problem:
            shipment.tracking_problem = PROBLEMS.get(record.problem, PROBLEMS["unavailable"])
            # Asking again would not change the carrier's answer, and spends the quota.
            shipment.track = record.problem != "not_shared"
            shipment.save(update_fields=["checked_at", "tracking_problem", "track", "updated_at"])
            updated += 1
            continue
        before = shipment.status
        shipment.status = record.status
        shipment.status_detail = record.detail[:255]
        shipment.delivered_at = record.delivered_at
        shipment.expected_on = record.expected_on
        shipment.tracking_problem = ""
        shipment.save()
        updated += 1
        if shipment.status == before:
            continue
        what = f"{shipment.description} ({shipment.get_carrier_display()} {number})"
        if shipment.status == Shipment.Status.DELIVERED:
            when = timezone.localtime(record.delivered_at or record.occurred_at)
            note(
                shipment,
                f"Delivered: {shipment.description}",
                f"{what} was delivered{f' to {shipment.sent_to}' if shipment.sent_to else ''} "
                f"on {when:%b %-d, %Y at %-I:%M %p}.",
            )
        elif shipment.status in (Shipment.Status.PROBLEM, Shipment.Status.RETURNED):
            note(
                shipment,
                f"{shipment.get_status_display()}: {shipment.description}",
                f"{shipment.get_carrier_display()} reports {what} as "
                f"{shipment.get_status_display().lower()}. Its latest update is on the "
                "Shipments panel.",
            )
            follow_up(shipment, f"Check on shipment: {shipment.description}")
    return updated


def forget_carrier_details(sender=None, now=None, **kwargs):
    """Clear a carrier's own words from shipments it last answered for nine months ago.

    Some carriers' terms, such as UPS's, limit how long their tracking data is kept.
    The shipment's status and delivery date, the firm's own record, stay.
    """
    cutoff = (now or timezone.now()) - KEEP_CARRIER_DETAILS
    Shipment.objects.filter(checked_at__lt=cutoff).exclude(status_detail="").update(
        status_detail="", expected_on=None
    )
    return 0


def store_records(sender, connection, capability, records, **kwargs):
    if capability != Capability.SHIPPING:
        return
    for record in records:
        if not isinstance(record, ShipmentRecord):
            continue
        with transaction.atomic():
            if not record_reading(record):
                logger.info("A tracking reading matched no shipment being tracked.")


def connect():
    lookups.provide(Capability.SHIPPING, shipments_to_check)
    records_received.connect(store_records, dispatch_uid="shipments-store-records")
    worker_ticked.connect(forget_carrier_details, dispatch_uid="shipments-forget-details")
