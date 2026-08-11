from django.contrib import admin

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
    WorkItem,
)


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
admin.site.register(Document)
admin.site.register(IntakeForm)
admin.site.register(IntakeInvite)
admin.site.register(IntakeSubmission)

admin.site.site_header = "Open Immigration CRM administration"
admin.site.site_title = "Open Immigration CRM"
