from django import forms
from django.contrib import admin
from django.core.files.uploadedfile import UploadedFile

from .models import (
    Activity,
    AuditLog,
    ChecklistItem,
    Contact,
    Document,
    FirmProfile,
    IntakeForm,
    IntakeInvite,
    IntakeSubmission,
    Matter,
    MatterParty,
    TimeEntry,
    WorkItem,
)
from .validators import validate_document


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "status", "stage", "assigned_to")
    list_filter = ("status", "stage")
    search_fields = ("first_name", "last_name", "email", "phone", "a_number")


@admin.register(Matter)
class MatterAdmin(admin.ModelAdmin):
    list_display = ("matter_number", "title", "primary_contact", "status", "assigned_to")
    list_filter = ("status", "case_type")
    search_fields = ("matter_number", "title", "receipt_number", "primary_contact__last_name")


@admin.register(WorkItem)
class WorkItemAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "scheduled_for", "assigned_to", "is_completed")
    list_filter = ("kind", "priority", "is_completed")
    search_fields = ("title", "description")


@admin.register(TimeEntry)
class TimeEntryAdmin(admin.ModelAdmin):
    list_display = ("user", "matter", "contact", "started_at", "stopped_at", "note")
    list_filter = ("user",)
    search_fields = ("note", "matter__matter_number", "contact__last_name")


class DocumentAdminForm(forms.ModelForm):
    """Admin uploads pass the same content checks as the staff upload view."""

    class Meta:
        model = Document
        fields = ["matter", "contact", "title", "category", "file"]

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        if isinstance(uploaded, UploadedFile):
            self.detected_mime = validate_document(uploaded)
        return uploaded


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    form = DocumentAdminForm
    list_display = ("title", "matter", "category", "created_at", "uploaded_by")
    list_filter = ("category",)
    search_fields = ("title", "original_filename", "matter__matter_number")
    readonly_fields = ("original_filename", "mime_type", "size_bytes", "uploaded_by", "created_at")

    def save_model(self, request, obj, form, change):
        uploaded = form.cleaned_data.get("file")
        if isinstance(uploaded, UploadedFile):
            obj.original_filename = uploaded.name[:255]
            obj.mime_type = getattr(form, "detected_mime", obj.mime_type)
            obj.size_bytes = uploaded.size
            obj.uploaded_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "action", "target_type", "target_id")
    list_filter = ("action", "target_type")
    search_fields = ("action", "target_id", "detail")
    readonly_fields = tuple(field.name for field in AuditLog._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(FirmProfile)
admin.site.register(MatterParty)
admin.site.register(Activity)
admin.site.register(ChecklistItem)
admin.site.register(IntakeForm)
admin.site.register(IntakeInvite)
admin.site.register(IntakeSubmission)

admin.site.site_header = "Open Immigration CRM administration"
admin.site.site_title = "Open Immigration CRM"
