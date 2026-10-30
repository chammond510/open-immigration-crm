from django.db import models
from django.utils import timezone

from crm.models import Contact


class StageChange(models.Model):
    """When a contact entered a pipeline stage, so reports can count consults and
    retentions by date. Recorded on every save that changes the stage."""

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="stage_changes")
    from_stage = models.CharField(max_length=20, blank=True)
    to_stage = models.CharField(max_length=20, choices=Contact.Stage.choices)
    changed_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["changed_at", "pk"]
        indexes = [models.Index(fields=["to_stage", "changed_at"])]

    def __str__(self):
        return f"{self.contact} entered {self.get_to_stage_display()}"
