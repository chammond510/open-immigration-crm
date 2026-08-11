import io
import json
import tempfile
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import RequestFactory, TestCase, override_settings

from crm.checks import production_configuration_check
from crm.context_processors import firm_profile
from crm.models import Contact
from crm.services import client_ip

from .factories import make_contact, make_user


class ManagementCommandTests(TestCase):
    def test_bootstrap_admin_and_duplicate_guard(self):
        output = io.StringIO()
        call_command(
            "bootstrap_admin",
            username="new-admin",
            email="admin@example.test",
            password="A-Strong-Testing-Password-Only-42!",
            stdout=output,
        )
        self.assertIn("Created administrator", output.getvalue())
        with self.assertRaises(CommandError):
            call_command(
                "bootstrap_admin",
                username="new-admin",
                password="A-Strong-Testing-Password-Only-42!",
            )

    def test_seed_demo_and_refusal_to_mix(self):
        make_user("admin")
        output = io.StringIO()
        call_command("seed_demo", username="admin", stdout=output)
        self.assertIn("fictional demo", output.getvalue())
        self.assertTrue(Contact.objects.filter(email__endswith="@example.test").exists())
        with self.assertRaises(CommandError):
            call_command("seed_demo", username="admin")

    def test_portable_export_does_not_overwrite(self):
        user = make_user()
        make_contact(user)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "portable.json"
            call_command("export_portable_data", str(output))
            payload = json.loads(output.read_text())
            self.assertTrue(any(row["model"] == "crm.contact" for row in payload))
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(CommandError):
                call_command("export_portable_data", str(output))


class SecurityHelperTests(TestCase):
    def test_client_ip_remote_fallback_and_context_processor(self):
        request = RequestFactory().get("/", REMOTE_ADDR="127.0.0.1")
        self.assertEqual(client_ip(request), "127.0.0.1")
        self.assertIn("firm_profile", firm_profile(request))

    @override_settings(
        DEBUG=False,
        SESSION_COOKIE_SECURE=True,
        CSRF_COOKIE_SECURE=True,
        CSRF_TRUSTED_ORIGINS=["https://crm.example.test"],
        DATABASES={"default": {"ENGINE": "django.db.backends.postgresql", "NAME": "test"}},
    )
    def test_production_configuration_check_passes(self):
        self.assertEqual(production_configuration_check(None), [])

    @override_settings(
        DEBUG=True,
        SESSION_COOKIE_SECURE=False,
        CSRF_COOKIE_SECURE=False,
        CSRF_TRUSTED_ORIGINS=[],
    )
    def test_production_configuration_check_flags_unsafe_defaults(self):
        ids = {finding.id for finding in production_configuration_check(None)}
        self.assertIn("openimmigration.E001", ids)
        self.assertIn("openimmigration.E002", ids)
        self.assertIn("openimmigration.W001", ids)
        self.assertIn("openimmigration.W002", ids)
