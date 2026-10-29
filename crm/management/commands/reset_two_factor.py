from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from crm.models import AuditLog
from crm.two_factor import reset_two_factor


class Command(BaseCommand):
    help = "Turn off two-factor sign-in for a user who lost their phone and recovery codes."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **options):
        user = get_user_model().objects.filter(username=options["username"]).first()
        if user is None:
            raise CommandError(f"No user named {options['username']!r}.")
        if not reset_two_factor(user):
            raise CommandError(f"{user.username!r} does not have two-factor sign-in set up.")
        AuditLog.objects.create(
            action="user.two_factor_reset",
            target_type=user.__class__.__name__,
            target_id=str(user.pk),
            detail="Reset from the command line",
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Reset two-factor sign-in for {user.username!r}. They can set it up again "
                "after signing in with their password."
            )
        )
