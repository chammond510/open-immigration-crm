from django.contrib import admin

from .models import ApiRequestLog, ApiToken


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ApiToken)
class ApiTokenAdmin(ReadOnlyAdmin):
    """Tokens are created and revoked on the API access page; hashes are never shown."""

    list_display = ("name", "prefix", "user", "expires_at", "last_used_at", "revoked_at")
    list_filter = ("revoked_at",)
    search_fields = ("name", "prefix", "user__username")
    fields = (
        "name",
        "prefix",
        "user",
        "scopes",
        "expires_at",
        "last_used_at",
        "last_used_ip",
        "revoked_at",
        "revoked_by",
        "created_at",
    )
    readonly_fields = fields


@admin.register(ApiRequestLog)
class ApiRequestLogAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "method", "path", "status_code", "token", "user")
    list_filter = ("status_code", "method")
    readonly_fields = tuple(field.name for field in ApiRequestLog._meta.fields)

    def has_delete_permission(self, request, obj=None):
        return False
