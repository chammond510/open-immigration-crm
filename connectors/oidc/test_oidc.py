import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from jwcrypto import jwk, jwt

from crm.connect.base import ConnectorError
from crm.connect.http import use_transport
from crm.connect.models import Connection, SignInLink
from crm.connect.services import acknowledge_data_flows
from crm.connect.sign_in import PENDING_KEY
from crm.connect.testing import ConnectorContract
from crm.models import AuditLog, TwoFactorDevice
from crm.tests.factories import make_user
from crm.two_factor import SESSION_PENDING_KEY, SESSION_VERIFIED_KEY

from . import GOOGLE, OidcConnector

KEY = jwk.JWK.generate(kty="RSA", size=2048, kid="fictional-key")
OTHER_KEY = jwk.JWK.generate(kty="RSA", size=2048, kid="fictional-key")
CLIENT_ID = "fictional-client-id"
SECRET = "fictional-client-secret"
DOMAIN = "firm.example.test"
TENANT = "0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0"
MICROSOFT = f"https://login.microsoftonline.com/{TENANT}/v2.0"
OTHER = "https://idp.example.test/oauth2/default"
STAFF_EMAIL = "lena@firm.example.test"
CREDENTIALS = {"client_id": CLIENT_ID, "client_secret": SECRET}
CONFIG = {"provider": "google", "domains": DOMAIN, "tenant": "", "issuer": "", "two_factor": "crm"}


class FakeProvider:
    """An identity provider that signs real ID tokens, behind a mock transport."""

    def __init__(self, issuer):
        self.issuer = issuer
        self.claims = {}
        self.key = KEY
        self.discovered_issuer = issuer
        self.methods = ["client_secret_post", "client_secret_basic"]
        self.token_status = 200
        self.codes = {}
        self.requests = []

    def authorize(self, nonce):
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = nonce
        return code

    def id_token(self, nonce):
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": CLIENT_ID,
            "sub": "fictional-subject-1",
            "email": STAFF_EMAIL,
            "email_verified": True,
            "hd": DOMAIN,
            "name": "Lena Fictional",
            "iat": now,
            "exp": now + 300,
            "nonce": nonce,
            **self.claims,
        }
        token = jwt.JWT(
            header={"alg": "RS256", "kid": "fictional-key"},
            claims={k: v for k, v in claims.items() if v is not None},
        )
        token.make_signed_token(self.key)
        return token.serialize()

    def handler(self, request):
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": self.discovered_issuer,
                    "authorization_endpoint": "https://idp.example.test/authorize",
                    "token_endpoint": "https://idp.example.test/token",
                    "jwks_uri": "https://idp.example.test/keys",
                    "token_endpoint_auth_methods_supported": self.methods,
                },
            )
        if path == "/keys":
            return httpx.Response(200, json={"keys": [json.loads(KEY.export_public())]})
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            basic = base64.b64decode(request.headers.get("authorization", "")[6:]).decode()
            given = form.get("client_secret") or basic.rpartition(":")[2]
            if given != SECRET:
                return httpx.Response(401, json={"error": "invalid_client"})
            if form.get("code") not in self.codes:
                return httpx.Response(400, json={"error": "invalid_grant"})
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            nonce = self.codes.pop(form["code"])
            return httpx.Response(200, json={"id_token": self.id_token(nonce)})
        return httpx.Response(404)


class ProviderMixin:
    issuer = GOOGLE

    def setUp(self):
        super().setUp()
        self.provider = FakeProvider(self.issuer)
        self.enterContext(use_transport(httpx.MockTransport(self.provider.handler)))


class OidcContractTests(ProviderMixin, ConnectorContract, TestCase):
    connector_class = OidcConnector
    valid_credentials = CREDENTIALS
    invalid_credentials = {"client_id": CLIENT_ID, "client_secret": "wrong"}
    connection_config = CONFIG

    def sign_in_answer(self, sign_in_url, nonce):
        query = parse_qs(urlsplit(sign_in_url).query)
        return {"code": self.provider.authorize(query["nonce"][0]), "state": query["state"][0]}


class SignInTestCase(ProviderMixin, TestCase):
    config = CONFIG

    def setUp(self):
        super().setUp()
        self.admin = make_user()
        self.staff = make_user("lena", superuser=False)
        self.staff.email = STAFF_EMAIL
        self.staff.save()
        self.connection = Connection.objects.create(
            connector_key="oidc",
            label="Firm Workspace",
            enabled_capabilities=["sign_in"],
            credentials=dict(CREDENTIALS),
            config=dict(self.config),
        )
        acknowledge_data_flows(self.connection, self.admin)

    def start(self, next_url=""):
        url = reverse("staff_sign_in", args=[self.connection.pk])
        response = self.client.get(url, {"next": next_url} if next_url else {})
        self.assertEqual(response.status_code, 302)
        return parse_qs(urlsplit(response["Location"]).query)

    def sign_in(self, next_url=""):
        query = self.start(next_url)
        code = self.provider.authorize(query["nonce"][0])
        return self.client.get(
            reverse("staff_sign_in_callback"), {"state": query["state"][0], "code": code}
        )

    def signed_in_user(self):
        return self.client.session.get("_auth_user_id")


class SignInFlowTests(SignInTestCase):
    def test_the_sign_in_page_offers_the_provider(self):
        page = self.client.get(reverse("login"), {"next": "/matters/"})
        self.assertContains(page, "Sign in with Firm Workspace")
        self.client.force_login(self.admin)
        catalog = self.client.get(reverse("connections"), {"capability": "sign_in"})
        self.assertContains(catalog, "Works through OpenID Connect", count=2)  # Okta, JumpCloud
        self.client.logout()
        self.assertContains(page, "?next=/matters/")
        query = self.start()
        self.assertEqual(
            (query["client_id"], query["hd"], query["scope"]),
            ([CLIENT_ID], [DOMAIN], ["openid email profile"]),
        )

    def test_first_sign_in_links_the_account_by_verified_email(self):
        response = self.sign_in(next_url="/matters/")
        self.assertRedirects(response, "/matters/", fetch_redirect_response=False)
        self.assertEqual(self.signed_in_user(), str(self.staff.pk))
        self.assertNotIn(SESSION_VERIFIED_KEY, self.client.session)
        link = SignInLink.objects.get()
        self.assertEqual(
            (link.user, link.issuer, link.subject), (self.staff, GOOGLE, "fictional-subject-1")
        )
        self.assertEqual(
            list(AuditLog.objects.values_list("action", flat=True).order_by("pk"))[-2:],
            ["user.sign_in_linked", "user.signed_in"],
        )
        admin = self.client_class()
        admin.force_login(self.admin)
        listed = admin.get(reverse("admin:connect_signinlink_changelist"))
        self.assertContains(listed, "Sign-in links")
        self.assertContains(listed, GOOGLE)
        # Later sign-ins follow the link, not the email address.
        self.client.logout()
        self.staff.email = "lena.new@firm.example.test"
        self.staff.save()
        self.sign_in()
        self.assertEqual(self.signed_in_user(), str(self.staff.pk))

    def test_accounts_that_cannot_sign_in(self):
        self.staff.email = "someone.else@firm.example.test"
        self.staff.save()
        response = self.client.get(self.sign_in().url)
        self.assertContains(response, "No staff account matches that Firm Workspace sign-in.")
        self.assertIsNone(self.signed_in_user())
        self.assertTrue(AuditLog.objects.filter(action="user.sign_in_refused").exists())
        # Two accounts with the same address: neither is chosen.
        self.staff.email = STAFF_EMAIL
        self.staff.save()
        make_user("other", superuser=False).__class__.objects.filter(username="other").update(
            email=STAFF_EMAIL.upper()
        )
        self.sign_in()
        self.assertIsNone(self.signed_in_user())
        # A linked account that was turned off.
        get_user_model().objects.filter(username="other").delete()
        self.sign_in()
        self.client.logout()
        self.staff.is_active = False
        self.staff.save()
        self.sign_in()
        self.assertIsNone(self.signed_in_user())
        # An email the provider does not vouch for.
        SignInLink.objects.all().delete()
        self.staff.is_active = True
        self.staff.save()
        self.provider.claims = {"email_verified": "false"}
        self.sign_in()
        self.assertIsNone(self.signed_in_user())
        self.provider.claims = {"email_verified": "true"}
        self.sign_in()
        self.assertEqual(self.signed_in_user(), str(self.staff.pk))

    def test_the_crm_second_step_still_applies(self):
        TwoFactorDevice.objects.create(
            user=self.staff, secret="JBSWY3DPEHPK3PXP", confirmed_at=timezone.now()
        )
        response = self.sign_in(next_url="/work/")
        self.assertRedirects(response, reverse("two_factor_verify"), fetch_redirect_response=False)
        self.assertIsNone(self.signed_in_user())
        self.assertEqual(self.client.session[SESSION_PENDING_KEY]["next"], "/work/")
        # The provider's second factor counts only when the firm chose it and it is reported.
        self.connection.config = {**CONFIG, "two_factor": "provider"}
        self.connection.save()
        self.client.session.flush()
        self.sign_in()
        self.assertIsNone(self.signed_in_user())
        self.provider.claims = {"amr": ["pwd", "mfa"]}
        response = self.sign_in()
        self.assertEqual(self.signed_in_user(), str(self.staff.pk))
        self.assertTrue(self.client.session[SESSION_VERIFIED_KEY])
        self.assertEqual(
            AuditLog.objects.get(action="user.signed_in").detail, "Provider's second factor"
        )

    def test_answers_that_did_not_start_here(self):
        callback = reverse("staff_sign_in_callback")
        response = self.client.get(callback, {"state": "made-up", "code": "x"}, follow=True)
        self.assertContains(response, "That sign-in expired or did not start here.")
        query = self.start()
        response = self.client.get(callback, {"state": "wrong", "code": "x"})
        self.assertIsNone(self.signed_in_user())
        query = self.start()
        session = self.client.session
        session[PENDING_KEY]["created"] -= 3600
        session.save()
        code = self.provider.authorize(query["nonce"][0])
        self.client.get(callback, {"state": query["state"][0], "code": code})
        self.assertIsNone(self.signed_in_user())
        query = self.start()
        response = self.client.get(
            callback, {"state": query["state"][0], "error": "access_denied"}, follow=True
        )
        self.assertContains(response, "Firm Workspace did not sign you in.")
        query = self.start()
        self.connection.status = Connection.Status.DISCONNECTED
        self.connection.save()
        response = self.client.get(callback, {"state": query["state"][0], "code": "x"}, follow=True)
        self.assertContains(response, "That way of signing in was turned off.")
        self.assertEqual(
            self.client.get(reverse("staff_sign_in", args=[self.connection.pk])).status_code, 404
        )
        self.assertNotContains(self.client.get(reverse("login")), "Sign in with")

    def test_an_answer_that_cannot_be_checked(self):
        self.provider.key = OTHER_KEY
        response = self.client.get(self.sign_in().url)
        self.assertContains(response, "Firm Workspace sign-in could not be checked")
        self.assertTrue(AuditLog.objects.filter(action="user.sign_in_failed").exists())
        self.provider.discovered_issuer = "https://elsewhere.example.test"
        response = self.client.get(reverse("staff_sign_in", args=[self.connection.pk]), follow=True)
        self.assertContains(response, "Signing in with Firm Workspace is not working")

    def test_unsafe_next_addresses_are_dropped(self):
        self.sign_in(next_url="https://elsewhere.example.test/")
        self.assertEqual(self.signed_in_user(), str(self.staff.pk))

    @override_settings(ALLOWED_HOSTS=["crm.example.test"])
    def test_the_connect_page_shows_the_return_address(self):
        self.client.force_login(self.admin)
        page = self.client.get(
            reverse("connector_detail", args=["oidc"]), HTTP_HOST="crm.example.test"
        )
        self.assertContains(page, "http://crm.example.test/accounts/sign-in/callback/")


class ConnectorCheckTests(SignInTestCase):
    def verify(self, **claims):
        connector = OidcConnector(Connection.objects.get(pk=self.connection.pk))
        self.provider.claims = claims
        code = self.provider.authorize("nonce-1")
        return connector.verify_sign_in(
            {"code": code}, "https://crm.example.test/cb/", "verifier", "nonce-1"
        )

    def test_id_token_checks(self):
        identity = self.verify(iss="accounts.google.com", email="Lena@Firm.Example.Test")
        self.assertEqual((identity.issuer, identity.email), (GOOGLE, STAFF_EMAIL))
        now = int(time.time())
        cases = [
            {"iss": "https://elsewhere.example.test"},
            {"aud": "another-client"},
            {"aud": [CLIENT_ID, "another-client"]},
            {"azp": "another-client"},
            {"exp": now - 600},
            {"iat": now + 3600},
            {"iat": None},
            {"nonce": "nonce-2"},
            {"sub": None},
            {"hd": None},
            {"hd": "other.example.test"},
            {"email": "lena@other.example.test"},
        ]
        for claims in cases:
            with self.subTest(claims=claims), self.assertRaises(ConnectorError):
                self.verify(**claims)
        connector = OidcConnector(self.connection)
        with self.assertRaisesMessage(ConnectorError, "different identity provider"):
            connector.verify_sign_in({"code": "x", "iss": OTHER}, "https://x/", "v", "n")

    def test_token_endpoint_answers(self):
        connector = OidcConnector(self.connection)
        with self.assertRaisesMessage(ConnectorError, "refused the sign-in"):
            connector.verify_sign_in({"code": "unknown"}, "https://x/", "v", "n")
        self.provider.methods = ["client_secret_basic"]
        self.assertEqual(self.verify().subject, "fictional-subject-1")
        self.connection.credentials = {**CREDENTIALS, "client_secret": "wrong"}
        with self.assertRaisesMessage(ConnectorError, "no longer accepts"):
            OidcConnector(self.connection).verify_sign_in({"code": "x"}, "https://x/", "v", "n")
        with self.assertRaisesMessage(ConnectorError, "did not accept the client ID"):
            OidcConnector(self.connection).test_connection()

    def test_settings_problems(self):
        for config, message in (
            ({"domains": ""}, "Enter the staff email domains."),
            ({"provider": "microsoft", "tenant": "firm.example.test"}, "tenant ID"),
            ({"provider": "other", "issuer": ""}, "issuer address"),
            ({"provider": "other", "issuer": "http://idp.example.test"}, "HTTPS"),
        ):
            self.connection.config = {**CONFIG, **config}
            with self.subTest(config=config), self.assertRaisesMessage(ConnectorError, message):
                OidcConnector(self.connection).test_connection()
        self.connection.config = CONFIG
        self.connection.credentials = {"client_id": CLIENT_ID}
        with self.assertRaisesMessage(ConnectorError, "client ID and secret"):
            OidcConnector(self.connection).test_connection()


class MicrosoftTests(SignInTestCase):
    issuer = MICROSOFT
    config = {**CONFIG, "provider": "microsoft", "tenant": TENANT.upper()}

    def setUp(self):
        super().setUp()
        self.provider.claims = {
            "hd": None,
            "email_verified": None,
            "tid": TENANT,
            "oid": "fictional-object-id",
            "xms_edov": True,
        }

    def test_entra_links_by_object_id_with_a_verified_domain(self):
        self.assertEqual(self.start()["prompt"], ["select_account"])
        self.sign_in()
        self.assertEqual(self.signed_in_user(), str(self.staff.pk))
        link = SignInLink.objects.get()
        self.assertEqual((link.issuer, link.subject), (MICROSOFT, "fictional-object-id"))

    def test_entra_refuses_guests_other_tenants_and_unverified_domains(self):
        base = dict(self.provider.claims)
        for claims in ({"acct": 1}, {"tid": "another-tenant"}, {"xms_edov": None}):
            self.provider.claims = {**base, **claims}
            with self.subTest(claims=claims):
                self.sign_in()
                self.assertIsNone(self.signed_in_user())
        self.assertFalse(SignInLink.objects.exists())


class OtherProviderTests(SignInTestCase):
    issuer = OTHER
    config = {**CONFIG, "provider": "other", "issuer": OTHER}

    def test_another_provider_uses_its_own_claims(self):
        self.provider.claims = {"hd": None}
        query = self.start()
        self.assertNotIn("prompt", query)
        self.sign_in()
        self.assertEqual(self.signed_in_user(), str(self.staff.pk))
        self.assertEqual(SignInLink.objects.get().issuer, OTHER)
