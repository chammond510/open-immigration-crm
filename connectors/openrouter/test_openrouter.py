import json
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx
from django.test import TestCase
from django.urls import reverse

from crm.ai.client import Message, generate
from crm.ai.features import CONNECTION_TEST
from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection
from crm.connect.testing import ConnectorContract
from crm.tests.factories import make_user

from . import OpenRouterConnector

KEY = "sk-or-v1-fictional"
MODEL = "fictional/model-1"


class FakeOpenRouter:
    def __init__(self):
        self.requests = []
        self.content = "ready"

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/api/v1/auth/keys":
            body = json.loads(request.content)
            if body.get("code") != "fictional-code" or not body.get("code_verifier"):
                return httpx.Response(403, json={"error": {"code": 403, "message": "Invalid code"}})
            return httpx.Response(200, json={"key": KEY, "user_id": "user_fictional"})
        if request.headers.get("Authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"error": {"code": 401, "message": "No auth"}})
        if path == "/api/v1/key":
            return httpx.Response(200, json={"data": {"label": "fictional"}})
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "gen-fictional",
                "model": body["model"],
                "choices": [
                    {
                        "message": {"role": "assistant", "content": self.content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 1},
            },
        )

    def body(self):
        return json.loads(self.requests[-1].content)


class FakeOpenRouterMixin:
    def setUp(self):
        super().setUp()
        self.openrouter = FakeOpenRouter()
        transport = http.use_transport(httpx.MockTransport(lambda r: self.openrouter(r)))
        transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)
        patcher = mock.patch.object(http, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)


class OpenRouterContractTests(FakeOpenRouterMixin, ConnectorContract, TestCase):
    connector_class = OpenRouterConnector
    valid_credentials = {"api_key": KEY}
    invalid_credentials = {"api_key": "sk-or-v1-revoked"}
    connection_config = {"model": MODEL, "privacy": "deny"}
    outbound_examples = {
        "generate": {
            "messages": [{"role": "user", "content": "Hello", "tool_calls": [], "tool_call_id": ""}]
        }
    }


class OpenRouterTests(FakeOpenRouterMixin, TestCase):
    def test_signing_in_gets_a_key_and_requests_keep_data_private(self):
        admin = make_user()
        self.client.force_login(admin)
        page = self.client.get(reverse("connector_detail", args=["openrouter"]))
        self.assertNotContains(page, "register the firm")
        self.assertContains(page, "Continue to sign-in")
        self.client.post(
            reverse("connector_detail", args=["openrouter"]),
            {
                "label": "OpenRouter",
                "capabilities": ["ai_models"],
                "setting_model": MODEL,
                "setting_privacy": "zdr",
                "acknowledge": "on",
            },
        )
        connection = Connection.objects.get()
        start = self.client.get(reverse("connection_oauth_start", args=[connection.pk]))
        location = urlsplit(start["Location"])
        self.assertEqual(f"{location.netloc}{location.path}", "openrouter.ai/auth")
        query = {k: v[0] for k, v in parse_qs(location.query).items()}
        self.assertEqual(query["callback_url"], "http://testserver/connections/oauth/callback/")
        self.assertEqual(query["code_challenge_method"], "S256")
        self.client.get(
            reverse("connection_oauth_callback"),
            {"code": "fictional-code", "state": query["state"]},
        )
        exchange = json.loads(self.openrouter.requests[0].content)
        self.assertEqual(exchange["code_challenge_method"], "S256")
        connection.refresh_from_db()
        self.assertEqual(connection.credentials, {"api_key": KEY})
        self.assertEqual(connection.status, Connection.Status.ACTIVE)

        Connection.objects.filter(pk=connection.pk).update(preview_mode=False)
        connection.refresh_from_db()
        self.openrouter.content = "{}"
        reply = generate(
            feature=CONNECTION_TEST,
            messages=[Message("user", "Reply with ready.")],
            schema={"type": "object", "properties": {}},
            actor=admin,
            connection=connection,
        )
        body = self.openrouter.body()
        self.assertEqual(
            body["provider"], {"data_collection": "deny", "zdr": True, "require_parameters": True}
        )
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertEqual(
            self.openrouter.requests[-1].headers["X-OpenRouter-Title"], "Open Immigration CRM"
        )
        self.assertEqual(reply.data, {})

    def test_a_bad_code_is_refused(self):
        connection = Connection.objects.create(
            connector_key="openrouter", label="OpenRouter", enabled_capabilities=["ai_models"]
        )
        with self.assertRaises(ConnectorError):
            OpenRouterConnector(connection).exchange_code(
                "wrong", "verifier", "https://crm.example.test/cb/"
            )
