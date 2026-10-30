import uuid

from django.conf import settings
from django.db import models

from crm.fields import EncryptedJSONField, EncryptedTextField

from .registry import registry


class Connection(models.Model):
    """One connected account at a provider, for the whole firm or one staff member."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        NEEDS_ATTENTION = "needs_attention", "Needs attention"
        NEEDS_REAUTH = "needs_reauth", "Needs to reconnect"
        PAUSED = "paused", "Paused"
        DISCONNECTED = "disconnected", "Disconnected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connector_key = models.CharField(max_length=41, db_index=True)
    label = models.CharField(max_length=120)
    # Set for a staff member's personal connection; empty for a firm-wide one.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="connections",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE, db_index=True
    )
    enabled_capabilities = models.JSONField(default=list, blank=True)
    credentials = EncryptedJSONField(null=True, blank=True)
    # Non-secret settings, such as which mailbox or deposit account to use.
    config = models.JSONField(default=dict, blank=True)
    granted_scopes = models.JSONField(default=list, blank=True)
    preview_mode = models.BooleanField(
        default=True,
        help_text="Outbound actions are recorded but not carried out until a superuser turns "
        "this off.",
    )
    outbound_admin_only = models.BooleanField(
        default=False,
        help_text="Only superusers can trigger outbound actions by hand.",
    )
    data_flows_version = models.PositiveIntegerField(null=True, blank=True)
    data_flows_acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    data_flows_acknowledged_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    # The /hooks/ URL token is shown once; only its SHA-256 hash is kept.
    webhook_token_hash = models.CharField(max_length=64, unique=True, null=True, blank=True)
    webhook_secret = EncryptedTextField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["label"]

    def __str__(self):
        return self.label

    @property
    def connector_class(self):
        return registry.get(self.connector_key)

    def connector(self):
        """An instance of this connection's connector, or None if it is not installed."""
        cls = self.connector_class
        return cls(self) if cls is not None else None

    @property
    def data_flows_acknowledged(self):
        cls = self.connector_class
        return (
            cls is not None
            and self.data_flows_version is not None
            and self.data_flows_version >= cls.data_flows.version
        )


class OutboundRequest(models.Model):
    """Every attempt to send something out through a connection, and what the policy decided."""

    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        PREVIEWED = "previewed", "Previewed"
        BLOCKED = "blocked", "Blocked"
        FAILED = "failed", "Failed"

    connection = models.ForeignKey(
        Connection,
        null=True,
        on_delete=models.SET_NULL,
        related_name="outbound_requests",
    )
    connector_key = models.CharField(max_length=41)
    capability = models.CharField(max_length=40)
    action = models.CharField(max_length=80)
    summary = models.CharField(max_length=255, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    is_automation = models.BooleanField(default=False)
    target_type = models.CharField(max_length=80, blank=True)
    target_id = models.CharField(max_length=80, blank=True)
    decision_code = models.CharField(max_length=40)
    status = models.CharField(max_length=20, choices=Status.choices)
    provider_ref = models.CharField(max_length=255, blank=True)
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.connector_key} {self.action} ({self.status})"


class SyncRun(models.Model):
    """One scheduled pull of records for one capability of a connection."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name="sync_runs")
    capability = models.CharField(max_length=40)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    cursor_before = models.JSONField(default=dict, blank=True)
    cursor_after = models.JSONField(default=dict, blank=True)
    records_received = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.connection} {self.capability} sync ({self.status})"


class WebhookEvent(models.Model):
    """A verified provider event, stored minimized and encrypted until the worker processes it."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PROCESSED = "processed", "Processed"
        FAILED = "failed", "Failed"

    connection = models.ForeignKey(
        Connection, on_delete=models.CASCADE, related_name="webhook_events"
    )
    event_id = models.CharField(max_length=255)
    capability = models.CharField(max_length=40)
    payload = EncryptedJSONField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    attempts = models.PositiveSmallIntegerField(default=0)
    records_received = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=500, blank=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]
        constraints = [
            # Providers retry deliveries; each event is accepted once.
            models.UniqueConstraint(
                fields=["connection", "event_id"], name="connect_webhook_event_once"
            ),
        ]

    def __str__(self):
        return f"{self.connection} event {self.event_id} ({self.status})"


class WorkerHeartbeat(models.Model):
    """Last time each worker process checked in, so the CRM can warn when syncs stop."""

    worker_id = models.CharField(max_length=120, unique=True)
    last_seen_at = models.DateTimeField()

    def __str__(self):
        return f"{self.worker_id} at {self.last_seen_at:%Y-%m-%d %H:%M}"


class ConnectorApp(models.Model):
    """The firm's own OAuth app registration for one provider, entered by a superuser."""

    connector_key = models.CharField(max_length=41, unique=True)
    client_id = models.CharField(max_length=255)
    client_secret = EncryptedTextField()
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"OAuth app for {self.connector_key}"
