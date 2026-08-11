from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from crm.models import ChecklistItem, Contact, FirmProfile, IntakeForm, Matter, WorkItem


class Command(BaseCommand):
    help = "Seed a small, plainly fictional local dataset. Refuses to mix with existing CRM data."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="admin", help="Staff owner for demo records")

    def handle(self, *args, **options):
        if any(model.objects.exists() for model in (Contact, Matter, WorkItem, IntakeForm)):
            raise CommandError("CRM data already exists; refusing to mix demo and real records.")

        User = get_user_model()
        try:
            owner = User.objects.get(username=options["username"], is_staff=True)
        except User.DoesNotExist as exc:
            raise CommandError("Create the named staff user before seeding demo data.") from exc

        firm = FirmProfile.load()
        firm.name = "Northstar Immigration (Demo)"
        firm.short_name = "Northstar"
        firm.matter_prefix = "DEMO"
        firm.save()

        amina = Contact.objects.create(
            first_name="Amina",
            last_name="Example",
            email="amina@example.test",
            phone="+1 555 010 1200",
            country_of_birth="Morocco",
            preferred_language="French",
            status=Contact.Status.CLIENT,
            stage=Contact.Stage.ACTIVE,
            assigned_to=owner,
        )
        lucas = Contact.objects.create(
            first_name="Lucas",
            last_name="Sample",
            email="lucas@example.test",
            phone="+1 555 010 1600",
            country_of_birth="Brazil",
            preferred_language="Portuguese",
            status=Contact.Status.PROSPECT,
            stage=Contact.Stage.CONSULTATION,
            assigned_to=owner,
        )
        matter = Matter.objects.create(
            primary_contact=amina,
            title="Family petition — fictional demonstration",
            case_type=Matter.CaseType.FAMILY,
            assigned_to=owner,
            description="Fictional sample record for evaluating the local application.",
        )
        WorkItem.objects.create(
            title="Review fictional identity documents",
            kind=WorkItem.Kind.TASK,
            priority=WorkItem.Priority.NORMAL,
            contact=amina,
            matter=matter,
            assigned_to=owner,
            scheduled_for=timezone.now() + timedelta(days=2),
        )
        WorkItem.objects.create(
            title="Demo consultation",
            kind=WorkItem.Kind.APPOINTMENT,
            priority=WorkItem.Priority.NORMAL,
            contact=lucas,
            assigned_to=owner,
            scheduled_for=timezone.now() + timedelta(days=1),
        )
        ChecklistItem.objects.bulk_create(
            [
                ChecklistItem(
                    matter=matter, title="Confirm scope and responsible attorney", position=10
                ),
                ChecklistItem(matter=matter, title="Collect required evidence", position=20),
                ChecklistItem(matter=matter, title="Complete attorney review", position=30),
            ]
        )
        IntakeForm.objects.create(
            name="Blank consultation intake",
            instructions="This fictional form demonstrates the blank intake builder.",
            questions=[
                {"key": "q1-goal", "label": "What would you like help with?"},
                {"key": "q2-deadline", "label": "Are you aware of any upcoming deadline?"},
            ],
        )
        self.stdout.write(
            self.style.SUCCESS("Created fictional demo records. Never use them as legal templates.")
        )
