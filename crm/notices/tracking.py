"""Which receipt numbers a case status connector should check, and when.

Receipt numbers on open matters are checked about once a day: the matter's own
receipt number and those on its notices. A receipt number that appears on more
than one matter is skipped, because its readings could not be filed on the right
one. Those never checked come first, then those checked longest ago.
"""

from datetime import timedelta

from django.db.models import Max
from django.utils import timezone

from crm.connect import lookups
from crm.connect.base import Capability
from crm.models import Matter

from .models import (
    RECEIPT_PATTERN,
    CaseNotice,
    CaseStatusCheck,
    CaseStatusProblem,
    normalize_receipt_number,
)

RECHECK_AFTER = timedelta(hours=20)


def receipt_matters():
    """Each valid receipt number on a matter or its notices, with the IDs of its matters."""
    pairs = list(Matter.objects.exclude(receipt_number="").values_list("receipt_number", "pk"))
    pairs += CaseNotice.objects.exclude(receipt_number="").values_list(
        "receipt_number", "matter_id"
    )
    found = {}
    for receipt, matter_id in pairs:
        receipt = normalize_receipt_number(receipt)
        if RECEIPT_PATTERN.match(receipt):
            found.setdefault(receipt, set()).add(matter_id)
    return found


def tracked_receipts():
    """Receipt numbers on exactly one matter, where that matter is not closed."""
    open_ids = set(Matter.objects.exclude(status=Matter.Status.CLOSED).values_list("pk", flat=True))
    return {
        receipt
        for receipt, matter_ids in receipt_matters().items()
        if len(matter_ids) == 1 and matter_ids <= open_ids
    }


def receipts_to_check(*, limit, now=None):
    """Tracked receipt numbers not checked in the last 20 hours, never-checked first."""
    now = now or timezone.now()
    receipts = tracked_receipts()
    last = dict(
        CaseStatusCheck.objects.filter(receipt_number__in=receipts)
        .values("receipt_number")
        .annotate(at=Max("checked_at"))
        .values_list("receipt_number", "at")
    )
    for receipt, at in CaseStatusProblem.objects.filter(receipt_number__in=receipts).values_list(
        "receipt_number", "last_seen_at"
    ):
        last[receipt] = max(at, last.get(receipt, at))
    due = [r for r in receipts if r not in last or last[r] <= now - RECHECK_AFTER]
    due.sort(key=lambda r: (r in last, last.get(r) or now, r))
    return due[:limit]


def connect():
    lookups.provide(Capability.CASE_STATUS, receipts_to_check)
