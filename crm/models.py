import hashlib
import secrets
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone


class FirmProfile(models.Model):
    """Single-firm identity and numbering configuration."""

    name = models.CharField(max_length=200, default="Open Immigration CRM")
    short_name = models.CharField(max_length=80, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    website = models.URLField(blank=True)
    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    region = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=100, blank=True)
    matter_prefix = models.CharField(max_length=12, default="MAT")
    matter_sequence_year = models.PositiveIntegerField(default=0)
    matter_sequence_value = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "firm profile"

    def save(self, *args, **kwargs):
        if self.pk and self.pk != 1:
            raise ValidationError("Open Immigration CRM supports one firm per installation.")
        self.pk = 1
        self.matter_prefix = (self.matter_prefix or "MAT").strip().upper()
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return self.name


class Contact(models.Model):
    class Status(models.TextChoices):
        LEAD = "lead", "Lead"
        PROSPECT = "prospect", "Prospective client"
        CLIENT = "client", "Client"
        FORMER = "former", "Former client"

    class Stage(models.TextChoices):
        INQUIRY = "inquiry", "Inquiry"
        CONSULTATION = "consultation", "Consultation"
        RETAINED = "retained", "Retained"
        ACTIVE = "active", "Active matter"
        CLOSED = "closed", "Closed"
        LOST = "lost", "Not retained"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(blank=True, db_index=True)
    phone = models.CharField(max_length=40, blank=True, db_index=True)
    date_of_birth = models.DateField(null=True, blank=True)
    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    region = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=100, blank=True)
    country_of_birth = models.CharField(max_length=100, blank=True)
    nationality = models.CharField(max_length=100, blank=True)
    preferred_language = models.CharField(max_length=80, blank=True)
    a_number = models.CharField("A-number", max_length=20, blank=True)
    current_immigration_status = models.CharField(max_length=120, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.LEAD,
        db_index=True,
    )
    stage = models.CharField(
        max_length=20,
        choices=Stage.choices,
        default=Stage.INQUIRY,
        db_index=True,
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_contacts",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["last_name", "first_name"]
        indexes = [
            models.Index(fields=["stage", "updated_at"]),
            models.Index(fields=["last_name", "first_name"]),
        ]

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return self.full_name


class Matter(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        DENIED = "denied", "Denied"
        CLOSED = "closed", "Closed"

    class CaseType(models.TextChoices):
        FAMILY = "family", "Family-based"
        EMPLOYMENT = "employment", "Employment-based"
        HUMANITARIAN = "humanitarian", "Humanitarian"
        NATURALIZATION = "naturalization", "Naturalization"
        REMOVAL = "removal", "Removal defense"
        WAIVER = "waiver", "Waiver"
        APPEAL = "appeal", "Appeal or motion"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    matter_number = models.CharField(max_length=30, unique=True, editable=False, db_index=True)
    primary_contact = models.ForeignKey(
        Contact,
        on_delete=models.PROTECT,
        related_name="matters",
    )
    title = models.CharField(max_length=255)
    case_type = models.CharField(
        max_length=30,
        choices=CaseType.choices,
        default=CaseType.OTHER,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_matters",
    )
    receipt_number = models.CharField(max_length=30, blank=True)
    priority_date = models.DateField(null=True, blank=True)
    filing_date = models.DateField(null=True, blank=True)
    description = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["status", "updated_at"]),
            models.Index(fields=["assigned_to", "status"]),
        ]

    @classmethod
    def next_matter_number(cls):
        year = timezone.localdate().year
        with transaction.atomic():
            firm = FirmProfile.objects.select_for_update().filter(pk=1).first()
            if firm is None:
                firm = FirmProfile.objects.create(pk=1)
                firm = FirmProfile.objects.select_for_update().get(pk=1)
            if firm.matter_sequence_year != year:
                firm.matter_sequence_year = year
                firm.matter_sequence_value = 0
            firm.matter_sequence_value += 1
            firm.save(
                update_fields=[
                    "matter_sequence_year",
                    "matter_sequence_value",
                    "updated_at",
                ]
            )
            return f"{firm.matter_prefix}-{year}-{firm.matter_sequence_value:04d}"

    def save(self, *args, **kwargs):
        if not self.matter_number:
            self.matter_number = self.next_matter_number()
        if self.status == self.Status.CLOSED and not self.closed_at:
            self.closed_at = timezone.now()
        elif self.status != self.Status.CLOSED:
            self.closed_at = None
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.matter_number} · {self.title}"


class MatterParty(models.Model):
    matter = models.ForeignKey(Matter, on_delete=models.CASCADE, related_name="parties")
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name="matter_roles")
    role = models.CharField(max_length=120)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["role", "contact__last_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["matter", "contact", "role"],
                name="crm_unique_matter_contact_role",
            )
        ]

    def __str__(self):
        return f"{self.contact} — {self.role}"


class Activity(models.Model):
    class Kind(models.TextChoices):
        NOTE = "note", "Note"
        STATUS = "status", "Status change"
        MEETING = "meeting", "Meeting"
        CALL = "call", "Call"
        DOCUMENT = "document", "Document"
        INTAKE = "intake", "Intake"
        SYSTEM = "system", "System"

    contact = models.ForeignKey(
        Contact,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="activities",
    )
    matter = models.ForeignKey(
        Matter,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="activities",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.NOTE)
    subject = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="crm_activities",
    )

    class Meta:
        ordering = ["-occurred_at", "-pk"]

    def clean(self):
        if not self.contact_id and not self.matter_id:
            raise ValidationError("An activity must be linked to a contact or matter.")

    def __str__(self):
        return self.subject


class WorkItem(models.Model):
    class Kind(models.TextChoices):
        TASK = "task", "Task"
        DEADLINE = "deadline", "Deadline"
        APPOINTMENT = "appointment", "Appointment"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        NORMAL = "normal", "Normal"
        HIGH = "high", "High"
        URGENT = "urgent", "Urgent"

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.TASK)
    priority = models.CharField(
        max_length=20,
        choices=Priority.choices,
        default=Priority.NORMAL,
    )
    contact = models.ForeignKey(
        Contact,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="work_items",
    )
    matter = models.ForeignKey(
        Matter,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="work_items",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_work_items",
    )
    scheduled_for = models.DateTimeField(null=True, blank=True, db_index=True)
    is_completed = models.BooleanField(default=False, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["is_completed", "scheduled_for", "-priority", "title"]
        indexes = [
            models.Index(fields=["is_completed", "scheduled_for"]),
            models.Index(fields=["assigned_to", "is_completed"]),
        ]

    def save(self, *args, **kwargs):
        if self.is_completed and not self.completed_at:
            self.completed_at = timezone.now()
        elif not self.is_completed:
            self.completed_at = None
        super().save(*args, **kwargs)

    @property
    def is_overdue(self):
        return bool(
            not self.is_completed and self.scheduled_for and self.scheduled_for < timezone.now()
        )

    def __str__(self):
        return self.title


class ChecklistItem(models.Model):
    matter = models.ForeignKey(Matter, on_delete=models.CASCADE, related_name="checklist")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    position = models.PositiveIntegerField(default=0)
    is_completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="completed_checklist_items",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "created_at"]

    def __str__(self):
        return self.title


class Document(models.Model):
    class Category(models.TextChoices):
        IDENTITY = "identity", "Identity"
        IMMIGRATION = "immigration", "Immigration filing"
        EVIDENCE = "evidence", "Evidence"
        CORRESPONDENCE = "correspondence", "Correspondence"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    matter = models.ForeignKey(Matter, on_delete=models.CASCADE, related_name="documents")
    contact = models.ForeignKey(
        Contact,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="documents",
    )
    title = models.CharField(max_length=255)
    category = models.CharField(
        max_length=30,
        choices=Category.choices,
        default=Category.OTHER,
    )
    file = models.FileField(upload_to="documents/%Y/%m/")
    original_filename = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=120)
    size_bytes = models.PositiveBigIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_crm_documents",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class IntakeForm(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    instructions = models.TextField(blank=True)
    questions = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class IntakeInvite(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    intake_form = models.ForeignKey(IntakeForm, on_delete=models.PROTECT, related_name="invites")
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="intake_invites")
    matter = models.ForeignKey(
        Matter,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="intake_invites",
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_intake_invites",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @staticmethod
    def hash_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    @classmethod
    def issue(cls, **kwargs):
        raw_token = secrets.token_urlsafe(32)
        invite = cls.objects.create(token_hash=cls.hash_token(raw_token), **kwargs)
        return invite, raw_token

    @property
    def is_active(self):
        return bool(
            not self.revoked_at and not self.submitted_at and timezone.now() < self.expires_at
        )

    def __str__(self):
        return f"{self.intake_form} for {self.contact}"


class IntakeSubmission(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invite = models.OneToOneField(
        IntakeInvite,
        on_delete=models.CASCADE,
        related_name="submission",
    )
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    answers = models.JSONField(default=dict)
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_intake_submissions",
    )

    class Meta:
        ordering = ["-submitted_at"]

    def __str__(self):
        return f"Submission for {self.invite.contact}"


class AuditLog(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="open_immigration_audit_logs",
    )
    action = models.CharField(max_length=120)
    target_type = models.CharField(max_length=80, blank=True)
    target_id = models.CharField(max_length=80, blank=True)
    detail = models.CharField(max_length=500, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} at {self.created_at:%Y-%m-%d %H:%M}"
