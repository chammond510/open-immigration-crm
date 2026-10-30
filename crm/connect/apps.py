from django.apps import AppConfig


class ConnectConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm.connect"
    label = "connect"
    verbose_name = "Connections"

    def ready(self):
        from . import checks  # noqa: F401
        from .registry import registry

        registry.load()
