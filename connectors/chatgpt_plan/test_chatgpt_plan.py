import json
import time
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx
from django.test import TestCase, override_settings
from django.urls import reverse
from jwcrypto import jwk, jwt

from crm.ai.client import AIUnavailable, Message, Tool, generate
from crm.ai.models import AISettings
from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection
from crm.connect.registry import registry
from crm.connect.services import acknowledge_data_flows
from crm.connect.testing import ConnectorContract
from crm.tests.factories import make_contact, make_matter, make_user

from . import LOOPBACK, ChatGPTPlanConnector, read_stream

FLAG = override_settings(CONNECTOR_FLAGS=frozenset({"chatgpt_plan"}))
CLIENT_ID = "oaiapp_fictional"
SIGNING_KEY = jwk.JWK.generate(kty="RSA", size=2048, kid="fictional-key")
OTHER_KEY = jwk.JWK.generate(kty="RSA", size=2048, kid="fictional-key")


def id_token(nonce, *, key=SIGNING_KEY, audience=CLIENT_ID, issuer="https://auth.openai.com"):
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": "user-fictional",
        "email": "paralegal@example.test",
        "nonce": nonce,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    token = jwt.JWT(header={"alg": "RS256", "kid": "fictional-key"}, claims=claims)
    token.make_signed_token(key)
    return token.serialize()


def stream(*events):
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


def completed(text="ready", output=None):
    output = output or [{"type": "message", "content": [{"type": "output_text", "text": text}]}]
    response = {
        "id": "resp_fictional",
        "model": "gpt-fictional",
        "status": "completed",
        "output": output,
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }
    return {"type": "response.completed", "response": response}


class FakeOpenAI:
    """OpenAI's sign-in and Responses endpoints, as the Sign in with ChatGPT docs describe."""

    def __init__(self):
        self.requests = []
        self.nonce = ""
        self.token_key = SIGNING_KEY
        self.scope = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
        self.refresh_error = ""
        self.events = [completed()]
        self.api_error = None
        self.models = [
            {"slug": "gpt-fictional", "display_name": "GPT Fictional", "visibility": "list"},
            {"slug": "gpt-hidden", "display_name": "Hidden", "visibility": "hide"},
        ]

    def __call__(self, request):
        self.requests.append(request)
        url = str(request.url)
        if url == "https://auth.openai.com/.well-known/jwks.json":
            return httpx.Response(200, json={"keys": [json.loads(SIGNING_KEY.export_public())]})
        if url == "https://auth.openai.com/api/accounts/oauth/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            assert form["resource"] == "https://api.openai.com/v1"
            if form["grant_type"] == "refresh_token":
                if self.refresh_error:
                    return httpx.Response(400, json={"error": self.refresh_error})
                return httpx.Response(
                    200,
                    json={
                        "access_token": "access-2",
                        "refresh_token": "refresh-2",
                        "expires_in": 3600,
                    },
                )
            assert form["client_id"] == CLIENT_ID and form["code_verifier"]
            assert form["redirect_uri"] == LOOPBACK
            if form["code"] != "fictional-code":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "id_token": id_token(self.nonce, key=self.token_key),
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": self.scope,
                },
            )
        if url == "https://auth.openai.com/api/accounts/oauth/revoke":
            return httpx.Response(200)
        if not request.headers.get("Authorization", "").startswith("Bearer access-"):
            return httpx.Response(401, json={"detail": "Not signed in"})
        if url.endswith("/v1/models"):
            return httpx.Response(200, json={"models": self.models})
        if self.api_error:
            status, code = self.api_error
            return httpx.Response(status, json={"error": {"code": code, "message": "fictional"}})
        return httpx.Response(
            200, text=stream(*self.events), headers={"content-type": "text/event-stream"}
        )

    def last(self, ending):
        return next(r for r in reversed(self.requests) if str(r.url).endswith(ending))


class FakeOpenAIMixin:
    def setUp(self):
        super().setUp()
        self.openai = FakeOpenAI()
        transport = http.use_transport(httpx.MockTransport(lambda r: self.openai(r)))
        transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)
        patcher = mock.patch.object(http, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)


def signed_in(connection, **extra):
    connection.credentials = {
        "client_id": CLIENT_ID,
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "id_token": "earlier-id-token",
        "expires_at": time.time() + 3600,
        "scopes": ["chatgpt.tokens.use.direct"],
        **extra,
    }
    connection.status = Connection.Status.ACTIVE
    connection.preview_mode = False
    connection.save()
    acknowledge_data_flows(connection, make_user("reviewer"))
    connection.refresh_from_db()
    return connection


@FLAG
class ChatGPTPlanContractTests(FakeOpenAIMixin, ConnectorContract, TestCase):
    connector_class = ChatGPTPlanConnector
    valid_credentials = {
        "client_id": CLIENT_ID,
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_at": time.time() + 86400,
    }
    invalid_credentials = {
        "client_id": CLIENT_ID,
        "access_token": "revoked",
        "expires_at": time.time() + 86400,
    }
    outbound_examples = {
        "generate": {
            "messages": [{"role": "user", "content": "Hello", "tool_calls": [], "tool_call_id": ""}]
        }
    }


class ChatGPTPlanFlagTests(TestCase):
    def test_offered_only_behind_its_flag(self):
        admin = make_user()
        self.client.force_login(admin)
        self.assertIsNone(registry.get("chatgpt_plan"))
        wanted = self.client.get(reverse("connector_detail", args=["chatgpt_plan"]))
        self.assertNotContains(wanted, "Continue with ChatGPT")
        catalog = self.client.get(reverse("connections"))
        self.assertContains(catalog, "ChatGPT plan (Sign in with ChatGPT)")
        connection = Connection.objects.create(
            connector_key="chatgpt_plan", label="Mine", enabled_capabilities=["ai_models"]
        )
        self.assertIsNone(connection.connector_class)
        with FLAG:
            self.assertIs(registry.get("chatgpt_plan"), ChatGPTPlanConnector)
            page = self.client.get(reverse("connector_detail", args=["chatgpt_plan"]))
            self.assertContains(page, "Continue with ChatGPT")


@FLAG
class ChatGPTPlanSignInTests(FakeOpenAIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.staff = make_user("paralegal", superuser=False)
        self.client.force_login(self.staff)
        self.client.post(
            reverse("connector_detail", args=["chatgpt_plan"]),
            {"label": "My ChatGPT plan", "capabilities": ["ai_models"], "acknowledge": "on"},
        )
        self.connection = Connection.objects.get()

    def start(self):
        response = self.client.get(reverse("connection_oauth_start", args=[self.connection.pk]))
        query = {k: v[0] for k, v in parse_qs(urlsplit(response["Location"]).query).items()}
        self.openai.nonce = self.client.session["connect_oauth_pending"]["nonce"]
        return response, query

    def paste(self, address):
        return self.client.post(
            reverse("connection_oauth_paste", args=[self.connection.pk]),
            {"address": address},
            follow=True,
        )

    def test_signing_in_by_pasting_the_loopback_address(self):
        self.assertEqual(self.connection.owner, self.staff)
        page = self.client.get(reverse("connection_detail", args=[self.connection.pk]))
        self.assertContains(page, "Continue with ChatGPT")
        self.assertContains(page, 'target="_blank"')
        self.assertContains(page, "Address from the sign-in tab")

        response, query = self.start()
        location = urlsplit(response["Location"])
        self.assertEqual(
            f"{location.scheme}://{location.netloc}{location.path}",
            "https://auth.openai.com/api/accounts/authorize",
        )
        self.assertEqual(query["client_id"], "dynamic_agent_client")
        self.assertEqual(query["agent_name_hint"], "Open Immigration CRM")
        self.assertEqual(query["ext_agent_host_id"], f"urn:uuid:{self.connection.pk}")
        self.assertEqual(query["redirect_uri"], LOOPBACK)
        self.assertIn("chatgpt.tokens.use.direct", query["scope"].split())
        self.assertEqual(query["resource"], "https://api.openai.com/v1")
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertTrue(query["nonce"])

        refused = self.paste("https://evil.example.test/auth/callback?code=x")
        self.assertContains(refused, "It starts with http://127.0.0.1.")
        done = self.paste(
            f"http://127.0.0.1:1455/auth/callback?code=fictional-code&state={query['state']}"
            f"&client_id={CLIENT_ID}&scope=openid"
        )
        self.assertContains(done, "Connected to ChatGPT plan.")
        self.assertContains(done, "Using ChatGPT plan: AI requests you send")
        self.connection.refresh_from_db()
        credentials = self.connection.credentials
        self.assertEqual(
            (credentials["client_id"], credentials["access_token"], credentials["email"]),
            (CLIENT_ID, "access-1", "paralegal@example.test"),
        )
        self.assertEqual(self.connection.status, Connection.Status.ACTIVE)

        # Signing in again uses the issued client ID and the earlier ID token.
        _, again = self.start()
        self.assertEqual(again["client_id"], CLIENT_ID)
        self.assertNotIn("agent_name_hint", again)
        self.assertEqual(again["id_token_hint"], credentials["id_token"])
        mismatch = self.paste(
            f"http://127.0.0.1:1455/auth/callback?code=fictional-code&state={again['state']}"
            "&client_id=oaiapp_other"
        )
        self.assertContains(mismatch, "a different ChatGPT registration")

        # The settings offer the plan's listed models.
        page = self.client.get(reverse("connection_detail", args=[self.connection.pk]))
        self.assertContains(page, "GPT Fictional")
        self.assertNotContains(page, "gpt-hidden")

        # Disconnecting revokes the refresh token.
        self.client.post(reverse("connection_disconnect", args=[self.connection.pk]))
        revoke = self.openai.last("/oauth/revoke")
        self.assertIn("token_type_hint=refresh_token", revoke.content.decode())

    def test_sign_ins_that_cannot_be_trusted(self):
        for change, message in (
            ({"token_key": OTHER_KEY}, "could not be verified"),
            ({"scope": "openid profile email offline_access"}, "plan usage was not allowed"),
        ):
            self.openai.token_key = SIGNING_KEY
            self.openai.scope = FakeOpenAI().scope
            for name, value in change.items():
                setattr(self.openai, name, value)
            _, query = self.start()
            response = self.paste(
                f"http://127.0.0.1:1455/auth/callback?code=fictional-code&state={query['state']}"
                f"&client_id={CLIENT_ID}"
            )
            self.assertContains(response, message)
        _, query = self.start()
        self.openai.nonce = "a-different-nonce"
        response = self.paste(
            f"http://127.0.0.1:1455/auth/callback?code=fictional-code&state={query['state']}"
            f"&client_id={CLIENT_ID}"
        )
        self.assertContains(response, "could not be verified")
        _, query = self.start()
        response = self.paste(
            f"http://127.0.0.1:1455/auth/callback?state={query['state']}&error=access_denied"
        )
        self.assertContains(response, "cancelled or refused")
        _, query = self.start()
        response = self.paste(
            f"http://127.0.0.1:1455/auth/callback?code=x&state={query['state']}"
            "&client_id=dynamic_agent_client"
        )
        self.assertContains(response, "did not return the CRM&#x27;s registration")
        response = self.paste("http://127.0.0.1:1455/auth/callback?code=x&state=stale")
        self.assertContains(response, "expired or did not come from this CRM")


@FLAG
class ChatGPTPlanRequestTests(FakeOpenAIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.staff = make_user("paralegal", superuser=False)
        self.connection = signed_in(
            Connection.objects.create(
                connector_key="chatgpt_plan",
                label="My ChatGPT plan",
                enabled_capabilities=["ai_models"],
                owner=self.staff,
            )
        )
        ai_settings = AISettings.load()
        ai_settings.enabled = True
        ai_settings.features = {"matter_assistant": True}
        ai_settings.save()

    def ask(self, text="Hello", **kwargs):
        return generate(
            feature="matter_assistant",
            messages=[Message("user", text)],
            actor=self.staff,
            connection=self.connection,
            **kwargs,
        )

    def test_requests_follow_the_plan_usage_rules(self):
        call = {
            "type": "function_call",
            "name": "get_matter",
            "namespace": "crm",
            "call_id": "c1",
            "arguments": "{}",
        }
        self.openai.events = [
            {"type": "response.output_text.delta", "delta": "x"},
            completed(output=[call]),
        ]
        reply = self.ask(system="Be brief.", tools=[Tool("get_matter", "A matter.")], max_tokens=99)
        self.assertEqual(reply.tool_calls[0].name, "get_matter")
        self.assertEqual(reply.input_tokens, 12)
        body = json.loads(self.openai.last("/responses").content)
        self.assertEqual(
            (body["store"], body["stream"], body["model"]), (False, True, "gpt-fictional")
        )
        self.assertEqual(body["instructions"], "Be brief.")
        self.assertNotIn("max_output_tokens", body)
        self.assertEqual(body["tools"][0]["type"], "namespace")
        self.assertEqual(body["tools"][0]["tools"][0]["name"], "get_matter")
        self.assertEqual(self.openai.last("/responses").headers["Authorization"], "Bearer access-1")

        self.connection.config = {"model": "gpt-chosen"}
        self.connection.save()
        self.openai.events = [completed('{"summary": "fine"}')]
        reply = self.ask(schema={"type": "object", "properties": {"summary": {"type": "string"}}})
        self.assertEqual(reply.data, {"summary": "fine"})
        body = json.loads(self.openai.last("/responses").content)
        self.assertEqual((body["model"], body["text"]["format"]["strict"]), ("gpt-chosen", True))

    def test_plan_limits_and_refusals(self):
        self.openai.events = [
            {
                "type": "response.failed",
                "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}},
            }
        ]
        with self.assertRaisesMessage(AIUnavailable, "chatgpt.com/settings/usage"):
            self.ask()
        self.openai.api_error = (403, "subscription_sharing_user_not_eligible")
        with self.assertRaisesMessage(AIUnavailable, "cannot use its plan in other apps"):
            self.ask()
        self.openai.api_error = (400, "something_else")
        with self.assertRaisesMessage(
            AIUnavailable, "ChatGPT refused the request (something_else)"
        ):
            self.ask()

    def test_tokens_are_refreshed_once_and_ended_sign_ins_ask_again(self):
        Connection.objects.filter(pk=self.connection.pk).update(
            credentials={**self.connection.credentials, "expires_at": time.time() - 10}
        )
        self.connection.refresh_from_db()
        self.ask()
        self.connection.refresh_from_db()
        self.assertEqual(
            (
                self.connection.credentials["access_token"],
                self.connection.credentials["refresh_token"],
            ),
            ("access-2", "refresh-2"),
        )
        self.assertEqual(self.connection.credentials["client_id"], CLIENT_ID)
        Connection.objects.filter(pk=self.connection.pk).update(
            credentials={**self.connection.credentials, "expires_at": time.time() - 10}
        )
        self.connection.refresh_from_db()
        self.openai.refresh_error = "refresh_token_reused"
        with self.assertRaisesMessage(AIUnavailable, "ChatGPT ended this sign-in. Sign in again."):
            self.ask()
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.status, Connection.Status.NEEDS_REAUTH)

    def test_streams_that_do_not_finish(self):
        with self.assertRaisesMessage(ConnectorError, "cut off"):
            read_stream(
                "data: not json\n\n" + stream({"type": "response.output_text.delta", "delta": "x"})
            )
        with self.assertRaisesMessage(
            ConnectorError, "stopped before finishing (max_output_tokens)"
        ):
            read_stream(
                stream(
                    {
                        "type": "response.incomplete",
                        "response": {"incomplete_details": {"reason": "max_output_tokens"}},
                    }
                )
            )
        with self.assertRaisesMessage(ConnectorError, "could not answer (server_error)"):
            read_stream(stream({"type": "error", "error": {"code": "server_error"}}))

    def test_the_plan_label_shows_where_staff_choose_a_connection(self):
        matter = make_matter(make_contact(self.staff), self.staff)
        self.client.force_login(self.staff)
        page = self.client.get(reverse("matter_detail", args=[matter.pk]))
        self.assertContains(page, "Using ChatGPT plan")
        self.assertContains(page, 'href="https://chatgpt.com/settings/usage"')
        # A personal plan is never offered for background work.
        admin = make_user()
        self.client.force_login(admin)
        settings_page = self.client.get(reverse("ai_settings"))
        self.assertNotContains(settings_page, "My ChatGPT plan</option>")
