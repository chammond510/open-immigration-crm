from django.apps import AppConfig


class WorkflowsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm.workflows"
    label = "workflows"
    verbose_name = "Workflows"

    def ready(self):
        from . import signals

        signals.connect()
