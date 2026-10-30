from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from crm.api.models import ApiRequestLog


class Command(BaseCommand):
    help = "Delete API request log rows older than a number of days (default 365)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=365)

    def handle(self, *args, **options):
        days = options["days"]
        if days < 1:
            raise CommandError("--days must be at least 1.")
        cutoff = timezone.now() - timedelta(days=days)
        deleted, _ = ApiRequestLog.objects.filter(created_at__lt=cutoff).delete()
        self.stdout.write(f"Deleted {deleted} API request log row{'s' if deleted != 1 else ''}.")
