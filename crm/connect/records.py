"""Typed records each capability's connectors produce.

Connectors return these from ``sync()`` and ``handle_webhook()``. Core features
store them: calls, texts, and emails become communications, calendar events and
bookings become appointments, payments are recorded on invoices, case status
readings are recorded on matters, and leads become contacts. ``problems()`` lists
what is wrong with a record; the contract tests require it to be empty.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .base import Capability, Record

INBOUND, OUTBOUND = "inbound", "outbound"
DIRECTIONS = (INBOUND, OUTBOUND)
RECEIPT_PATTERN = re.compile(r"^[A-Z]{3}\d{10}$")
PAYMENT_STATUSES = ("completed", "failed", "refunded", "partially_refunded")
# Why a case status source could not give a status for a receipt number.
NOT_FOUND, INVALID_RECEIPT, UNAVAILABLE = "not_found", "invalid_receipt", "unavailable"
CASE_STATUS_PROBLEMS = (NOT_FOUND, INVALID_RECEIPT, UNAVAILABLE)


def aware(value):
    return value is not None and value.tzinfo is not None


@dataclass(frozen=True, kw_only=True)
class TypedRecord(Record):
    kind: str = ""
    # Email of the staff member whose account, line, or calendar it came from, if known.
    staff_email: str = ""

    def problems(self):
        problems = []
        if not self.external_id:
            problems.append("external_id is required")
        if not aware(self.occurred_at):
            problems.append("occurred_at must be a timezone-aware datetime")
        return problems


@dataclass(frozen=True, kw_only=True)
class CallRecord(TypedRecord):
    kind: str = "call"
    direction: str
    remote: str  # The other party's phone number, as the provider gives it.
    duration_seconds: int | None = None
    outcome: str = ""  # For example completed, missed, or voicemail.
    summary: str = ""  # A recap or notes from the provider, if any.
    recording_url: str = ""

    def problems(self):
        problems = super().problems()
        if self.direction not in DIRECTIONS:
            problems.append("direction must be inbound or outbound")
        if self.duration_seconds is not None and self.duration_seconds < 0:
            problems.append("duration_seconds cannot be negative")
        if self.recording_url and not self.recording_url.startswith("https://"):
            problems.append("recording_url must use HTTPS")
        return problems


@dataclass(frozen=True, kw_only=True)
class TextRecord(TypedRecord):
    kind: str = "text"
    direction: str
    remote: str
    body: str = ""
    status: str = ""  # For example received, sent, delivered, or failed.
    # The provider's own reading of an opt-out reply: "stop", "start", or empty. Core
    # also checks the body for the standard keywords.
    opt_out: str = ""

    def problems(self):
        problems = super().problems()
        if self.direction not in DIRECTIONS:
            problems.append("direction must be inbound or outbound")
        if self.opt_out not in ("", "stop", "start"):
            problems.append("opt_out must be stop, start, or empty")
        return problems


@dataclass(frozen=True, kw_only=True)
class EmailRecord(TypedRecord):
    kind: str = "email"
    direction: str
    remote: str  # The other party's address: the sender of inbound mail, else the first recipient.
    subject: str = ""
    snippet: str = ""  # A short plain-text excerpt; full bodies stay at the provider.
    thread_id: str = ""

    def problems(self):
        problems = super().problems()
        if self.direction not in DIRECTIONS:
            problems.append("direction must be inbound or outbound")
        if "@" not in self.remote:
            problems.append("remote must be an email address")
        return problems


@dataclass(frozen=True, kw_only=True)
class CalendarEventRecord(TypedRecord):
    """``occurred_at`` is the start time."""

    kind: str = "calendar_event"
    title: str
    ends_at: object = None
    location: str = ""
    attendee_emails: tuple = ()
    cancelled: bool = False
    etag: str = ""

    def problems(self):
        problems = super().problems()
        if not self.title:
            problems.append("title is required")
        if self.ends_at is not None and not aware(self.ends_at):
            problems.append("ends_at must be timezone-aware")
        return problems


@dataclass(frozen=True, kw_only=True)
class BookingRecord(TypedRecord):
    """A booked appointment, such as a consultation. ``occurred_at`` is the start time."""

    kind: str = "booking"
    title: str
    invitee_name: str = ""
    invitee_email: str = ""
    invitee_phone: str = ""
    ends_at: object = None
    location: str = ""
    cancelled: bool = False
    answers: dict = field(default_factory=dict)  # Booking questions and their answers.
    tracking: dict = field(default_factory=dict)  # For example utm_source and utm_campaign.
    # The external_id of the booking this one reschedules, whose appointment moves.
    replaces: str = ""

    def problems(self):
        problems = super().problems()
        if not self.title:
            problems.append("title is required")
        if not (self.invitee_email or self.invitee_phone):
            problems.append("invitee_email or invitee_phone is required")
        return problems


@dataclass(frozen=True, kw_only=True)
class PaymentRecord(TypedRecord):
    kind: str = "payment"
    amount: str  # Decimal as text, such as "250.00".
    currency: str = "USD"
    # completed, failed, refunded (in full), or partially_refunded.
    status: str = "completed"
    reference: str = ""  # The invoice number the payer entered or the link carried.
    method: str = "card"  # A Payment.Method value.
    deposit_account: str = ""  # operating or trust, when the provider's account says.
    payer_email: str = ""

    def problems(self):
        problems = super().problems()
        try:
            if Decimal(self.amount) <= 0:
                problems.append("amount must be positive")
        except (InvalidOperation, TypeError):
            problems.append("amount must be a decimal string")
        if self.status not in PAYMENT_STATUSES:
            problems.append(f"status must be one of {', '.join(PAYMENT_STATUSES)}")
        if self.deposit_account not in ("", "operating", "trust"):
            problems.append("deposit_account must be operating or trust")
        return problems


@dataclass(frozen=True, kw_only=True)
class CaseStatusRecord(TypedRecord):
    """``occurred_at`` is when the status was checked.

    When the source could not give a status, ``problem`` says why, ``status_title``
    is empty, and ``status_text`` holds the source's own explanation, if any.
    """

    kind: str = "case_status"
    receipt_number: str
    status_title: str = ""
    status_text: str = ""
    problem: str = ""

    def problems(self):
        problems = super().problems()
        if not RECEIPT_PATTERN.match(self.receipt_number or ""):
            problems.append("receipt_number must be three letters and ten digits")
        if self.problem and self.problem not in CASE_STATUS_PROBLEMS:
            problems.append(f"problem must be one of {', '.join(CASE_STATUS_PROBLEMS)}")
        if bool(self.problem) == bool(self.status_title):
            problems.append("give either a status_title or a problem")
        return problems


# The record types each capability's connectors may produce.
@dataclass(frozen=True, kw_only=True)
class LeadRecord(TypedRecord):
    """A new inquiry, such as a website form submission. ``occurred_at`` is when it was sent."""

    kind: str = "lead"
    name: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    message: str = ""
    form: str = ""  # Which form it came from, such as "Contact page".
    answers: dict = field(default_factory=dict)  # Other questions and their answers.
    tracking: dict = field(default_factory=dict)  # For example source and utm_source.

    def problems(self):
        problems = super().problems()
        if not (self.email or self.phone):
            problems.append("email or phone is required")
        if not (self.name or self.first_name or self.last_name):
            problems.append("a name is required")
        return problems


RECORD_TYPES = {
    Capability.PHONE: (CallRecord,),
    Capability.TEXTING: (TextRecord,),
    Capability.MAILBOX: (EmailRecord,),
    Capability.CALENDAR: (CalendarEventRecord,),
    Capability.SCHEDULING: (BookingRecord,),
    Capability.PAYMENTS: (PaymentRecord,),
    Capability.CASE_STATUS: (CaseStatusRecord,),
    Capability.LEAD_CAPTURE: (LeadRecord,),
}
