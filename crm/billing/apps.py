from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm.billing"
    label = "billing"
    verbose_name = "Billing"

    def ready(self):
        from crm.connect.signals import worker_ticked

        from . import records, reminders

        records.connect()
        worker_ticked.connect(reminders.queue_due, dispatch_uid="billing-email-reminders")
