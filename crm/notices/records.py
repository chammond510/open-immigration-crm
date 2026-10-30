"""Record case status readings a case status connector reports on their matters."""

import logging

from crm.connect.records import CaseStatusRecord
from crm.connect.signals import records_received
from crm.models import Matter

from .models import CaseNotice, normalize_receipt_number
from .services import record_status

logger = logging.getLogger(__name__)


def matter_for(receipt_number):
    """The one matter with this receipt number on it or on one of its notices."""
    ids = set(
        Matter.objects.filter(receipt_number__iexact=receipt_number).values_list("pk", flat=True)
    )
    ids |= set(
        CaseNotice.objects.filter(receipt_number=receipt_number).values_list("matter_id", flat=True)
    )
    return Matter.objects.get(pk=ids.pop()) if len(ids) == 1 else None


def store_records(sender, connection, capability, records, **kwargs):
    for record in records:
        if not isinstance(record, CaseStatusRecord):
            continue
        try:
            receipt = normalize_receipt_number(record.receipt_number)
            matter = matter_for(receipt)
            if matter is None:
                continue
            record_status(
                matter,
                receipt_number=receipt,
                status_title=record.status_title,
                status_text=record.status_text,
                checked_at=record.occurred_at,
                source_connection=connection,
            )
        except Exception:
            logger.exception(
                "Could not store a case status record from connection %s", connection.pk
            )


def connect():
    records_received.connect(store_records, dispatch_uid="notices-store-records")
