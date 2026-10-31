from django.conf import settings
from django.db import models


class Notification(models.Model):
    class Kind(models.TextChoices):
        ASSIGNED = "assigned", "Assigned to you"
        HANDED_OVER = "handed_over", "Handed over to you"

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # What it is about, such as "matter" and the matter's ID, for staff pages only.
    target_type = models.CharField(max_length=20, blank=True)
    target_id = models.CharField(max_length=40, blank=True)
    text = models.CharField(max_length=255)
    url = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["recipient", "read_at"])]

    def __str__(self):
        return self.text


class Preference(models.Model):
    """What a member of staff wants to hear about by email."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preference"
    )
    email_assignments = models.BooleanField(
        "Email me when someone gives me work, a matter, or a contact",
        default=False,
        help_text="The email says what kind of record it is, never the client's details.",
    )

    def __str__(self):
        return f"Notification preferences for {self.user}"

    @classmethod
    def for_user(cls, user):
        return cls.objects.filter(user=user).first() or cls(user=user)
