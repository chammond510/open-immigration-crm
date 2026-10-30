from django.contrib import admin

from .models import Communication


@admin.register(Communication)
class CommunicationAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "channel", "direction", "contact", "matter", "subject")
    list_filter = ("channel", "direction")
    search_fields = ("subject", "counterpart", "contact__last_name", "matter__matter_number")
    raw_id_fields = ("contact", "matter", "logged_by", "source_connection")
    readonly_fields = ("source_connection", "external_id", "created_at", "updated_at")
