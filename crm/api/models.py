import hashlib
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from . import scopes as api_scopes

TOKEN_PREFIX = "oic_"


class ApiSettings(models.Model):
    """Firm-wide API switches. The API is off until a superuser turns it on."""

    enabled = models.BooleanField(
        "Turn on the API",
        default=False,
        help_text="While off, every API request is refused, whatever its token.",
    )
    staff_tokens_allowed = models.BooleanField(
        "Let staff create their own tokens",
        default=False,
        help_text="While off, only superusers' tokens are accepted.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "API settings"
        verbose_name_plural = "API settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "API settings"


class ApiToken(models.Model):
    """A bearer token that acts as one staff member, limited to its scopes.

    Only the SHA-256 hash is stored; the token is shown once, when created.
    """

    LIFETIME_CHOICES = [(30, "30 days"), (90, "90 days"), (180, "180 days"), (365, "1 year")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_tokens"
    )
    name = models.CharField(max_length=100, help_text="What uses this token, such as a Zap.")
    prefix = models.CharField(max_length=12, help_text="The token's first characters.")
    token_hash = models.CharField(max_length=64, unique=True)
    scopes = models.JSONField(default=list)
    expires_at = models.DateTimeField()
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_used_ip = models.GenericIPAddressField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="revoked_api_tokens",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "API token"

    @staticmethod
    def hash_token(raw_token):
        return hashlib.sha256(raw_token.encode()).hexdigest()

    @classmethod
    def issue(cls, *, user, name, scopes, lifetime_days):
        raw_token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        token = cls.objects.create(
            user=user,
            name=name,
            prefix=raw_token[:12],
            token_hash=cls.hash_token(raw_token),
            scopes=api_scopes.normalize(scopes),
            expires_at=timezone.now() + timedelta(days=lifetime_days),
        )
        return token, raw_token

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    @property
    def status(self):
        if self.revoked_at:
            return "revoked"
        return "expired" if self.is_expired else "active"

    @property
    def scope_summary(self):
        return api_scopes.describe(self.scopes)

    def has_scope(self, name):
        return name in self.scopes

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"


class ApiRequestLog(models.Model):
    """One row per API request, including refused ones.

    The query string is not stored, since searches can contain client names.
    """

    token = models.ForeignKey(
        ApiToken, null=True, blank=True, on_delete=models.SET_NULL, related_name="requests"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="api_requests",
    )
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=255)
    status_code = models.PositiveSmallIntegerField()
    duration_ms = models.PositiveIntegerField(default=0)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["token", "created_at"])]

    def __str__(self):
        return f"{self.method} {self.path} {self.status_code}"
