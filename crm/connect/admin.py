from django.contrib import admin

from .models import Connection, OutboundRequest


@admin.register(Connection)
class ConnectionAdmin(admin.ModelAdmin):
    """Read-only view for operators; credentials are never displayed."""

    list_display = ("label", "connector_key", "owner", "status", "preview_mode", "last_success_at")
    list_filter = ("connector_key", "status", "preview_mode")
    search_fields = ("label", "connector_key")
    fields = (
        "label",
        "connector_key",
        "owner",
        "status",
        "enabled_capabilities",
        "config",
        "granted_scopes",
        "preview_mode",
        "outbound_admin_only",
        "data_flows_version",
        "data_flows_acknowledged_by",
        "data_flows_acknowledged_at",
        "last_success_at",
        "last_error_at",
        "last_error",
        "created_by",
        "created_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(OutboundRequest)
class OutboundRequestAdmin(admin.ModelAdmin):
    list_display = ("created_at", "connector_key", "action", "status", "decision_code", "actor")
    list_filter = ("status", "decision_code", "connector_key")
    readonly_fields = tuple(field.name for field in OutboundRequest._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
