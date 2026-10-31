from django.apps import AppConfig


class SignaturesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm.signatures"
    label = "signatures"
    verbose_name = "E-signature"

    def ready(self):
        from . import records

        records.connect()
