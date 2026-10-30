import re
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from crm.models import Contact, Document, Matter, WorkItem

RECEIPT_PATTERN = re.compile(r"^[A-Z]{3}\d{10}$")


def normalize_receipt_number(value):
    return re.sub(r"[\s-]", "", value or "").upper()


def validate_receipt_number(value):
    if value and not RECEIPT_PATTERN.match(value):
        raise ValidationError(
            "A USCIS receipt number is three letters followed by ten digits, such as IOE0912345678."
        )


class CaseNotice(models.Model):
    """A notice from USCIS about a matter, entered from the paper or PDF notice."""

    class Kind(models.TextChoices):
        RECEIPT = "receipt", "Receipt notice"
        BIOMETRICS = "biometrics", "Biometrics appointment"
        INTERVIEW = "interview", "Interview notice"
        RFE = "rfe", "Request for evidence (RFE)"
        NOID = "noid", "Notice of intent to deny (NOID)"
        APPROVAL = "approval", "Approval notice"
        DENIAL = "denial", "Denial notice"
        REJECTION = "rejection", "Rejection notice"
        TRANSFER = "transfer", "Transfer notice"
        CARD = "card", "Card produced or mailed"
        OTHER = "other", "Other notice"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    matter = models.ForeignKey(Matter, on_delete=models.CASCADE, related_name="notices")
    contact = models.ForeignKey(
        Contact,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="notices",
        help_text="The applicant or beneficiary the notice names, if not the primary contact.",
    )
    kind = models.CharField("Notice type", max_length=20, choices=Kind.choices, db_index=True)
    receipt_number = models.CharField(
        max_length=13, blank=True, db_index=True, validators=[validate_receipt_number]
    )
    form_type = models.CharField("Form", max_length=20, blank=True, help_text="For example, I-130.")
    notice_date = models.DateField(help_text="The date printed on the notice.")
    uscis_received_date = models.DateField(
        "USCIS received date",
        null=True,
        blank=True,
        help_text="On receipt notices, the date USCIS received the filing.",
    )
    priority_date = models.DateField(null=True, blank=True)
    response_due_date = models.DateField(
        null=True, blank=True, help_text="For RFEs and NOIDs: when the response must reach USCIS."
    )
    appointment_at = models.DateTimeField(
        "Appointment", null=True, blank=True, help_text="Biometrics or interview date and time."
    )
    appointment_location = models.CharField(max_length=255, blank=True)
    document = models.ForeignKey(
        Document,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="notices",
        help_text="The scanned notice, uploaded to the matter's documents.",
    )
    notes = models.TextField(blank=True)
    deadline = models.OneToOneField(
        WorkItem, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    appointment = models.OneToOneField(
        WorkItem, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-notice_date", "-created_at"]

    def __str__(self):
        parts = [self.get_kind_display(), self.form_type, self.receipt_number]
        return " · ".join(part for part in parts if part)


class CaseStatusCheck(models.Model):
    """One reading of a receipt number's USCIS case status, by staff or a connector."""

    matter = models.ForeignKey(Matter, on_delete=models.CASCADE, related_name="status_checks")
    receipt_number = models.CharField(
        max_length=13, db_index=True, validators=[validate_receipt_number]
    )
    status_title = models.CharField(max_length=255, help_text="For example, Case Was Approved.")
    status_text = models.TextField(blank=True)
    checked_at = models.DateTimeField(default=timezone.now, db_index=True)
    # True when the status differs from the previous check of the same receipt.
    changed = models.BooleanField(default=False)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    source_connection = models.ForeignKey(
        "connect.Connection",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="status_checks",
    )
    # The task created for staff to review this change, for automatic readings.
    review_task = models.ForeignKey(
        WorkItem, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-checked_at"]

    def __str__(self):
        return f"{self.receipt_number}: {self.status_title}"


class CaseStatusProblem(models.Model):
    """A receipt number a case status source could not answer for on its last check.

    It is removed when a status for the receipt number is recorded.
    """

    class Kind(models.TextChoices):
        NOT_FOUND = "not_found", "USCIS did not recognize this receipt number"
        INVALID_RECEIPT = "invalid_receipt", "USCIS said this receipt number is not valid"
        UNAVAILABLE = "unavailable", "USCIS did not answer"

    matter = models.ForeignKey(Matter, on_delete=models.CASCADE, related_name="status_problems")
    receipt_number = models.CharField(max_length=13, unique=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # The source's own explanation, shown to staff as given.
    detail = models.TextField(blank=True)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField(db_index=True)
    source_connection = models.ForeignKey(
        "connect.Connection", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["receipt_number"]

    def __str__(self):
        return f"{self.receipt_number}: {self.get_kind_display()}"
