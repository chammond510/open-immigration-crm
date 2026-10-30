import uuid

from django.conf import settings
from django.db import models

from crm.fields import EncryptedJSONField

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
