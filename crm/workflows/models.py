"""Firm-authored workflow templates and the phases they create on matters.

No templates ship with the project; each firm writes its own.
"""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from crm.models import Matter, WorkItem


class WorkflowTemplate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    case_type = models.CharField(
        max_length=30,
        choices=Matter.CaseType.choices,
        blank=True,
        help_text="Leave blank to offer this workflow for every kind of matter.",
    )
    description = models.TextField(blank=True)
    apply_to_new_matters = models.BooleanField(
        "Apply to new matters of this case type",
        default=False,
        help_text="New matters of the case type start with this workflow.",
    )
    is_active = models.BooleanField(default=True)
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
        ordering = ["name"]

    def clean(self):
        if self.apply_to_new_matters and not self.case_type:
            raise ValidationError(
                {"apply_to_new_matters": "Choose a case type to apply this to new matters."}
            )
        if self.apply_to_new_matters and self.is_active:
            other = (
                WorkflowTemplate.objects.filter(
                    case_type=self.case_type, apply_to_new_matters=True, is_active=True
                )
                .exclude(pk=self.pk)
                .first()
            )
            if other:
                raise ValidationError(
                    {
                        "apply_to_new_matters": f"{other.name} already applies to new "
                        f"{self.get_case_type_display().lower()} matters."
                    }
                )

    def __str__(self):
        return self.name


class PhaseTemplate(models.Model):
    template = models.ForeignKey(WorkflowTemplate, on_delete=models.CASCADE, related_name="phases")
    name = models.CharField(max_length=200)
    position = models.PositiveIntegerField(default=0)
    due_days = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="A deadline this many days after the phase starts."
    )

    class Meta:
        ordering = ["position", "pk"]

    def __str__(self):
        return self.name


class ItemTemplate(models.Model):
    phase = models.ForeignKey(PhaseTemplate, on_delete=models.CASCADE, related_name="items")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "pk"]

    def __str__(self):
        return self.title


class MatterWorkflow(models.Model):
    """A template applied to one matter. Later edits to the template do not change it."""

    matter = models.OneToOneField(Matter, on_delete=models.CASCADE, related_name="workflow")
    template = models.ForeignKey(
        WorkflowTemplate,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="matter_workflows",
    )
    name = models.CharField(max_length=200)
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} on {self.matter.matter_number}"


class MatterPhase(models.Model):
    class Status(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"
        SKIPPED = "skipped", "Skipped"

    workflow = models.ForeignKey(MatterWorkflow, on_delete=models.CASCADE, related_name="phases")
    name = models.CharField(max_length=200)
    position = models.PositiveIntegerField(default=0)
    due_days = models.PositiveSmallIntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NOT_STARTED)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # The deadline in the work queue, created when the phase starts.
    due_item = models.OneToOneField(
        WorkItem, null=True, blank=True, on_delete=models.SET_NULL, related_name="matter_phase"
    )

    class Meta:
        ordering = ["position", "pk"]

    @property
    def is_open(self):
        return self.status in (self.Status.NOT_STARTED, self.Status.IN_PROGRESS)

    def __str__(self):
        return self.name
