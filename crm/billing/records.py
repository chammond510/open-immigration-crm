"""Record the payments a payment connector reports on the invoices they reference.

A payment is recorded once per provider ID, and a refund voids it. When the
provider does not say which account a payment went to, it is recorded to
operating only if the invoice has no advance (trust) lines; otherwise it waits
for staff, since trust money must not be put in the wrong account.
"""

import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from crm.connect.records import PaymentRecord
from crm.connect.signals import records_received
from crm.models import AuditLog

from .models import Invoice, InvoiceLine, Payment
from .services import record_payment, void_payment

logger = logging.getLogger(__name__)


def note(connection, action, detail):
    AuditLog.objects.create(
        action=action,
        target_type="Connection",
        target_id=str(connection.pk),
        detail=detail[:500],
    )


def deposit_account(record, invoice):
    if record.deposit_account:
        return record.deposit_account
    has_advance = invoice.lines.filter(funds=InvoiceLine.Funds.ADVANCE).exists()
    return "" if has_advance else Payment.Account.OPERATING


@transaction.atomic
def store_one(connection, record):
    existing = Payment.objects.filter(
        source_connection=connection, external_id=record.external_id
    ).first()
    if record.status == "refunded":
        if existing and not existing.voided_at:
            void_payment(existing, None, f"Refunded at {connection.label}")
        return
    if record.status != "completed" or existing:
        return
    reference = record.reference.strip()
    invoice = Invoice.objects.filter(number__iexact=reference).first() if reference else None
    if invoice is None:
        note(
            connection, "payment.unmatched", f"A {record.amount} payment named no invoice we have."
        )
        return
    account = deposit_account(record, invoice)
    if not account:
        note(
            connection,
            "payment.needs_review",
            f"A {record.amount} payment on {invoice.number} did not say whether it went to "
            "operating or trust.",
        )
        return
    try:
        record_payment(
            invoice,
            None,
            amount=Decimal(record.amount),
            method=record.method if record.method in Payment.Method.values else "other",
            deposit_account=account,
            received_on=timezone.localdate(record.occurred_at),
            reference=record.external_id[:120],
            note=f"Recorded from {connection.label}"[:255],
            source_connection=connection,
            external_id=record.external_id,
        )
    except ValidationError as exc:
        note(connection, "payment.not_recorded", f"{invoice.number}: {' '.join(exc.messages)}")


def store_records(sender, connection, capability, records, **kwargs):
    for record in records:
        if not isinstance(record, PaymentRecord):
            continue
        try:
            store_one(connection, record)
        except Exception:
            logger.exception("Could not store a payment record from connection %s", connection.pk)


def connect():
    records_received.connect(store_records, dispatch_uid="billing-store-records")
