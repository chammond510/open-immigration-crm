import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create the first staff administrator from flags or environment variables."

    def add_arguments(self, parser):
        parser.add_argument("--username", default=os.environ.get("ADMIN_USERNAME", "admin"))
        parser.add_argument("--email", default=os.environ.get("ADMIN_EMAIL", ""))
        parser.add_argument("--password", default=os.environ.get("ADMIN_PASSWORD", ""))

    def handle(self, *args, **options):
        username = options["username"].strip()
        email = options["email"].strip()
        password = options["password"]
        if not password:
            raise CommandError("Pass --password or set ADMIN_PASSWORD.")

        User = get_user_model()
        if User.objects.filter(username=username).exists():
            raise CommandError(f"User {username!r} already exists; no changes made.")
        candidate = User(username=username, email=email)
        try:
            validate_password(password, candidate)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc

        User.objects.create_superuser(username=username, email=email, password=password)
        self.stdout.write(self.style.SUCCESS(f"Created administrator {username!r}."))
