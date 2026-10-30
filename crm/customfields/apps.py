from django.apps import AppConfig


class CustomFieldsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm.customfields"
    label = "customfields"
    verbose_name = "Custom fields"
