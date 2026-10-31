import uuid

from django.conf import settings
from django.db import models

from crm.fields import EncryptedTextField


class SpreadsheetImport(models.Model):
    """One spreadsheet of contacts and matters, from upload to import.

    The file is kept encrypted for a day after upload, for the preview and the list of
    skipped rows, and then deleted. The report kept afterwards has counts, row
    numbers, and reasons, never the rows' values.
    """

    class Status(models.TextChoices):
        PREPARING = "preparing", "Not imported"
        IMPORTED = "imported", "Imported"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    filename = models.CharField(max_length=255)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PREPARING)
    content = EncryptedTextField(null=True, blank=True)
    headers = models.JSONField(default=list)
    row_count = models.PositiveIntegerField(default=0)
    mapping = models.JSONField(default=dict)
    options = models.JSONField(default=dict)
    imported_at = models.DateTimeField(null=True, blank=True)
    contacts_created = models.PositiveIntegerField(default=0)
    matters_created = models.PositiveIntegerField(default=0)
    skipped = models.JSONField(default=list)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.filename
