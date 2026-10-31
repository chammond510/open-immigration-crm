from django.conf import settings
from django.db import models


class Translation(models.Model):
    """A machine translation of a communication or an intake submission.

    It is kept beside the original, never in its place, and always shown as
    machine-made. ``content`` holds the translated values by the original's field,
    or by intake question key.
    """

    communication = models.ForeignKey(
        "comms.Communication",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="translations",
    )
    submission = models.ForeignKey(
        "crm.IntakeSubmission",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="translations",
    )
    language = models.CharField(max_length=12)
    source_language = models.CharField(max_length=12, blank=True)
    content = models.JSONField(default=dict)
    # Who translated it, kept if the connection is later removed.
    translator = models.CharField(max_length=255)
    by_ai = models.BooleanField(default=False)
    connection = models.ForeignKey(
        "connect.Connection", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(communication__isnull=False, submission__isnull=True)
                | models.Q(communication__isnull=True, submission__isnull=False),
                name="translation_of_one_record",
            )
        ]

    def __str__(self):
        return f"Translation into {self.language}"
