"""Typed records each capability's connectors produce.

Connectors return these from ``sync()`` and ``handle_webhook()``. Core features
store them: calls, texts, and emails become communications, calendar events and
bookings become appointments, payments are recorded on invoices, case status
readings are recorded on matters, leads become contacts, tracking readings are
recorded on a matter's shipments, and contacts and matters from a previous practice
system become an import staff review and confirm.
``problems()`` lists what is wrong with a record; the contract tests require it to
be empty.
"""

import re
from dataclasses import dataclass, field
from datetime import date
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


SIGNATURE_STATUSES = ("sent", "signed", "completed", "declined", "cancelled")


@dataclass(frozen=True, kw_only=True)
class SignatureRecord(TypedRecord):
    """Where a document sent for signature stands. ``external_id`` is the provider's ID
    for the document, the one ``send_for_signature`` returned; ``occurred_at`` is when
    the provider said so.

    ``signed`` means a signer signed and others have not; ``completed`` means everyone
    signed, and ``signed_pdf`` then holds the signed PDF's bytes.
    """

    kind: str = "signature"
    status: str
    signer_email: str = ""  # Who signed or declined, when the provider says.
    signed_pdf: bytes = b""

    def problems(self):
        problems = super().problems()
        if self.status not in SIGNATURE_STATUSES:
            problems.append(f"status must be one of {', '.join(SIGNATURE_STATUSES)}")
        if self.status == "completed" and not self.signed_pdf.startswith(b"%PDF-"):
            problems.append("a completed signature needs the signed PDF")
        return problems


MATTER_STATUSES = ("open", "pending", "closed")


@dataclass(frozen=True, kw_only=True)
class ContactRecord(TypedRecord):
    """A person from the firm's previous practice system, to import once.

    ``occurred_at`` is when the provider last changed it. A company, which the CRM's
    contacts cannot hold, has ``company`` set and is reported rather than imported.
    """

    kind: str = "contact"
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    email: str = ""
    phone: str = ""
    date_of_birth: object = None  # A date, if known.
    address_line1: str = ""
    address_line2: str = ""
    city: str = ""
    region: str = ""
    postal_code: str = ""
    country: str = ""

    def problems(self):
        problems = super().problems()
        if not (self.first_name or self.last_name or self.company):
            problems.append("a name or company is required")
        if self.date_of_birth is not None and not isinstance(self.date_of_birth, date):
            problems.append("date_of_birth must be a date")
        return problems


@dataclass(frozen=True, kw_only=True)
class MatterRecord(TypedRecord):
    """A matter from the firm's previous practice system, to import once.

    ``client`` is the ``external_id`` of its client's ContactRecord, and ``parties`` the
    other linked contacts as (that contact's ``external_id``, role) pairs. ``occurred_at``
    is when the provider last changed it.
    """

    kind: str = "matter"
    client: str
    title: str
    number: str = ""  # The provider's own matter number, kept in the description.
    case_type: str = ""  # A CRM case type, where the practice area names one.
    practice_area: str = ""
    status: str = "open"
    opened_on: object = None
    description: str = ""
    responsible_email: str = ""  # The responsible staff member, matched by email.
    parties: tuple = ()

    def problems(self):
        problems = super().problems()
        if not self.client:
            problems.append("client is required")
        if not self.title:
            problems.append("title is required")
        if self.status not in MATTER_STATUSES:
            problems.append(f"status must be one of {', '.join(MATTER_STATUSES)}")
        if self.opened_on is not None and not isinstance(self.opened_on, date):
            problems.append("opened_on must be a date")
        if any(len(party) != 2 or not party[0] for party in self.parties):
            problems.append("each party must be a (contact external_id, role) pair")
        return problems


SHIPMENT_STATUSES = ("sent", "in_transit", "out_for_delivery", "delivered", "problem", "returned")
# Why a carrier could not give a tracking number's status. NOT_SHARED: the carrier
# keeps this package's tracking from the firm's account, such as one labelled under
# someone else's mailer account.
NOT_SHARED = "not_shared"
SHIPMENT_PROBLEMS = (NOT_FOUND, UNAVAILABLE, NOT_SHARED)


@dataclass(frozen=True, kw_only=True)
class ShipmentRecord(TypedRecord):
    """Where a tracked package stands. ``occurred_at`` is when it was checked.

    ``carrier`` is the carrier's key, such as ``usps``. ``detail`` is the latest
    tracking event in the carrier's words, and ``delivered_at`` when it was
    delivered. When the carrier could not say, ``problem`` says why and ``status``
    is empty.
    """

    kind: str = "shipment"
    carrier: str
    tracking_number: str
    status: str = ""
    detail: str = ""
    delivered_at: object = None
    expected_on: object = None
    problem: str = ""

    def problems(self):
        problems = super().problems()
        if not (self.carrier and self.tracking_number):
            problems.append("carrier and tracking_number are required")
        if self.problem and self.problem not in SHIPMENT_PROBLEMS:
            problems.append(f"problem must be one of {', '.join(SHIPMENT_PROBLEMS)}")
        if bool(self.problem) == bool(self.status):
            problems.append("give either a status or a problem")
        if self.status and self.status not in SHIPMENT_STATUSES:
            problems.append(f"status must be one of {', '.join(SHIPMENT_STATUSES)}")
        if self.delivered_at is not None and not aware(self.delivered_at):
            problems.append("delivered_at must be a timezone-aware datetime")
        if self.expected_on is not None and not isinstance(self.expected_on, date):
            problems.append("expected_on must be a date")
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
    Capability.ESIGNATURE: (SignatureRecord,),
    Capability.MIGRATION: (ContactRecord, MatterRecord),
    Capability.SHIPPING: (ShipmentRecord,),
}
