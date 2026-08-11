import json
import os
from datetime import date, datetime
from pathlib import Path
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers import serialize
from django.db.models import Model

from crm.models import (
    Activity,
    AuditLog,
    ChecklistItem,
    Contact,
    Document,
    FirmProfile,
    IntakeForm,
    IntakeInvite,
    IntakeSubmission,
    Matter,
    MatterParty,
    WorkItem,
)

MODELS: tuple[type[Model], ...] = (
    FirmProfile,
    Contact,
    Matter,
    MatterParty,
    Activity,
    WorkItem,
    ChecklistItem,
    Document,
    IntakeForm,
    IntakeInvite,
    IntakeSubmission,
    AuditLog,
)


class Encoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (datetime, date, UUID)):
            return str(obj)
        return super().default(obj)


class Command(BaseCommand):
    help = "Export CRM database records to a portable JSON fixture (document files are not copied)."

    def add_arguments(self, parser):
        parser.add_argument("output", help="New .json output path")

    def handle(self, *args, **options):
        output = Path(options["output"]).expanduser().resolve()
        if output.suffix.lower() != ".json":
            raise CommandError("Output path must end in .json.")
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = []
        for model in MODELS:
            payload.extend(json.loads(serialize("json", model.objects.all())))
        try:
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise CommandError("Output path already exists; refusing to overwrite it.") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, cls=Encoder, indent=2)
            handle.write("\n")
        self.stdout.write(self.style.SUCCESS(f"Exported {len(payload)} records to {output}."))
