import uuid

from django.conf import settings
from django.db import models

from crm.models import Document, Matter


class SignatureRequest(models.Model):
    """A matter document sent for signature through an e-signature connection.

    ``signers`` is a list of ``{"name", "email", "contact", "status"}``, where status is
    "pending", "signed", or "declined". When everyone has signed, the signed PDF is
    filed on the matter as ``signed_document``.
    """

    class Status(models.TextChoices):
        SENT = "sent", "Out for signature"
        COMPLETED = "completed", "Signed"
        DECLINED = "declined", "Declined"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    matter = models.ForeignKey(Matter, on_delete=models.CASCADE, related_name="signature_requests")
    document = models.ForeignKey(
        Document, null=True, on_delete=models.SET_NULL, related_name="signature_requests"
    )
    title = models.CharField(max_length=255)
    signers = models.JSONField(default=list)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.SENT, db_index=True
    )
    connection = models.ForeignKey(
        "connect.Connection", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    external_id = models.CharField(max_length=255)
    signed_document = models.ForeignKey(
        Document, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "external_id"], name="signatures_one_request_per_document"
            )
        ]

    def __str__(self):
        return self.title

    @property
    def is_open(self):
        return self.status == self.Status.SENT
