from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm.api"
    label = "api"
    verbose_name = "REST API"
