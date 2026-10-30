from django.contrib import admin

from .models import Connection, OutboundRequest, SyncRun, WebhookEvent, WorkerHeartbeat


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


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SyncRun)
class SyncRunAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "connection", "capability", "status", "records_received")
    list_filter = ("status", "capability")
    readonly_fields = tuple(field.name for field in SyncRun._meta.fields)


@admin.register(WebhookEvent)
class WebhookEventAdmin(ReadOnlyAdmin):
    """Payloads stay encrypted and are never displayed."""

    list_display = ("received_at", "connection", "capability", "status", "attempts")
    list_filter = ("status", "capability")
    fields = (
        "connection",
        "event_id",
        "capability",
        "status",
        "attempts",
        "records_received",
        "error",
        "received_at",
        "processed_at",
    )
    readonly_fields = fields


@admin.register(WorkerHeartbeat)
class WorkerHeartbeatAdmin(ReadOnlyAdmin):
    list_display = ("worker_id", "last_seen_at")
    readonly_fields = ("worker_id", "last_seen_at")
