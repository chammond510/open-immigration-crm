from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Permission
from django.test import override_settings
from django.utils import timezone

from crm.api import scopes
from crm.api.models import TOKEN_PREFIX, ApiRequestLog, ApiSettings, ApiToken
from crm.tests.factories import make_user
from crm.tests.test_two_factor import enroll, require_two_factor

from .helpers import ApiTestCase


def error_code(response):
    return response.json()["error"]["code"]


class ScopeTests(ApiTestCase):
    def test_normalize_and_describe(self):
        self.assertEqual(
            scopes.normalize(["matters:write", "contacts:read", "billing:write", "nope"]),
            ["contacts:read", "matters:read", "matters:write"],
        )
        self.assertEqual(
            scopes.describe(["contacts:read", "matters:read", "matters:write"]),
            "Contacts (read), Matters and their parties (read and write)",
        )
        self.assertNotIn("billing:write", scopes.ALL_SCOPES)

    def test_issued_tokens_store_only_a_hash(self):
        token, raw_token = self.issue("notes:write")
        self.assertTrue(raw_token.startswith(TOKEN_PREFIX))
        self.assertEqual(token.prefix, raw_token[:12])
        self.assertEqual(token.token_hash, ApiToken.hash_token(raw_token))
        self.assertNotIn(raw_token, str(ApiToken.objects.values().get()))
        self.assertEqual(token.scopes, ["notes:read", "notes:write"])
        self.assertEqual(token.status, "active")
        self.assertEqual(str(token), f"Fictional automation ({token.prefix}…)")


class AuthenticationTests(ApiTestCase):
    def test_a_valid_token_signs_in_and_is_logged(self):
        token, raw_token = self.issue("contacts:read")
        response = self.api("get", "/api/v1/?q=Fictional", token=raw_token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        body = response.json()
        self.assertEqual(body["user"]["username"], self.user.username)
        self.assertEqual(body["token"]["scopes"], ["contacts:read"])
        self.assertIn("contacts", body["endpoints"])
        self.assertIn("rfe", body["choices"]["notice.kind"])
        entry = ApiRequestLog.objects.get()
        self.assertEqual((entry.token, entry.user), (token, self.user))
        self.assertEqual((entry.method, entry.path, entry.status_code), ("GET", "/api/v1/", 200))
        token.refresh_from_db()
        self.assertIsNotNone(token.last_used_at)

    def test_missing_unknown_revoked_and_expired_tokens_are_refused(self):
        self.assertEqual(error_code(self.api("get", "/api/v1/", token="")), "unauthorized")
        response = self.client.get("/api/v1/", headers={"Authorization": "Basic abc"})
        self.assertEqual(response.status_code, 401)
        response = self.api("get", "/api/v1/", token=f"{TOKEN_PREFIX}not-a-real-token")
        self.assertEqual((response.status_code, error_code(response)), (401, "invalid_token"))

        token, raw_token = self.issue()
        ApiToken.objects.filter(pk=token.pk).update(revoked_at=timezone.now())
        self.assertEqual(error_code(self.api("get", "/api/v1/", token=raw_token)), "token_revoked")
        token, raw_token = self.issue()
        ApiToken.objects.filter(pk=token.pk).update(expires_at=timezone.now() - timedelta(1))
        self.assertEqual(error_code(self.api("get", "/api/v1/", token=raw_token)), "token_expired")
        self.assertEqual(ApiRequestLog.objects.count(), 0)

    def test_a_session_cookie_is_not_enough(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/api/v1/").status_code, 401)

    def test_api_off_staff_tokens_and_inactive_users(self):
        staff = make_user("paralegal", superuser=False)
        _, staff_token = self.issue(user=staff)
        response = self.api("get", "/api/v1/", token=staff_token)
        self.assertEqual((response.status_code, error_code(response)), (403, "staff_tokens_off"))
        # Whoever manages connections and AI may use a token while staff tokens are off.
        _, partner_token = self.issue(user=make_user("partner", role="Managing partner"))
        self.assertEqual(self.api("get", "/api/v1/", token=partner_token).status_code, 200)
        ApiSettings.objects.filter(pk=1).update(staff_tokens_allowed=True)
        self.assertEqual(self.api("get", "/api/v1/", token=staff_token).status_code, 200)

        staff.is_active = False
        staff.save()
        self.assertEqual(
            error_code(self.api("get", "/api/v1/", token=staff_token)), "user_inactive"
        )

        ApiSettings.objects.filter(pk=1).update(enabled=False)
        response = self.api("get", "/api/v1/")
        self.assertEqual((response.status_code, error_code(response)), (403, "api_disabled"))

    def test_firm_required_two_factor_applies_to_tokens(self):
        require_two_factor()
        response = self.api("get", "/api/v1/")
        self.assertEqual(error_code(response), "two_factor_required")
        enroll(self.user)
        self.assertEqual(self.api("get", "/api/v1/").status_code, 200)

    def test_scopes_limit_reads_and_writes(self):
        _, raw_token = self.issue("contacts:read")
        self.assertEqual(self.api("get", "/api/v1/contacts/", token=raw_token).status_code, 200)
        response = self.api("post", "/api/v1/contacts/", {"first_name": "A"}, token=raw_token)
        self.assertEqual((response.status_code, error_code(response)), (403, "missing_scope"))
        self.assertIn("contacts:write", response.json()["error"]["message"])
        self.assertEqual(self.api("get", "/api/v1/matters/", token=raw_token).status_code, 403)
        # Endpoints that need no scope still need a token.
        self.assertEqual(self.api("get", "/api/v1/users/", token=raw_token).status_code, 200)

    def test_billing_needs_the_billing_permission(self):
        staff = make_user("paralegal", superuser=False)
        ApiSettings.objects.filter(pk=1).update(staff_tokens_allowed=True)
        _, raw_token = self.issue("billing:read", user=staff)
        response = self.api("get", "/api/v1/invoices/", token=raw_token)
        self.assertEqual(error_code(response), "no_billing_access")
        staff.user_permissions.add(Permission.objects.get(codename="access_billing"))
        self.assertEqual(self.api("get", "/api/v1/invoices/", token=raw_token).status_code, 200)

    @override_settings(API_HOURLY_LIMIT=2)
    def test_hourly_limit(self):
        self.api("get", "/api/v1/")
        self.api("get", "/api/v1/")
        response = self.api("get", "/api/v1/")
        self.assertEqual((response.status_code, error_code(response)), (429, "rate_limited"))
        self.assertEqual(response["Retry-After"], "60")
        for _ in range(5):
            self.assertEqual(self.api("get", "/api/v1/").status_code, 429)
        self.assertEqual(ApiRequestLog.objects.count(), 2)

    @override_settings(API_REQUESTS_PER_MINUTE=3)
    def test_anonymous_requests_are_bounded_before_database_authentication(self):
        for i in range(3):
            self.assertEqual(
                self.client.get("/api/v1/", HTTP_X_FORWARDED_FOR=f"203.0.113.{i}").status_code,
                401,
            )
        with mock.patch("crm.api.http.authenticate") as authenticate:
            response = self.client.get("/api/v1/")
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response["Retry-After"], "60")
            authenticate.assert_not_called()
        self.assertFalse(ApiRequestLog.objects.exists())

    @override_settings(API_REQUESTS_PER_MINUTE=2)
    def test_one_anonymous_client_cannot_use_another_clients_budget(self):
        for _ in range(2):
            self.assertEqual(
                self.client.get("/api/v1/", REMOTE_ADDR="198.51.100.1").status_code, 401
            )
        self.assertEqual(
            self.client.get("/api/v1/", REMOTE_ADDR="198.51.100.1").status_code, 429
        )
        self.assertEqual(
            self.api("get", "/api/v1/", REMOTE_ADDR="198.51.100.2").status_code, 200
        )
        self.assertEqual(ApiRequestLog.objects.count(), 1)

    def test_disabled_api_and_invalid_credentials_do_not_create_log_rows(self):
        ApiSettings.objects.all().delete()
        for _ in range(12):
            self.assertEqual(self.client.get("/api/v1/").status_code, 403)
        self.assertFalse(ApiSettings.objects.exists())
        self.assertFalse(ApiRequestLog.objects.exists())

    @override_settings(API_HOURLY_LIMIT=2)
    def test_scope_refusals_cannot_bypass_the_hourly_log_limit(self):
        _, raw_token = self.issue("contacts:read")
        statuses = [
            self.api("get", "/api/v1/matters/", token=raw_token).status_code
            for _ in range(5)
        ]
        self.assertEqual(statuses, [403, 403, 429, 429, 429])
        self.assertEqual(ApiRequestLog.objects.count(), 2)

    def test_methods_paths_and_bodies(self):
        response = self.api("delete", "/api/v1/contacts/")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "GET, POST")
        response = self.client.get("/api/v1/nothing-here/")
        self.assertEqual((response.status_code, error_code(response)), (404, "not_found"))
        self.assertIn(
            "end with a slash", self.client.get("/api/v1/contacts").json()["error"]["message"]
        )
        self.assertEqual(self.api("get", "/api/v1/contacts/not-a-uuid/").status_code, 404)

        response = self.api("post", "/api/v1/contacts/", "first_name=A", content_type="text/plain")
        self.assertEqual(response.status_code, 415)
        response = self.client.post(
            "/api/v1/contacts/",
            "{not json",
            content_type="application/json",
            headers={"Authorization": f"Bearer {self.raw_token}"},
        )
        self.assertEqual(error_code(response), "invalid_json")
        self.assertEqual(error_code(self.api("post", "/api/v1/contacts/", [1, 2])), "invalid_json")
        response = self.api("post", "/api/v1/contacts/", {"notes": "x" * 300_000})
        self.assertEqual(response.status_code, 413)
