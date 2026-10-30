from django.apps import AppConfig


class AiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm.ai"
    label = "ai"
    verbose_name = "AI features"
