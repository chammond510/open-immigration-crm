from django.apps import AppConfig


class CommsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm.comms"
    label = "comms"
    verbose_name = "Communications"

    def ready(self):
        from . import records

        records.connect()
