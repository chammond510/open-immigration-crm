import uuid

from django.db import models

from crm.connect.models import Connection
from crm.fields import EncryptedJSONField


class EventDelivery(models.Model):
    """One event queued for one automation connection, retried until it settles.

    The ID doubles as the delivery's ``webhook-id``, so a receiver can drop
    repeats. The payload is stored encrypted and deleted once the delivery
    settles; each attempt is also an ``OutboundRequest``.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RETRYING = "retrying", "Retrying"
        SENT = "sent", "Sent"
        PREVIEWED = "previewed", "Previewed"
        BLOCKED = "blocked", "Blocked"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(
        Connection, on_delete=models.CASCADE, related_name="event_deliveries"
    )
    event_type = models.CharField(max_length=60)
    target_type = models.CharField(max_length=80, blank=True)
    target_id = models.CharField(max_length=80, blank=True)
    payload = EncryptedJSONField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "event deliveries"

    @property
    def is_settled(self):
        return self.status not in (self.Status.QUEUED, self.Status.RETRYING)

    def __str__(self):
        return f"{self.event_type} to {self.connection}"
