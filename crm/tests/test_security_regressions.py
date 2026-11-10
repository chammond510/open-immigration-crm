"""Regression tests for confidential data and authentication boundaries."""

import ipaddress
import logging
from datetime import timedelta
from types import SimpleNamespace

from django.contrib.sessions.models import Session
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from gunicorn.config import Config
from gunicorn.glogging import Logger

from config.gunicorn import access_log_format
from crm.logging import PrivateGunicornRequestFilter, PrivateRequestFormatter
from crm.models import FirmProfile, IntakeForm, IntakeInvite, TwoFactorDevice
from crm.services import client_ip
from crm.two_factor import reset_two_factor

from .factories import make_contact, make_user
from .test_two_factor import current_code, enroll, sign_in_verified


class ProxyAddressTests(SimpleTestCase):
    @override_settings(TRUSTED_PROXY_NETWORKS=())
    def test_direct_callers_cannot_claim_a_forwarded_address(self):
        request = RequestFactory().get(
            "/", REMOTE_ADDR="198.51.100.4", HTTP_X_FORWARDED_FOR="203.0.113.8"
        )
        self.assertEqual(client_ip(request), "198.51.100.4")
        request.META["REMOTE_ADDR"] = "invalid"
        self.assertIsNone(client_ip(request))

    @override_settings(
        TRUSTED_PROXY_NETWORKS=(
            ipaddress.ip_network("192.0.2.0/24"),
            ipaddress.ip_network("2001:db8:1::/48"),
        )
    )
    def test_only_trusted_hops_are_skipped(self):
        for peer, chain, expected in (
            ("192.0.2.1", "203.0.113.9, 198.51.100.7, 192.0.2.2", "198.51.100.7"),
            ("2001:db8:1::1", "2001:db8:2::1, 2001:db8:1::2", "2001:db8:2::1"),
            ("198.51.100.7", "203.0.113.9", "198.51.100.7"),
            ("192.0.2.1", "203.0.113.9, malformed", "192.0.2.1"),
            ("192.0.2.1", "192.0.2.2", "192.0.2.1"),
            ("192.0.2.1", "", "192.0.2.1"),
        ):
            with self.subTest(peer=peer, chain=chain):
                request = RequestFactory().get("/", REMOTE_ADDR=peer, HTTP_X_FORWARDED_FOR=chain)
                self.assertEqual(client_ip(request), expected)


class LoginThrottlingTests(TestCase):
    def test_changing_addresses_and_headers_does_not_reset_account_lockout(self):
        user = make_user()
        statuses = [
            self.client.post(
                reverse("login"),
                {"username": user.username, "password": "wrong-password"},
                REMOTE_ADDR=f"198.51.100.{i}",
                HTTP_X_FORWARDED_FOR=f"203.0.113.{i}",
            ).status_code
            for i in range(1, 9)
        ]
        self.assertEqual(statuses, [200] * 4 + [429] * 4)
        # A different account is not locked out by failures against the first one.
        other = make_user("other")
        response = self.client.post(
            reverse("login"), {"username": other.username, "password": "wrong-password"}
        )
        self.assertEqual(response.status_code, 200)


class SessionRevocationTests(TestCase):
    def test_reset_revokes_all_target_sessions_with_or_without_required_mfa(self):
        user = make_user()
        colleague = make_user("colleague")
        unaffected = Client()
        unaffected.force_login(colleague)
        for required in (False, True):
            with self.subTest(required=required):
                FirmProfile.objects.update_or_create(
                    pk=1, defaults={"require_two_factor": required}
                )
                enroll(user)
                first, second = Client(), Client()
                sign_in_verified(first, user)
                sign_in_verified(second, user)
                keys = [first.session.session_key, second.session.session_key]
                reset_two_factor(user)
                self.assertFalse(Session.objects.filter(session_key__in=keys).exists())
                self.assertFalse(TwoFactorDevice.objects.filter(user=user).exists())
                for client in (first, second):
                    response = client.get(reverse("dashboard"))
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response["Location"].startswith(reverse("login")))
                self.assertTrue(
                    Session.objects.filter(session_key=unaffected.session.session_key).exists()
                )

    def test_team_reset_revokes_the_staff_members_session(self):
        manager = make_user()
        staff = make_user("staff", superuser=False)
        enroll(staff)
        other = Client()
        sign_in_verified(other, staff)
        self.client.force_login(manager)
        response = self.client.post(reverse("team_two_factor_reset", args=[staff.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", other.get(reverse("dashboard")).wsgi_request.session)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_self_disable_rotates_current_session_and_revokes_other_sessions(self):
        user = make_user()
        _, codes = enroll(user)
        sign_in_verified(self.client, user)
        old_key = self.client.session.session_key
        other = Client()
        sign_in_verified(other, user)
        response = self.client.post(
            reverse("two_factor_disable"),
            {"disable-password": "Testing-Password-Only-42!", "disable-code": codes[0]},
        )
        self.assertRedirects(response, reverse("dashboard"))
        self.assertNotEqual(old_key, self.client.session.session_key)
        self.assertNotIn("_auth_user_id", other.get(reverse("dashboard")).wsgi_request.session)


class PrivateResponseTests(TestCase):
    def assert_private(self, response):
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["Referrer-Policy"], "strict-origin")

    def test_client_data_setup_and_recovery_codes_cannot_be_cached(self):
        user = make_user()
        contact = make_contact(user)
        self.client.force_login(user)
        self.assert_private(self.client.get(reverse("contact_detail", args=[contact.pk])))
        self.assert_private(self.client.get(reverse("two_factor_settings")))
        device = TwoFactorDevice.objects.get(user=user)
        response = self.client.post(reverse("two_factor_settings"), {"code": current_code(device)})
        self.assertContains(response, "Two-factor sign-in is on")
        self.assert_private(response)

    def test_public_intake_and_authentication_redirects_cannot_be_cached(self):
        user = make_user()
        contact = make_contact(user)
        form = IntakeForm.objects.create(name="Fictional intake")
        _, token = IntakeInvite.issue(
            intake_form=form, contact=contact, expires_at=timezone.now() + timedelta(days=1)
        )
        self.assert_private(self.client.get(reverse("public_intake", args=[token])))
        self.assert_private(self.client.get(reverse("login")))
        self.assert_private(self.client.get(reverse("dashboard")))
        enroll(user)
        self.client.force_login(user)  # No second step: middleware signs this session out.
        self.assert_private(self.client.get(reverse("dashboard")))


class BrowserFormPostTests(TestCase):
    """Browsers derive the Origin of a form post from the page's referrer policy.

    Django's test client sends no Origin header, so these tests send the one a
    browser would. Under "no-referrer" the Fetch standard sends "Origin: null" on
    every form post, which Django's CSRF check rejects (the 403 found on 2026-11-10).
    """

    # Policies under which a browser sends "Origin: null" on a same-origin
    # HTTPS form post (Fetch standard, "serializing a request origin").
    NULL_ORIGIN_POLICIES = {"no-referrer"}

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.user = make_user()

    def csrf_token(self, url, **extra):
        response = self.client.get(url, **extra)
        self.assertNotIn(response["Referrer-Policy"], self.NULL_ORIGIN_POLICIES)
        self.assertEqual(response["Referrer-Policy"], "strict-origin")
        return response.cookies["csrftoken"].value

    def sign_in(self, token, **extra):
        return self.client.post(
            reverse("login"),
            {
                "username": self.user.username,
                "password": "Testing-Password-Only-42!",
                "csrfmiddlewaretoken": token,
            },
            **extra,
        )

    def test_sign_in_with_the_browser_origin_succeeds(self):
        token = self.csrf_token(reverse("login"))
        response = self.sign_in(token, HTTP_ORIGIN="http://testserver")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_sign_in_over_https_with_origin_or_origin_only_referer_succeeds(self):
        token = self.csrf_token(reverse("login"), secure=True)
        response = self.sign_in(token, secure=True, HTTP_ORIGIN="https://testserver")
        self.assertEqual(response.status_code, 302)
        self.client.logout()
        # A browser that omits Origin sends a strict-origin Referer: no path.
        token = self.csrf_token(reverse("login"), secure=True)
        response = self.sign_in(token, secure=True, HTTP_REFERER="https://testserver/")
        self.assertEqual(response.status_code, 302)

    def test_null_origin_is_still_refused(self):
        token = self.csrf_token(reverse("login"))
        response = self.sign_in(token, HTTP_ORIGIN="null")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_public_intake_with_the_browser_origin_succeeds(self):
        form = IntakeForm.objects.create(name="Fictional intake")
        invite, raw_token = IntakeInvite.issue(
            intake_form=form,
            contact=make_contact(self.user),
            expires_at=timezone.now() + timedelta(days=1),
        )
        url = reverse("public_intake", args=[raw_token])
        token = self.csrf_token(url)
        response = self.client.post(
            url,
            {"email": "client@example.test", "csrfmiddlewaretoken": token},
            HTTP_ORIGIN="http://testserver",
        )
        self.assertEqual(response.status_code, 200)
        invite.refresh_from_db()
        self.assertIsNotNone(invite.submitted_at)


class AccessLogTests(SimpleTestCase):
    def test_request_error_logs_do_not_echo_urls_or_exception_details(self):
        secret = "/i/fictional-secret/"
        for name in ("django.request", "django.server", "django.security.csrf"):
            record = logging.LogRecord(
                name,
                logging.ERROR,
                "",
                0,
                "Request failed at %s",
                (secret,),
                (ValueError, ValueError(secret), None),
            )
            record.status_code = 500
            rendered = PrivateRequestFormatter().format(record)
            self.assertNotIn(secret, rendered)
            self.assertIn("status=500 error=ValueError", rendered)
        for message, args in (
            ("Error handling request %s", (secret,)),
            (f"Invalid request from ip=198.51.100.1: {secret}", ()),
        ):
            record = logging.LogRecord(
                "gunicorn.error",
                logging.ERROR,
                "",
                0,
                message,
                args,
                (ValueError, ValueError(secret), None),
            )
            PrivateGunicornRequestFilter().filter(record)
            self.assertNotIn(secret, logging.Formatter().format(record))

    def test_operational_access_log_excludes_bearer_links_and_headers(self):
        cfg = Config()
        cfg.set("access_log_format", access_log_format)
        logger = Logger(cfg)
        for path in (
            "/accounts/welcome/user/secret-token/",
            "/accounts/password-reset/user/secret-token/",
            "/i/secret-token/",
            "/hooks/secret-token/",
            "/contacts/?q=private-client",
        ):
            with self.subTest(path=path):
                environ = {
                    "REQUEST_METHOD": "GET",
                    "RAW_URI": path,
                    "PATH_INFO": path,
                    "QUERY_STRING": "q=private-client",
                    "SERVER_PROTOCOL": "HTTP/1.1",
                    "REMOTE_ADDR": "198.51.100.10",
                    "HTTP_REFERER": "https://crm.example.test/i/secret-token/",
                    "HTTP_USER_AGENT": "private-client",
                    "HTTP_AUTHORIZATION": "Bearer secret-token",
                }
                response = SimpleNamespace(status="200 OK", sent=10, headers=[])
                atoms = logger.atoms(
                    response, SimpleNamespace(headers=[]), environ, timedelta(milliseconds=10)
                )
                line = cfg.access_log_format % atoms
                self.assertNotIn("secret-token", line)
                self.assertNotIn("private-client", line)
                self.assertNotIn(path, line)
                self.assertIn('"GET" 200 10', line)
