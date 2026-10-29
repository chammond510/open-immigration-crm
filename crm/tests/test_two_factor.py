import base64
import io
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from cryptography.fernet import Fernet
from django.contrib.admin.sites import site as admin_site
from django.core.exceptions import FieldError, ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.fields import EncryptedJSONField
from crm.models import AuditLog, FirmProfile, RecoveryCode, TwoFactorDevice
from crm.two_factor import (
    ACCEPTED,
    ACCEPTED_RECOVERY,
    LOCKED,
    REJECTED,
    SESSION_VERIFIED_KEY,
    check_second_factor,
    current_counter,
    generate_secret,
    hash_recovery_code,
    hotp,
    match_counter,
    provisioning_uri,
    replace_recovery_codes,
)

from .factories import make_user

PASSWORD = "Testing-Password-Only-42!"
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()
BASE_DIR = Path(__file__).resolve().parents[2]


def enroll(user):
    device = TwoFactorDevice.objects.create(
        user=user, secret=generate_secret(), confirmed_at=timezone.now()
    )
    return device, replace_recovery_codes(user)


def current_code(device):
    return hotp(device.secret, current_counter())


def require_two_factor():
    profile = FirmProfile.load()
    profile.require_two_factor = True
    profile.save()


def sign_in_verified(client, user):
    client.force_login(user)
    session = client.session
    session[SESSION_VERIFIED_KEY] = True
    session.save()


class TotpTests(TestCase):
    def test_rfc_6238_reference_values(self):
        self.assertEqual(hotp(RFC_SECRET, 59 // 30), "287082")
        self.assertEqual(hotp(RFC_SECRET, 1111111109 // 30), "081804")
        self.assertEqual(hotp(RFC_SECRET, 1234567890 // 30), "005924")

    def test_drift_window_replay_and_format(self):
        now = 1_800_000_000
        step = current_counter(now)
        self.assertEqual(
            match_counter(RFC_SECRET, hotp(RFC_SECRET, step - 1), after=0, now=now), step - 1
        )
        self.assertEqual(
            match_counter(RFC_SECRET, hotp(RFC_SECRET, step + 1), after=0, now=now), step + 1
        )
        self.assertIsNone(match_counter(RFC_SECRET, hotp(RFC_SECRET, step + 2), after=0, now=now))
        self.assertIsNone(match_counter(RFC_SECRET, hotp(RFC_SECRET, step), after=step, now=now))
        spaced = hotp(RFC_SECRET, step)
        self.assertEqual(
            match_counter(RFC_SECRET, f"{spaced[:3]} {spaced[3:]}", after=0, now=now), step
        )
        self.assertIsNone(match_counter(RFC_SECRET, "２８７０８２", after=0, now=now))
        self.assertIsNone(match_counter(RFC_SECRET, "12345", after=0, now=now))

    def test_provisioning_uri_and_recovery_code_normalization(self):
        uri = provisioning_uri("ABCDEFGH", account="attorney", issuer="Example Firm")
        self.assertTrue(uri.startswith("otpauth://totp/Example%20Firm%3Aattorney?"))
        self.assertIn("secret=ABCDEFGH", uri)
        self.assertIn("issuer=Example+Firm", uri)
        self.assertEqual(hash_recovery_code("abcde-fghjk"), hash_recovery_code("ABCDE FGHJK"))


class EncryptedFieldTests(TestCase):
    def raw_secret(self, device):
        with connection.cursor() as cursor:
            cursor.execute("SELECT secret FROM crm_twofactordevice WHERE id = %s", [device.pk])
            return cursor.fetchone()[0]

    def test_secret_is_ciphertext_at_rest(self):
        device = TwoFactorDevice.objects.create(user=make_user(), secret="PLAINTEXTSECRET")
        self.assertNotIn("PLAINTEXTSECRET", self.raw_secret(device))
        self.assertEqual(TwoFactorDevice.objects.get(pk=device.pk).secret, "PLAINTEXTSECRET")

    def test_encrypted_values_cannot_be_searched(self):
        with self.assertRaises(FieldError):
            list(TwoFactorDevice.objects.filter(secret="PLAINTEXTSECRET"))
        self.assertFalse(TwoFactorDevice.objects.filter(secret__isnull=True).exists())

    def test_key_rotation(self):
        old_key, new_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        with override_settings(FIELD_ENCRYPTION_KEYS=[old_key]):
            device = TwoFactorDevice.objects.create(user=make_user(), secret="ROTATEME")
            old_ciphertext = self.raw_secret(device)
        with override_settings(FIELD_ENCRYPTION_KEYS=[new_key, old_key]):
            self.assertEqual(TwoFactorDevice.objects.get(pk=device.pk).secret, "ROTATEME")
            call_command("rotate_field_encryption", stdout=io.StringIO())
            self.assertNotEqual(self.raw_secret(device), old_ciphertext)
        with override_settings(FIELD_ENCRYPTION_KEYS=[new_key]):
            self.assertEqual(TwoFactorDevice.objects.get(pk=device.pk).secret, "ROTATEME")
        with override_settings(FIELD_ENCRYPTION_KEYS=[old_key]):
            with self.assertRaises(ImproperlyConfigured):
                TwoFactorDevice.objects.get(pk=device.pk)

    def test_encrypted_json_round_trip(self):
        field = EncryptedJSONField()
        stored = field.get_prep_value({"token": "value", "expires": 3600})
        self.assertNotIn("value", stored)
        self.assertEqual(
            field.from_db_value(stored, None, connection), {"token": "value", "expires": 3600}
        )
        self.assertIsNone(field.get_prep_value(None))
        self.assertIsNone(field.from_db_value(None, None, connection))


class EncryptionSettingsTests(TestCase):
    def run_check(self, **env):
        base = {
            "PATH": os.environ.get("PATH", ""),
            "DEBUG": "False",
            "SECRET_KEY": "settings-test-secret-key-0000000000000000000000000000000000",
            "ALLOWED_HOSTS": "crm.example.test",
        }
        return subprocess.run(
            [sys.executable, "manage.py", "check"],
            cwd=BASE_DIR,
            env={**base, **env},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_production_requires_valid_keys(self):
        missing = self.run_check()
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("Production requires FIELD_ENCRYPTION_KEYS", missing.stderr)
        invalid = self.run_check(FIELD_ENCRYPTION_KEYS="not-a-key")
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("32 random bytes", invalid.stderr)
        development = base64.urlsafe_b64encode(b"development-only-field-key-00000").decode()
        self.assertNotEqual(self.run_check(FIELD_ENCRYPTION_KEYS=development).returncode, 0)
        valid = self.run_check(FIELD_ENCRYPTION_KEYS=Fernet.generate_key().decode())
        self.assertEqual(valid.returncode, 0, valid.stderr)


class SecondFactorTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.device, self.codes = enroll(self.user)

    def test_lockout_after_repeated_failures(self):
        for _ in range(4):
            self.assertEqual(check_second_factor(self.user, "000000"), REJECTED)
        self.assertEqual(check_second_factor(self.user, "000000"), LOCKED)
        self.assertEqual(check_second_factor(self.user, current_code(self.device)), LOCKED)
        TwoFactorDevice.objects.filter(pk=self.device.pk).update(
            locked_until=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(check_second_factor(self.user, current_code(self.device)), ACCEPTED)

    def test_recovery_codes_work_once(self):
        self.assertEqual(check_second_factor(self.user, self.codes[0].lower()), ACCEPTED_RECOVERY)
        self.assertEqual(check_second_factor(self.user, self.codes[0]), REJECTED)
        replace_recovery_codes(self.user)
        self.assertEqual(check_second_factor(self.user, self.codes[1]), REJECTED)

    def test_unenrolled_user_is_rejected(self):
        self.assertEqual(check_second_factor(make_user("other"), "123456"), REJECTED)


class LoginFlowTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.device, self.codes = enroll(self.user)

    def password_step(self, next_url=None):
        url = reverse("login") + (f"?next={next_url}" if next_url else "")
        return self.client.post(url, {"username": "attorney", "password": PASSWORD})

    def test_enrolled_user_must_pass_second_step(self):
        response = self.password_step()
        self.assertRedirects(response, reverse("two_factor_verify"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(self.client.get(reverse("two_factor_verify")), "Enter your code")

        response = self.client.post(
            reverse("two_factor_verify"), {"code": current_code(self.device)}
        )
        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))
        self.assertTrue(AuditLog.objects.filter(action="user.two_factor_verified").exists())
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_next_url_survives_second_step(self):
        self.password_step(next_url=reverse("matter_list"))
        response = self.client.post(
            reverse("two_factor_verify"), {"code": current_code(self.device)}
        )
        self.assertRedirects(response, reverse("matter_list"))

    def test_wrong_code_then_lockout(self):
        self.password_step()
        response = self.client.post(reverse("two_factor_verify"), {"code": "000000"})
        self.assertContains(response, "That code was not accepted")
        self.assertTrue(AuditLog.objects.filter(action="user.two_factor_failed").exists())
        for _ in range(3):
            self.client.post(reverse("two_factor_verify"), {"code": "000000"})
        response = self.client.post(reverse("two_factor_verify"), {"code": "000000"}, follow=True)
        self.assertRedirects(response, reverse("login"))
        self.assertContains(response, "Too many incorrect codes")
        self.password_step()
        response = self.client.post(
            reverse("two_factor_verify"), {"code": current_code(self.device)}
        )
        self.assertRedirects(response, reverse("login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_recovery_code_sign_in(self):
        self.password_step()
        response = self.client.post(
            reverse("two_factor_verify"), {"code": self.codes[0]}, follow=True
        )
        self.assertRedirects(response, reverse("dashboard"))
        self.assertContains(response, "9 unused codes remain")
        self.assertTrue(AuditLog.objects.filter(action="user.recovery_code_used").exists())

    def test_second_step_expires(self):
        self.password_step()
        session = self.client.session
        session["two_factor_pending"]["started_at"] -= 301
        session.save()
        response = self.client.get(reverse("two_factor_verify"), follow=True)
        self.assertRedirects(response, reverse("login"))
        self.assertContains(response, "Your sign-in expired")

    def test_verify_without_password_step_returns_to_login(self):
        self.assertRedirects(self.client.get(reverse("two_factor_verify")), reverse("login"))

    def test_unenrolled_user_signs_in_with_password_only(self):
        make_user("paralegal", superuser=False)
        response = self.client.post(
            reverse("login"), {"username": "paralegal", "password": PASSWORD}
        )
        self.assertRedirects(response, reverse("dashboard"))


class EnforcementTests(TestCase):
    def test_session_that_skipped_second_step_is_signed_out(self):
        user = make_user()
        enroll(user)
        self.client.force_login(user)
        response = self.client.get(reverse("matter_list"))
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('matter_list')}")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_admin_login_form_redirects_to_staff_login(self):
        make_user()
        response = self.client.get("/admin/login/?next=/admin/")
        self.assertRedirects(response, f"{reverse('login')}?next=/admin/")

    def test_firm_requirement_sends_unenrolled_staff_to_setup(self):
        user = make_user()
        require_two_factor()
        self.client.force_login(user)
        self.assertRedirects(
            self.client.get(reverse("dashboard")),
            reverse("two_factor_settings"),
            fetch_redirect_response=False,
        )
        response = self.client.get(reverse("two_factor_settings"))
        self.assertContains(response, "Your firm requires two-factor sign-in")
        self.assertNotContains(response, "Not now")
        self.assertRedirects(self.client.post(reverse("logout")), reverse("login"))

    def test_firm_requirement_is_a_firm_setting(self):
        user = make_user()
        sign_in_verified(self.client, user)
        enroll(user)
        self.client.post(
            reverse("firm_settings"),
            {"name": "Example Firm", "matter_prefix": "MAT", "require_two_factor": "on"},
        )
        self.assertTrue(FirmProfile.load().require_two_factor)


class EnrollmentTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_enrollment_confirms_with_a_code_and_shows_recovery_codes(self):
        response = self.client.get(reverse("two_factor_settings"))
        self.assertContains(response, "data:image/svg+xml")
        device = TwoFactorDevice.objects.get(user=self.user)
        self.assertContains(response, device.secret[:4])
        self.assertFalse(device.is_confirmed)

        response = self.client.post(reverse("two_factor_settings"), {"code": "000000"})
        self.assertContains(response, "That code did not match")

        response = self.client.post(reverse("two_factor_settings"), {"code": current_code(device)})
        self.assertContains(response, "Two-factor sign-in is on")
        self.assertEqual(len(response.context["codes"]), 10)
        self.assertEqual(RecoveryCode.objects.filter(user=self.user).count(), 10)
        device.refresh_from_db()
        self.assertTrue(device.is_confirmed)
        self.assertTrue(AuditLog.objects.filter(action="user.two_factor_enabled").exists())
        # Enrolling marks this session verified, so it is not signed out.
        self.assertContains(
            self.client.get(reverse("two_factor_settings")), "unused recovery codes"
        )

    def test_regenerate_recovery_codes(self):
        _, old_codes = enroll(self.user)
        sign_in_verified(self.client, self.user)
        response = self.client.post(
            reverse("two_factor_recovery_codes"), {"regenerate-code": "000000"}
        )
        self.assertRedirects(response, reverse("two_factor_settings"))
        response = self.client.post(
            reverse("two_factor_recovery_codes"), {"regenerate-code": old_codes[0]}
        )
        self.assertContains(response, "New recovery codes")
        self.assertFalse(
            RecoveryCode.objects.filter(code_hash=hash_recovery_code(old_codes[1])).exists()
        )

    def test_disable_requires_password_and_code(self):
        device, _ = enroll(self.user)
        sign_in_verified(self.client, self.user)
        self.client.post(
            reverse("two_factor_disable"),
            {"disable-password": "wrong-password", "disable-code": current_code(device)},
        )
        self.assertTrue(TwoFactorDevice.objects.filter(user=self.user).exists())
        TwoFactorDevice.objects.filter(pk=device.pk).update(last_used_counter=0)
        response = self.client.post(
            reverse("two_factor_disable"),
            {"disable-password": PASSWORD, "disable-code": current_code(device)},
        )
        self.assertRedirects(response, reverse("dashboard"))
        self.assertFalse(TwoFactorDevice.objects.filter(user=self.user).exists())
        self.assertFalse(RecoveryCode.objects.filter(user=self.user).exists())

    def test_disable_blocked_when_firm_requires_two_factor(self):
        device, _ = enroll(self.user)
        require_two_factor()
        sign_in_verified(self.client, self.user)
        response = self.client.post(
            reverse("two_factor_disable"),
            {"disable-password": PASSWORD, "disable-code": current_code(device)},
        )
        self.assertRedirects(response, reverse("two_factor_settings"))
        self.assertTrue(TwoFactorDevice.objects.filter(user=self.user).exists())


class ResetTests(TestCase):
    def test_reset_command(self):
        user = make_user()
        enroll(user)
        output = io.StringIO()
        call_command("reset_two_factor", "attorney", stdout=output)
        self.assertIn("Reset two-factor sign-in", output.getvalue())
        self.assertFalse(TwoFactorDevice.objects.filter(user=user).exists())
        self.assertTrue(AuditLog.objects.filter(action="user.two_factor_reset").exists())
        with self.assertRaises(CommandError):
            call_command("reset_two_factor", "attorney")
        with self.assertRaises(CommandError):
            call_command("reset_two_factor", "nobody")

    def test_admin_delete_resets_device_and_codes(self):
        admin_user = make_user("admin")
        user = make_user("paralegal", superuser=False)
        device, _ = enroll(user)
        request = RequestFactory().post("/admin/")
        request.user = admin_user
        model_admin = admin_site._registry[TwoFactorDevice]
        model_admin.delete_queryset(request, TwoFactorDevice.objects.filter(pk=device.pk))
        self.assertFalse(TwoFactorDevice.objects.filter(user=user).exists())
        self.assertFalse(RecoveryCode.objects.filter(user=user).exists())
