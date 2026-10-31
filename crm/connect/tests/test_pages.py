import base64
import hashlib
from dataclasses import replace
from unittest import mock
from urllib.parse import parse_qs, urlparse

import httpx
from django.template import Context, Template
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.connect import http, oauth
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, ConnectorApp, WorkerHeartbeat
from crm.connect.registry import registry
from crm.connect.services import acknowledge_data_flows, create_connection
from crm.models import AuditLog
from crm.tests.factories import make_user

# Core reaches connectors only through the registry, never by import.
ExampleConnector = registry.get("example")
VALID_KEY = "example-fictional-key"


def example_connection(user, **overrides):
    options = {
        "label": "Front desk texts",
        "created_by": user,
        "capabilities": ["texting"],
        "credentials": {"api_key": VALID_KEY},
        **overrides,
    }
    return create_connection("example", **options)


@override_settings(CONNECTORS_SHOW_HIDDEN=True)
class CatalogTests(TestCase):
    def setUp(self):
        self.admin = make_user()
        self.staff = make_user("paralegal", superuser=False)

    def test_catalog_lists_wanted_and_test_connectors_by_capability(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("connections"))
        self.assertContains(response, "Connect your services")
        self.assertContains(response, 'href="/connections/"')
        self.assertContains(response, "Phone")
        self.assertContains(response, "RingCentral")
        self.assertContains(response, "Not built yet")
        self.assertContains(response, "Example service")
        self.assertContains(response, "Test only")
        self.assertContains(response, "not affiliated with or endorsed by")

    def test_a_wanted_service_that_works_through_another_connector(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("connections"), {"capability": "system_email"})
        self.assertContains(response, "Postmark")
        self.assertContains(response, "Works through SMTP server")
        wanted = next(entry for entry in registry.catalog() if entry.name == "Postmark")
        page = self.client.get(reverse("connector_detail", args=[wanted.key]))
        self.assertContains(page, "works with the SMTP server connector")
        self.assertContains(page, reverse("connector_detail", args=["smtp"]))
        page = self.client.get(reverse("connector_detail", args=["ringcentral"]))
        self.assertContains(page, "Not built yet")

    def test_filters(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("connections"), {"capability": "payments"})
        self.assertContains(response, "Stripe")
        self.assertNotContains(response, "RingCentral")
        response = self.client.get(reverse("connections"), {"q": "ring"})
        self.assertContains(response, "RingCentral")
        self.assertNotContains(response, "Stripe")
        response = self.client.get(reverse("connections"), {"maturity": "beta"})
        self.assertContains(response, "Example service")
        self.assertNotContains(response, "RingCentral")
        response = self.client.get(reverse("connections"), {"q": "no such service"})
        self.assertContains(response, "No services match")

    @override_settings(CONNECTORS_SHOW_HIDDEN=False)
    def test_hidden_connectors_stay_hidden_by_default(self):
        self.client.force_login(self.admin)
        self.assertNotContains(self.client.get(reverse("connections")), "Example service")
        response = self.client.get(reverse("connector_detail", args=["example"]))
        self.assertEqual(response.status_code, 404)

    def test_your_connections_respect_visibility(self):
        firm = example_connection(self.admin)
        with mock.patch.object(registry.get("example_oauth"), "test_connection"):
            personal = create_connection(
                "example_oauth",
                label="Admin calendar",
                created_by=self.admin,
                capabilities=["calendar"],
                owner=self.admin,
            )
        self.client.force_login(self.staff)
        response = self.client.get(reverse("connections"))
        self.assertContains(response, firm.label)
        self.assertNotContains(response, personal.label)
        self.assertEqual(
            self.client.get(reverse("connection_detail", args=[personal.pk])).status_code, 404
        )
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("connections")), personal.label)

    def test_wanted_and_unknown_connectors(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("connector_detail", args=["ringcentral"]))
        self.assertContains(response, "No connector for RingCentral exists yet")
        self.assertEqual(
            self.client.get(reverse("connector_detail", args=["nonexistent"])).status_code, 404
        )


@override_settings(CONNECTORS_SHOW_HIDDEN=True)
class ApiKeyConnectTests(TestCase):
    def setUp(self):
        self.admin = make_user()
        self.client.force_login(self.admin)
        self.url = reverse("connector_detail", args=["example"])

    def post(self, **overrides):
        data = {
            "label": "Front desk texts",
            "capabilities": ["texting"],
            "credential_api_key": VALID_KEY,
            "acknowledge": "on",
            **overrides,
        }
        return self.client.post(self.url, {k: v for k, v in data.items() if v is not None})

    def test_superuser_connects_reviews_and_tests_in_one_step(self):
        page = self.client.get(self.url)
        self.assertContains(page, "What this connection sends and receives")
        fields = list(page.context["form"].fields)
        self.assertEqual(fields[-1], "acknowledge")
        self.assertLess(fields.index("credential_api_key"), fields.index("acknowledge"))
        self.assertContains(page, "Fictional messages staff approve")
        response = self.post()
        connection = Connection.objects.get()
        self.assertRedirects(
            response,
            reverse("connection_detail", args=[connection.pk]),
            fetch_redirect_response=False,
        )
        self.assertTrue(connection.data_flows_acknowledged)
        self.assertEqual(connection.status, Connection.Status.ACTIVE)
        self.assertTrue(connection.preview_mode)
        page = self.client.get(response.url)
        self.assertContains(page, "Connected to Example service")
        self.assertNotContains(page, VALID_KEY)

    def test_acknowledgment_is_required_for_superusers(self):
        response = self.post(acknowledge=None)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Connection.objects.exists())

    def test_rejected_credentials_are_reported(self):
        response = self.post(credential_api_key="wrong")
        connection = Connection.objects.get()
        self.assertEqual(connection.status, Connection.Status.NEEDS_ATTENTION)
        self.assertContains(self.client.get(response.url), "did not accept those credentials")

    def test_staff_cannot_connect_firm_wide_services(self):
        self.client.force_login(make_user("paralegal", superuser=False))
        self.assertContains(
            self.client.get(self.url), "Only people who manage connections and AI can connect"
        )
        self.assertEqual(self.post(acknowledge=None).status_code, 403)


@override_settings(CONNECTORS_SHOW_HIDDEN=True)
class OAuthConnectTests(TestCase):
    def setUp(self):
        self.admin = make_user()
        self.staff = make_user("paralegal", superuser=False)
        self.detail_url = reverse("connector_detail", args=["example_oauth"])

    def save_app(self):
        self.client.force_login(self.admin)
        return self.client.post(
            reverse("connector_app", args=["example_oauth"]),
            {"client_id": "fictional-client", "client_secret": "fictional-secret"},
        )

    def connect_as_staff(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            self.detail_url, {"label": "My calendar", "capabilities": ["calendar"]}
        )
        return Connection.objects.get(), response

    def test_app_registration_is_superuser_only_and_encrypted(self):
        self.client.force_login(self.staff)
        page = self.client.get(self.detail_url)
        self.assertContains(page, "Someone who manages connections and AI must register")
        self.assertEqual(
            self.client.post(
                reverse("connector_app", args=["example_oauth"]), {"client_id": "x"}
            ).status_code,
            403,
        )
        self.assertRedirects(self.save_app(), self.detail_url)
        app = ConnectorApp.objects.get()
        self.assertEqual(app.client_secret, "fictional-secret")
        self.assertTrue(AuditLog.objects.filter(action="connector_app.saved").exists())
        page = self.client.get(self.detail_url)
        self.assertContains(page, "http://testserver/connections/oauth/callback/")
        self.client.post(reverse("connector_app", args=["example_oauth"]), {"client_id": "x"})
        self.assertEqual(ConnectorApp.objects.get().client_id, "fictional-client")

    def test_full_sign_in(self):
        self.save_app()
        connection, response = self.connect_as_staff()
        self.assertEqual(connection.owner, self.staff)
        self.assertEqual(connection.status, Connection.Status.NEEDS_REAUTH)
        page = self.client.get(response.url)
        self.assertContains(page, "Sign in to Example calendar")

        start = self.client.get(reverse("connection_oauth_start", args=[connection.pk]))
        location = urlparse(start["Location"])
        self.assertEqual(f"{location.scheme}://{location.netloc}", "https://auth.example.test")
        query = {key: values[0] for key, values in parse_qs(location.query).items()}
        self.assertEqual(query["client_id"], "fictional-client")
        self.assertEqual(query["scope"], "calendar.read")
        self.assertEqual(query["access_type"], "offline")
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertEqual(query["redirect_uri"], "http://testserver/connections/oauth/callback/")

        tokens = {"access_token": "example-token", "refresh_token": "r", "expires_in": 3600}
        with mock.patch.object(oauth, "post_form", return_value=tokens) as token_call:
            done = self.client.get(
                reverse("connection_oauth_callback"), {"state": query["state"], "code": "abc"}
            )
        self.assertRedirects(done, reverse("connection_detail", args=[connection.pk]))
        sent = token_call.call_args.args[1]
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(sent["code_verifier"].encode()).digest()
        )
        self.assertEqual(challenge.decode().rstrip("="), query["code_challenge"])
        self.assertEqual(sent["client_secret"], "fictional-secret")
        connection.refresh_from_db()
        self.assertEqual(connection.status, Connection.Status.ACTIVE)
        self.assertEqual(connection.credentials["access_token"], "example-token")
        self.assertTrue(AuditLog.objects.filter(action="connection.signed_in").exists())
        # A pending sign-in is single-use.
        replay = self.client.get(
            reverse("connection_oauth_callback"), {"state": query["state"], "code": "abc"}
        )
        self.assertRedirects(replay, reverse("connections"))

    def test_bad_state_refusal_and_provider_errors(self):
        self.save_app()
        connection, _ = self.connect_as_staff()
        start = self.client.get(reverse("connection_oauth_start", args=[connection.pk]))
        state = parse_qs(urlparse(start["Location"]).query)["state"][0]
        wrong = self.client.get(
            reverse("connection_oauth_callback"), {"state": "forged", "code": "abc"}
        )
        self.assertRedirects(wrong, reverse("connection_detail", args=[connection.pk]))

        self.client.get(reverse("connection_oauth_start", args=[connection.pk]))
        state = self.client.session[oauth.PENDING_KEY]["state"]
        refused = self.client.get(
            reverse("connection_oauth_callback"), {"state": state, "error": "access_denied"}
        )
        self.assertContains(self.client.get(refused.url), "cancelled or refused")

        self.client.get(reverse("connection_oauth_start", args=[connection.pk]))
        state = self.client.session[oauth.PENDING_KEY]["state"]
        failure = ConnectorError("The provider refused the sign-in (HTTP 400).")
        with mock.patch.object(oauth, "post_form", side_effect=failure):
            self.client.get(reverse("connection_oauth_callback"), {"state": state, "code": "x"})
        connection.refresh_from_db()
        self.assertEqual(connection.status, Connection.Status.NEEDS_REAUTH)
        self.assertIn("HTTP 400", connection.last_error)

    def test_expired_state_and_missing_app(self):
        self.client.force_login(self.staff)
        with mock.patch.object(registry.get("example_oauth"), "test_connection"):
            connection = create_connection(
                "example_oauth",
                label="Mine",
                created_by=self.staff,
                capabilities=["calendar"],
                owner=self.staff,
            )
        start = self.client.get(reverse("connection_oauth_start", args=[connection.pk]))
        self.assertRedirects(
            start,
            reverse("connection_detail", args=[connection.pk]),
            fetch_redirect_response=False,
        )
        self.assertContains(self.client.get(start.url), "must first enter the firm")

        ConnectorApp.objects.create(connector_key="example_oauth", client_id="c", client_secret="s")
        self.client.get(reverse("connection_oauth_start", args=[connection.pk]))
        session = self.client.session
        session[oauth.PENDING_KEY]["created"] -= oauth.STATE_TTL_SECONDS + 1
        state = session[oauth.PENDING_KEY]["state"]
        session.save()
        expired = self.client.get(
            reverse("connection_oauth_callback"), {"state": state, "code": "abc"}
        )
        self.assertContains(self.client.get(expired.url), "expired")

    def test_others_cannot_start_or_finish_your_sign_in(self):
        self.save_app()
        connection, _ = self.connect_as_staff()
        other = make_user("associate", superuser=False)
        self.client.force_login(other)
        self.assertEqual(
            self.client.get(reverse("connection_oauth_start", args=[connection.pk])).status_code,
            404,
        )
        response = self.client.get(reverse("connection_oauth_callback"), {"state": "x"})
        self.assertRedirects(response, reverse("connections"))


class TokenRequestTests(TestCase):
    def post(self, handler):
        with http.use_transport(httpx.MockTransport(handler)), mock.patch.object(http, "sleep"):
            return oauth.post_form("https://auth.example.test/token", {"grant_type": "x"})

    def test_post_form_rules(self):
        with self.assertRaisesMessage(ConnectorError, "HTTPS"):
            oauth.post_form("http://auth.example.test/token", {})
        sent = []

        def ok(request):
            sent.append(request)
            return httpx.Response(200, json={"a": 1})

        self.assertEqual(self.post(ok), {"a": 1})
        self.assertEqual(sent[0].content, b"grant_type=x")
        self.assertEqual(sent[0].headers["Accept"], "application/json")
        with self.assertRaisesMessage(ConnectorError, "not understood"):
            self.post(lambda request: httpx.Response(200, content=b"not json"))
        with self.assertRaisesMessage(ConnectorError, "not understood"):
            self.post(lambda request: httpx.Response(200, json=["a list"]))
        big = b"x" * (oauth.TOKEN_RESPONSE_MAX_BYTES + 2)
        with self.assertRaisesMessage(ConnectorError, "too large"):
            self.post(lambda request: httpx.Response(200, content=big))
        with self.assertRaises(oauth.TokenRefused):
            self.post(lambda request: httpx.Response(401, json={"error": "invalid_grant"}))

        def down(request):
            raise httpx.ConnectError("down")

        with self.assertRaisesMessage(ConnectorError, "could not be reached"):
            self.post(down)


@override_settings(CONNECTORS_SHOW_HIDDEN=True)
class ConnectionManagementTests(TestCase):
    def setUp(self):
        self.admin = make_user()
        self.staff = make_user("paralegal", superuser=False)
        self.connection = example_connection(self.admin)

    def url(self, name):
        return reverse(name, args=[self.connection.pk])

    def test_staff_can_view_but_not_manage_firm_connections(self):
        self.client.force_login(self.staff)
        page = self.client.get(self.url("connection_detail"))
        self.assertContains(page, "Someone who manages connections and AI must review")
        self.assertNotContains(page, "Disconnect")
        for name in (
            "connection_test",
            "connection_disconnect",
            "connection_webhooks",
            "connection_acknowledge",
            "connection_settings",
        ):
            self.assertEqual(self.client.post(self.url(name)).status_code, 403, name)

    def test_review_settings_and_test(self):
        self.client.force_login(self.admin)
        self.client.post(self.url("connection_acknowledge"))
        self.connection.refresh_from_db()
        self.assertTrue(self.connection.data_flows_acknowledged)
        self.client.post(self.url("connection_settings"), {"outbound_admin_only": "on"})
        self.connection.refresh_from_db()
        self.assertFalse(self.connection.preview_mode)
        self.assertTrue(self.connection.outbound_admin_only)
        entry = AuditLog.objects.get(action="connection.settings_changed")
        self.assertEqual(entry.detail, "preview off, admin-only on")
        response = self.client.post(self.url("connection_test"))
        self.assertContains(self.client.get(response.url), "The connection test passed")
        Connection.objects.filter(pk=self.connection.pk).update(credentials={"api_key": "wrong"})
        response = self.client.post(self.url("connection_test"))
        self.assertContains(self.client.get(response.url), "rejected this API key")

    def test_webhook_address_and_secret_are_shown_once(self):
        self.client.force_login(self.admin)
        page = self.client.post(self.url("connection_webhooks"))
        self.assertContains(page, "http://testserver/hooks/")
        self.assertContains(page, "Signing secret")
        self.connection.refresh_from_db()
        self.assertEqual(page.context["secret"], self.connection.webhook_secret)
        detail = self.client.get(self.url("connection_detail"))
        self.assertContains(detail, "Issue a new address and secret")
        self.assertNotContains(detail, self.connection.webhook_secret)

    def test_provider_issued_secrets(self):
        self.client.force_login(self.admin)
        spec = replace(ExampleConnector.webhooks, secret_source="provider")
        with mock.patch.object(ExampleConnector, "webhooks", spec):
            self.assertContains(
                self.client.get(self.url("connection_detail")), "Signing secret from the provider"
            )
            missing = self.client.post(self.url("connection_webhooks"))
            self.assertContains(self.client.get(missing.url), "Paste the signing secret")
            page = self.client.post(
                self.url("connection_webhooks"), {"provider_secret": "provider-issued"}
            )
        self.assertContains(page, "http://testserver/hooks/")
        self.assertNotContains(page, "provider-issued")

    def test_disconnect(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.url("connection_disconnect"))
        page = self.client.get(response.url)
        self.assertContains(page, "Disconnected. Stored credentials were deleted")
        self.assertContains(page, "Connect Example service again")
        self.connection.refresh_from_db()
        self.assertIsNone(self.connection.credentials)

    def test_uninstalled_connector_page(self):
        orphan = Connection.objects.create(connector_key="uninstalled", label="Old service")
        self.client.force_login(self.admin)
        page = self.client.get(reverse("connection_detail", args=[orphan.pk]))
        self.assertContains(page, "not installed on this site")


class PromptAndAlertTests(TestCase):
    def setUp(self):
        self.admin = make_user()
        self.request = RequestFactory().get("/")
        self.request.user = self.admin

    def render(self, source):
        return Template("{% load connect_tags %}" + source).render(
            Context({"request": self.request})
        )

    def test_connect_prompt(self):
        html = self.render('{% connect_prompt "texting" "to send reminders" %}')
        self.assertIn("Connect your texting service to send reminders.", html)
        self.assertIn("/connections/?capability=texting", html)
        connection = example_connection(self.admin)
        html = self.render('{% connect_prompt "texting" %}')
        self.assertIn(f'Connected through <a href="/connections/{connection.pk}/">', html)
        self.assertEqual(self.render("{{ 'payments'|capability_label }}"), "Payments")
        self.assertEqual(self.render("{{ 'unknown'|capability_label }}"), "unknown")

    @override_settings(WORKER_TICK_SECONDS=60)
    def test_dashboard_alerts(self):
        self.client.force_login(self.admin)
        self.assertNotContains(self.client.get(reverse("dashboard")), "need attention")
        connection = example_connection(self.admin)
        acknowledge_data_flows(connection, self.admin)
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, "background worker has not checked in")
        WorkerHeartbeat.objects.create(worker_id="w", last_seen_at=timezone.now())
        Connection.objects.filter(pk=connection.pk).update(status=Connection.Status.NEEDS_ATTENTION)
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, "1 connection needs attention")
        self.assertNotContains(page, "background worker")
