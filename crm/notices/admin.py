from django.contrib import admin

from .models import CaseNotice, CaseStatusCheck


@admin.register(CaseNotice)
class CaseNoticeAdmin(admin.ModelAdmin):
    list_display = ("notice_date", "kind", "form_type", "receipt_number", "matter")
    list_filter = ("kind",)
    search_fields = ("receipt_number", "form_type", "matter__matter_number")
    raw_id_fields = ("matter", "contact", "document", "deadline", "appointment", "created_by")


@admin.register(CaseStatusCheck)
class CaseStatusCheckAdmin(admin.ModelAdmin):
    list_display = ("checked_at", "receipt_number", "status_title", "changed", "matter")
    list_filter = ("changed",)
    search_fields = ("receipt_number", "status_title", "matter__matter_number")
    raw_id_fields = ("matter", "recorded_by", "source_connection")
