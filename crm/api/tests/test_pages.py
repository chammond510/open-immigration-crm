from datetime import timedelta
from io import StringIO

from django.core.management import CommandError, call_command
from django.urls import reverse
from django.utils import timezone

from crm.api.models import ApiRequestLog, ApiSettings, ApiToken
from crm.models import AuditLog
from crm.tests.factories import make_user

from .helpers import ApiTestCase


class ApiAccessPageTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.staff = make_user("paralegal", superuser=False)

    def test_staff_see_their_own_tokens_and_cannot_create_by_default(self):
        mine, _ = self.issue(user=self.staff)
        self.issue()
        self.client.force_login(self.staff)
        page = self.client.get(reverse("api_access"))
        self.assertContains(page, "Only superusers can create tokens on this installation.")
        self.assertContains(page, mine.prefix)
        self.assertEqual(len(page.context["own_tokens"]), 1)
        self.assertEqual(page.context["other_tokens"], [])
        self.assertNotContains(page, "API settings</h2>")
        self.assertContains(page, 'href="/settings/api/"')
        response = self.client.get(reverse("api_token_create"), follow=True)
        self.assertContains(response, "Only superusers can create API tokens")

    def test_create_shows_the_token_once(self):
        ApiSettings.objects.filter(pk=1).update(staff_tokens_allowed=True)
        self.client.force_login(self.staff)
        form = self.client.get(reverse("api_token_create"))
        self.assertContains(form, 'value="billing:read"')
        self.assertNotContains(form, 'value="billing:write"')
        response = self.client.post(
            reverse("api_token_create"),
            {"name": "Fictional Zap", "lifetime_days": "90", "scopes": ["contacts:write"]},
        )
        self.assertEqual(response["Cache-Control"], "no-store")
        token = ApiToken.objects.get(name="Fictional Zap")
        raw_token = response.context["raw_token"]
        self.assertContains(response, raw_token)
        self.assertEqual(token.token_hash, ApiToken.hash_token(raw_token))
        self.assertEqual(token.user, self.staff)
        self.assertEqual(token.scopes, ["contacts:read", "contacts:write"])
        audit = AuditLog.objects.get(action="api_token.created")
        self.assertEqual(audit.detail, "Contacts (read and write)")
        self.assertNotContains(self.client.get(reverse("api_access")), raw_token)

        response = self.client.post(reverse("api_token_create"), {"name": "No scopes"})
        self.assertContains(response, "This field is required.")
        self.assertFalse(ApiToken.objects.filter(name="No scopes").exists())

    def test_revoking(self):
        theirs, _ = self.issue()
        mine, raw_token = self.issue(user=self.staff)
        self.client.force_login(self.staff)
        self.assertEqual(
            self.client.post(reverse("api_token_revoke", args=[theirs.pk])).status_code, 403
        )
        self.client.post(reverse("api_token_revoke", args=[mine.pk]))
        mine.refresh_from_db()
        self.assertEqual((mine.status, mine.revoked_by), ("revoked", self.staff))
        self.assertTrue(AuditLog.objects.filter(action="api_token.revoked").exists())

        self.client.force_login(self.user)
        page = self.client.get(reverse("api_access"))
        self.assertEqual([t.pk for t in page.context["other_tokens"]], [mine.pk])
        self.assertContains(page, "Revoked")
        self.client.post(reverse("api_token_revoke", args=[theirs.pk]))
        theirs.refresh_from_db()
        self.assertEqual(theirs.revoked_by, self.user)

    def test_superuser_settings_and_recent_requests(self):
        _, raw_token = self.issue()
        self.api("get", "/api/v1/", token=raw_token)
        self.client.force_login(self.staff)
        response = self.client.post(reverse("api_settings_update"), {"enabled": ""})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ApiSettings.load().enabled)
        self.assertEqual(
            self.client.get(reverse("api_access")).context["recent_requests"].count(), 0
        )

        self.client.force_login(self.user)
        page = self.client.get(reverse("api_access"))
        self.assertContains(page, "GET /api/v1/")
        self.assertEqual(ApiRequestLog.objects.count(), 1)
        response = self.client.post(reverse("api_settings_update"), {}, follow=True)
        self.assertContains(response, "The API is off.")
        self.assertContains(response, "every token is refused")
        self.assertFalse(ApiSettings.load().enabled)
        self.assertTrue(AuditLog.objects.filter(action="api_settings.updated").exists())


class PruneCommandTests(ApiTestCase):
    def test_prune_api_log(self):
        old = ApiRequestLog.objects.create(method="GET", path="/api/v1/", status_code=200)
        ApiRequestLog.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(400))
        ApiRequestLog.objects.create(method="GET", path="/api/v1/", status_code=200)
        output = StringIO()
        call_command("prune_api_log", stdout=output)
        self.assertEqual(output.getvalue().strip(), "Deleted 1 API request log row.")
        self.assertEqual(ApiRequestLog.objects.count(), 1)
        with self.assertRaises(CommandError):
            call_command("prune_api_log", "--days", "0")
