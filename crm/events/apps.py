from django.apps import AppConfig


class EventsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm.events"
    label = "events"
    verbose_name = "Automation events"

    def ready(self):
        from . import signals

        signals.connect()
