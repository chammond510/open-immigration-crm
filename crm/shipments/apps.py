from django.apps import AppConfig


class ShipmentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm.shipments"
    label = "shipments"
    verbose_name = "Shipments"

    def ready(self):
        from . import services

        services.connect()
