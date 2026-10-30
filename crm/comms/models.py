import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from crm.models import Contact, Matter


class Communication(models.Model):
    """One call, text, email, or meeting, logged by staff or recorded by a connector."""

    class Channel(models.TextChoices):
        CALL = "call", "Call"
        TEXT = "text", "Text message"
        EMAIL = "email", "Email"
        MEETING = "meeting", "Meeting"

    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    channel = models.CharField(max_length=20, choices=Channel.choices, db_index=True)
    direction = models.CharField(max_length=20, choices=Direction.choices, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    duration = models.DurationField(null=True, blank=True)
    # The other party's phone number, email address, or name, as recorded.
    counterpart = models.CharField(max_length=255, blank=True)
    subject = models.CharField(max_length=255, blank=True)
    summary = models.TextField(blank=True)
    contact = models.ForeignKey(
        Contact,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="communications",
    )
    matter = models.ForeignKey(
        Matter,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="communications",
    )
    logged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="logged_communications",
    )
    # Set for records a connector brought in; empty for manual entries.
    source_connection = models.ForeignKey(
        "connect.Connection",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="communications",
    )
    external_id = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-occurred_at", "-created_at"]
        constraints = [
            # A provider record is stored once per connection, however often it syncs.
            models.UniqueConstraint(
                fields=["source_connection", "external_id"],
                condition=~Q(external_id=""),
                name="comms_one_record_per_provider_id",
            ),
        ]

    def clean(self):
        if not self.contact_id and not self.matter_id and not self.source_connection_id:
            raise ValidationError("Link the communication to a contact or matter.")

    @property
    def duration_display(self):
        if not self.duration:
            return ""
        minutes = max(1, round(self.duration.total_seconds() / 60))
        return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes} min"

    def __str__(self):
        return self.subject or f"{self.get_channel_display()} on {self.occurred_at:%Y-%m-%d}"
