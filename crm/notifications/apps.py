from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm.notifications"
    label = "notifications"
    verbose_name = "Notifications"

    def ready(self):
        from . import services

        services.connect()
