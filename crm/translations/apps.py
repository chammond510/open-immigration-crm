from django.apps import AppConfig


class TranslationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm.translations"
    label = "translations"
    verbose_name = "Translation"
