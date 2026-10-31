from django.apps import AppConfig


class CrmConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm"

    def ready(self):
        from . import (
            appointments,
            checks,  # noqa: F401
            leads,
            meetings,
        )

        appointments.connect()
        leads.connect()
        meetings.connect()
