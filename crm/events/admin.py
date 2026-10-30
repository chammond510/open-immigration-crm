from django.contrib import admin

from .models import EventDelivery


@admin.register(EventDelivery)
class EventDeliveryAdmin(admin.ModelAdmin):
    """Payloads stay encrypted and are never displayed."""

    list_display = ("created_at", "event_type", "connection", "status", "attempts")
    list_filter = ("status", "event_type")
    fields = (
        "connection",
        "event_type",
        "target_type",
        "target_id",
        "status",
        "attempts",
        "next_attempt_at",
        "last_error",
        "created_at",
        "finished_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
