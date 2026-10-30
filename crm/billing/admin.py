from django.contrib import admin

from .models import BillingSettings, Invoice, InvoiceLine, Payment, StaffRate


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0
    raw_id_fields = ("time_entry",)


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "contact", "matter", "status", "issue_date", "due_date")
    list_filter = ("status",)
    search_fields = ("number", "contact__last_name", "matter__matter_number")
    raw_id_fields = ("contact", "matter", "created_by")
    inlines = [InvoiceLineInline]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("received_on", "invoice", "amount", "method", "deposit_account", "voided_at")
    list_filter = ("deposit_account", "method")
    raw_id_fields = ("invoice", "recorded_by", "source_connection")

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(BillingSettings)
admin.site.register(StaffRate)
