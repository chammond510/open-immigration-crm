from django.contrib import admin

from .models import MatterWorkflow, WorkflowTemplate


@admin.register(WorkflowTemplate)
class WorkflowTemplateAdmin(admin.ModelAdmin):
    """Templates are written on the Workflows page; this view is for review."""

    list_display = ("name", "case_type", "apply_to_new_matters", "is_active", "updated_at")
    list_filter = ("case_type", "is_active")
    search_fields = ("name",)
    readonly_fields = ("created_by", "created_at", "updated_at")


@admin.register(MatterWorkflow)
class MatterWorkflowAdmin(admin.ModelAdmin):
    list_display = ("matter", "name", "started_at", "completed_at")
    search_fields = ("matter__matter_number", "name")
    raw_id_fields = ("matter", "template", "applied_by")
