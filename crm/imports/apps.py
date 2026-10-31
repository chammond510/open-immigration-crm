from django.apps import AppConfig


class ImportsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm.imports"
    label = "imports"
    verbose_name = "Spreadsheet import"

    def ready(self):
        from crm.connect.signals import worker_ticked

        from . import migration
        from .spreadsheet import purge_uploads

        migration.connect()
        worker_ticked.connect(purge_uploads, dispatch_uid="spreadsheet-import-purge")
