from django.contrib import admin

from .models import CustomField


@admin.register(CustomField)
class CustomFieldAdmin(admin.ModelAdmin):
    list_display = ("label", "applies_to", "key", "field_type", "required", "is_active")
    list_filter = ("applies_to", "field_type", "is_active")
    readonly_fields = ("key", "created_at", "updated_at")
