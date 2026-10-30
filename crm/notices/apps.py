from django.apps import AppConfig


class NoticesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm.notices"
    label = "notices"
    verbose_name = "USCIS notices"
