import json
from unittest import mock

import httpx
from django.test import TestCase

from crm.ai.client import Message, Tool, generate
from crm.ai.features import CONNECTION_TEST
from crm.ai.models import AIRequest
from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection
from crm.connect.services import acknowledge_data_flows
from crm.connect.testing import ConnectorContract
from crm.tests.factories import make_user

from . import API_VERSION, DEFAULT_MODEL, AnthropicConnector

KEY = "sk-ant-api03-fictional-key"


class FakeClaude:
    def __init__(self):
        self.requests = []
        self.answer = None

    def __call__(self, request):
        self.requests.append(request)
        if request.headers.get("x-api-key") != KEY:
            return httpx.Response(
                401, json={"type": "error", "error": {"type": "authentication_error"}}
            )
        assert request.headers["anthropic-version"] == API_VERSION
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": DEFAULT_MODEL}], "has_more": False})
        body = json.loads(request.content)
        assert "temperature" not in body
        answer = self.answer or {
            "content": [
                {"type": "thinking", "thinking": "", "signature": "fictional"},
                {"type": "text", "text": "ready"},
            ],
            "stop_reason": "end_turn",
        }
        return httpx.Response(
            200,
            json={
                "id": "msg_fictional",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "usage": {"input_tokens": 12, "output_tokens": 3},
                **answer,
            },
        )

    def body(self):
        return json.loads(self.requests[-1].content)


class FakeClaudeMixin:
    def setUp(self):
        super().setUp()
        self.claude = FakeClaude()
        transport = http.use_transport(httpx.MockTransport(lambda r: self.claude(r)))
        transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)
        patcher = mock.patch.object(http, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)


class AnthropicContractTests(FakeClaudeMixin, ConnectorContract, TestCase):
    connector_class = AnthropicConnector
    valid_credentials = {"api_key": KEY}
    invalid_credentials = {"api_key": "sk-ant-api03-revoked"}
    outbound_examples = {
        "generate": {
            "messages": [{"role": "user", "content": "Hello", "tool_calls": [], "tool_call_id": ""}]
        }
    }


class AnthropicTests(FakeClaudeMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user()
        self.connection = Connection.objects.create(
            connector_key="anthropic",
            label="Claude",
            enabled_capabilities=["ai_models"],
            credentials={"api_key": KEY},
            config={"model": DEFAULT_MODEL, "workspace_id": ""},
            preview_mode=False,
        )
        acknowledge_data_flows(self.connection, self.admin)

    def connector(self):
        return AnthropicConnector(self.connection)

    def test_subscription_sign_ins_are_refused(self):
        for credential in ("sk-ant-oat01-fictional", "sk-ant-admin01-fictional", "fictional"):
            self.connection.credentials = {"api_key": credential}
            with self.assertRaisesMessage(ConnectorError, "Claude.ai subscription sign-ins"):
                self.connector().test_connection()
        self.assertEqual(self.claude.requests, [])

    def test_a_request_through_the_ai_interface(self):
        reply = generate(
            feature=CONNECTION_TEST,
            messages=[Message("user", "Reply with ready.")],
            system="Be brief.",
            actor=self.admin,
            connection=self.connection,
            max_tokens=50,
        )
        self.assertEqual(
            (reply.text, reply.model, reply.input_tokens), ("ready", DEFAULT_MODEL, 12)
        )
        body = self.claude.body()
        self.assertEqual(
            body,
            {
                "model": DEFAULT_MODEL,
                "max_tokens": 50,
                "messages": [{"role": "user", "content": "Reply with ready."}],
                "system": "Be brief.",
            },
        )
        self.assertEqual(AIRequest.objects.get().model, DEFAULT_MODEL)

    def test_tools_round_trip_with_thinking_kept(self):
        self.claude.answer = {
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig-1"},
                {"type": "tool_use", "id": "toolu_1", "name": "find_matter", "input": {"q": "x"}},
            ],
            "stop_reason": "tool_use",
        }
        tools = [
            Tool(
                "find_matter",
                "Find a matter.",
                {"type": "object", "properties": {"q": {"type": "string"}}},
            )
        ]
        reply = generate(
            feature=CONNECTION_TEST,
            messages=[Message("user", "Find it.")],
            tools=tools,
            actor=self.admin,
            connection=self.connection,
        )
        self.assertEqual(reply.tool_calls[0].arguments, {"q": "x"})
        self.assertEqual(self.claude.body()["tools"][0]["input_schema"]["type"], "object")
        self.claude.answer = None
        generate(
            feature=CONNECTION_TEST,
            messages=[
                Message("user", "Find it."),
                reply.as_message(),
                Message("tool", "Matter M-1 (fictional)", tool_call_id="toolu_1"),
            ],
            tools=tools,
            actor=self.admin,
            connection=self.connection,
        )
        sent = self.claude.body()["messages"]
        self.assertEqual(sent[1], {"role": "assistant", "content": self.claude.answer or reply.raw})
        self.assertEqual(sent[1]["content"][0]["signature"], "sig-1")
        self.assertEqual(
            sent[2],
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "Matter M-1 (fictional)",
                    }
                ],
            },
        )

    def test_structured_output(self):
        self.claude.answer = {
            "content": [{"type": "text", "text": '{"summary": "Fictional"}'}],
            "stop_reason": "end_turn",
        }
        schema = {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        }
        reply = generate(
            feature=CONNECTION_TEST,
            messages=[Message("user", "Summarize.")],
            schema=schema,
            actor=self.admin,
            connection=self.connection,
        )
        self.assertEqual(reply.data, {"summary": "Fictional"})
        sent = self.claude.body()["output_config"]["format"]
        self.assertEqual(sent["type"], "json_schema")
        self.assertIs(sent["schema"]["additionalProperties"], False)
        self.claude.answer = {
            "content": [{"type": "text", "text": "not json"}],
            "stop_reason": "end_turn",
        }
        with self.assertRaisesMessage(ConnectorError, "did not follow the requested format"):
            self.connector().perform("generate", {"messages": [], "schema": schema})

    def test_workspace_header_and_errors(self):
        self.connection.config = {"model": DEFAULT_MODEL, "workspace_id": "wrkspc_fictional"}
        self.connector().test_connection()
        self.assertEqual(
            self.claude.requests[-1].headers["anthropic-workspace-id"], "wrkspc_fictional"
        )
        self.connection.credentials = {"api_key": "sk-ant-api03-other"}
        with self.assertRaisesMessage(ConnectorError, "did not accept the API key"):
            self.connector().test_connection()
