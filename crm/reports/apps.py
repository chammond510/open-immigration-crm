from django.apps import AppConfig


class ReportsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm.reports"
    label = "reports"
    verbose_name = "Reports"

    def ready(self):
        from . import signals

        signals.connect()
