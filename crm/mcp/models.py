from django.conf import settings
from django.db import models
from django.utils import timezone


class McpSettings(models.Model):
    """Firm-wide switches for the MCP server. Both are off on a new installation."""

    enabled = models.BooleanField(
        "Turn on the MCP server",
        default=False,
        help_text="While off, assistants cannot sign in or call any tool.",
    )
    write_tools_enabled = models.BooleanField(
        "Allow write tools",
        default=False,
        help_text=(
            "Assistants may add tasks, notes, communications, and status checks, after the "
            "staff member confirms each one. Nothing can be deleted."
        ),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "MCP settings"
        verbose_name_plural = "MCP settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "MCP settings"


class McpToolCall(models.Model):
    """One tool call by an assistant. Arguments and results are not stored."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="mcp_tool_calls",
    )
    application = models.ForeignKey(
        settings.OAUTH2_PROVIDER_APPLICATION_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    tool = models.CharField(max_length=80)
    is_write = models.BooleanField(default=False)
    ok = models.BooleanField(default=True)
    error = models.CharField(max_length=255, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["user", "created_at"])]

    def __str__(self):
        return f"{self.tool} by {self.user}"
