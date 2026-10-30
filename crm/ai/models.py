import uuid

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


class BackgroundRun(models.Model):
    """One run of a background AI feature, such as preparing memos for upcoming consults."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Finished"
        FAILED = "failed", "Did not finish"

    feature = models.CharField(max_length=60)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # What the run produced: memos prepared or tasks suggested.
    items = models.PositiveIntegerField(default=0)
    # For task suggestions: communications logged up to here have been reviewed.
    cursor = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.feature} ({self.status})"


class ConsultMemo(models.Model):
    """A prep memo for an upcoming consult, drafted by an AI model from CRM records.

    It is a draft for the attorney's own review, never shown to the client.
    """

    class Status(models.TextChoices):
        PREPARING = "preparing", "Preparing"
        READY = "ready", "Ready"
        FAILED = "failed", "Could not be prepared"

    appointment = models.OneToOneField(
        "crm.WorkItem", on_delete=models.CASCADE, related_name="consult_memo"
    )
    contact = models.ForeignKey(
        "crm.Contact", on_delete=models.CASCADE, related_name="consult_memos"
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PREPARING)
    headline = models.CharField(max_length=255, blank=True)
    body = models.TextField(blank=True)
    model = models.CharField(max_length=120, blank=True)
    connection = models.ForeignKey(
        "connect.Connection", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Who asked for this version; empty when it was prepared in the background.
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # A fingerprint of the records it was drafted from, so it is redrafted when they change.
    source_digest = models.CharField(max_length=64, blank=True)
    prepared_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Consult memo for {self.appointment}"


class TaskSuggestion(models.Model):
    """A follow-up task an AI model suggested from recent communications.

    Nothing is created until a staff member accepts it, and they can change it first.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for review"
        ACCEPTED = "accepted", "Accepted"
        DISMISSED = "dismissed", "Dismissed"
        EXPIRED = "expired", "Expired"

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    contact = models.ForeignKey(
        "crm.Contact", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    matter = models.ForeignKey(
        "crm.Matter", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    source_communication = models.ForeignKey(
        "comms.Communication", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    title = models.CharField(max_length=255)
    reason = models.TextField(blank=True)
    priority = models.CharField(max_length=20, default="normal")
    due_date = models.DateField(null=True, blank=True)
    # Who the work would fall to: the matter's or contact's owner, when there is one.
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # A short restatement, used to avoid suggesting the same thing twice.
    intent = models.CharField(max_length=255, blank=True)
    model = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField()
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    work_item = models.ForeignKey(
        "crm.WorkItem", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["due_date", "-created_at"]

    def __str__(self):
        return self.title


class Conversation(models.Model):
    """A staff member's conversation with the assistant, kept here rather than at the provider."""

    class Status(models.TextChoices):
        READY = "ready", "Ready"
        WORKING = "working", "Working"
        WAITING = "waiting", "Waiting for you to confirm"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ai_conversations"
    )
    matter = models.ForeignKey(
        "crm.Matter",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="ai_conversations",
    )
    title = models.CharField(max_length=200)
    connection = models.ForeignKey(
        "connect.Connection", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.READY)
    # Why the last request did not finish, in words safe to show.
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title


class ConversationMessage(models.Model):
    class Role(models.TextChoices):
        USER = "user", "Staff member"
        ASSISTANT = "assistant", "Assistant"
        TOOL = "tool", "Tool"

    class Action(models.TextChoices):
        # A change the assistant asked to make waits for the staff member.
        WAITING = "waiting", "Waiting for you"
        CONFIRMED = "confirmed", "Confirmed"
        DECLINED = "declined", "Declined"

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=20, choices=Role.choices)
    content = models.TextField(blank=True)
    # On an assistant message: the tools it called, as [{"id", "name", "arguments"}].
    tool_calls = models.JSONField(default=list, blank=True)
    # On a tool message: the call it answers, the tool, and its arguments.
    tool_call_id = models.CharField(max_length=120, blank=True)
    tool_name = models.CharField(max_length=60, blank=True)
    arguments = models.JSONField(default=dict, blank=True)
    action = models.CharField(max_length=20, choices=Action.choices, blank=True)
    # On an assistant message: the provider's own copy, sent back only to that provider.
    raw = models.JSONField(null=True, blank=True)
    connector_key = models.CharField(max_length=41, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"{self.role} message"
