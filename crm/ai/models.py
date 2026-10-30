from django.conf import settings
from django.db import models


class AISettings(models.Model):
    """Firm-wide AI switches. Everything is off on a new installation."""

    enabled = models.BooleanField(
        "Turn on AI features",
        default=False,
        help_text="While off, no AI feature runs and nothing is sent to an AI model.",
    )
    # Feature key -> True when a superuser turned that feature on.
    features = models.JSONField(default=dict, blank=True)
    background_connection = models.ForeignKey(
        "connect.Connection",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Work no one is waiting on, such as preparing a memo ahead of a consult, "
        "runs only through this firm-wide connection.",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "AI settings"
        verbose_name_plural = "AI settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def feature_on(self, key):
        return self.enabled and bool((self.features or {}).get(key))

    def __str__(self):
        return "AI settings"


class AIRequest(models.Model):
    """One request to an AI model: the feature, who asked, and the connection that paid.

    Prompts and answers are not stored here.
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    feature = models.CharField(max_length=60)
    connection = models.ForeignKey(
        "connect.Connection", null=True, on_delete=models.SET_NULL, related_name="ai_requests"
    )
    connector_key = models.CharField(max_length=41, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    is_background = models.BooleanField(default=False)
    model = models.CharField(max_length=120, blank=True)
    # The outbound policy's outcome: sent, previewed, blocked, or failed.
    status = models.CharField(max_length=20)
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.feature} via {self.connector_key} ({self.status})"
