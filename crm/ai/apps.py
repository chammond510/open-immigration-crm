from django.apps import AppConfig


class AiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm.ai"
    label = "ai"
    verbose_name = "AI features"

    def ready(self):
        from crm.connect.signals import worker_ticked

        from . import jobs

        worker_ticked.connect(jobs.queue_due, dispatch_uid="ai-background-work")
