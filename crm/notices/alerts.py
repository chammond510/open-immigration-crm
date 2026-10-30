"""Which automatic case status changes need a staff review task, and how urgent it is.

The classification reads only USCIS's standard status title, so it stays
predictable. It sets a task's title and priority; it never interprets the change
for the client, and nothing is sent to the client.
"""

import re
from dataclasses import dataclass

from crm.models import WorkItem


@dataclass(frozen=True)
class Review:
    title: str
    priority: str


# Checked in order; the first match wins. A response being received is checked
# before requests, because its title also names the request.
RULES = (
    (
        r"response to uscis|correspondence was received|evidence was received",
        Review("Review response received by USCIS", WorkItem.Priority.NORMAL),
    ),
    (
        r"request for (initial |additional )?evidence|\brfe\b",
        Review("Review USCIS request for evidence", WorkItem.Priority.URGENT),
    ),
    (
        r"intent to (deny|revoke|terminate)|\bnoid\b|\bdenied\b|\brejected\b|\brevoked\b"
        r"|\bterminated\b",
        Review("Review USCIS denial or rejection", WorkItem.Priority.URGENT),
    ),
    (r"interview", Review("Review USCIS interview update", WorkItem.Priority.HIGH)),
    (
        r"biometrics appointment|fingerprint appointment|fingerprints were taken|biometrics reuse",
        Review("Review USCIS biometrics update", WorkItem.Priority.HIGH),
    ),
    (
        r"transferred|new office has jurisdiction",
        Review("Review USCIS transfer", WorkItem.Priority.HIGH),
    ),
    (
        r"approved|approval|welcome notice|card (is being produced|was mailed|was delivered"
        r"|was picked up)|oath ceremony",
        Review("Review USCIS approval", WorkItem.Priority.HIGH),
    ),
)
# Progress that needs no task; the change is still noted on the matter.
ROUTINE = re.compile(
    r"actively being reviewed|case was received|receipt notice was sent|fee was received"
    r"|case is being actively reviewed",
)
OTHER = Review("Review USCIS update", WorkItem.Priority.NORMAL)
PRIORITY_ORDER = [
    WorkItem.Priority.LOW,
    WorkItem.Priority.NORMAL,
    WorkItem.Priority.HIGH,
    WorkItem.Priority.URGENT,
]


def review_for(status_title):
    """The review a change to this status needs, or None for routine progress."""
    title = " ".join(status_title.lower().split())
    for pattern, review in RULES:
        if re.search(pattern, title):
            return review
    if ROUTINE.search(title):
        return None
    return OTHER


def higher_priority(first, second):
    return max(first, second, key=PRIORITY_ORDER.index)
