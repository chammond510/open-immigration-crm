from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction

from crm.fields import EncryptedTextField


class Command(BaseCommand):
    help = (
        "Re-encrypt every encrypted field with the first FIELD_ENCRYPTION_KEYS key. "
        "Run after adding a new key first; remove the old key only afterward."
    )

    def handle(self, *args, **options):
        total = 0
        with transaction.atomic():
            for model in apps.get_models():
                fields = [
                    field.name
                    for field in model._meta.concrete_fields
                    if isinstance(field, EncryptedTextField)
                ]
                if not fields:
                    continue
                for obj in model._default_manager.select_for_update().iterator():
                    obj.save(update_fields=fields)
                    total += 1
        self.stdout.write(self.style.SUCCESS(f"Re-encrypted {total} record(s)."))
