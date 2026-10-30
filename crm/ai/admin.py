from django.contrib import admin

from .models import AIRequest


@admin.register(AIRequest)
class AIRequestAdmin(admin.ModelAdmin):
    list_display = ("created_at", "feature", "connector_key", "actor", "model", "status")
    list_filter = ("status", "feature", "connector_key", "is_background")
    raw_id_fields = ("connection", "actor")
