import json
from unittest import mock

import httpx
from django.test import TestCase

from crm.ai.client import Message, Tool, generate
from crm.ai.features import CONNECTION_TEST
from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection
from crm.connect.services import acknowledge_data_flows
from crm.connect.testing import ConnectorContract
from crm.tests.factories import make_user

from . import OpenAIConnector, strict_schema

KEY = "sk-fictional-openai-key"
MODEL = "fictional-model-1"


class FakeOpenAI:
    def __init__(self):
        self.requests = []
        self.output = None

    def __call__(self, request):
        self.requests.append(request)
        if request.headers.get("Authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"error": {"type": "invalid_request_error"}})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": MODEL}]})
        body = json.loads(request.content)
        output = self.output or [
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "fictional"},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "ready"}],
            },
        ]
        return httpx.Response(
            200,
            json={
                "id": "resp_fictional",
                "status": "completed",
                "model": body["model"],
                "output": output,
                "usage": {"input_tokens": 9, "output_tokens": 2},
            },
        )

    def body(self):
        return json.loads(self.requests[-1].content)


class FakeOpenAIMixin:
    def setUp(self):
        super().setUp()
        self.openai = FakeOpenAI()
        transport = http.use_transport(httpx.MockTransport(lambda r: self.openai(r)))
        transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)
        patcher = mock.patch.object(http, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)


class OpenAIContractTests(FakeOpenAIMixin, ConnectorContract, TestCase):
    connector_class = OpenAIConnector
    valid_credentials = {"api_key": KEY}
    invalid_credentials = {"api_key": "sk-revoked"}
    connection_config = {"model": MODEL}
    outbound_examples = {
        "generate": {
            "messages": [{"role": "user", "content": "Hello", "tool_calls": [], "tool_call_id": ""}]
        }
    }


class OpenAITests(FakeOpenAIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user()
        self.connection = Connection.objects.create(
            connector_key="openai",
            label="OpenAI",
            enabled_capabilities=["ai_models"],
            credentials={"api_key": KEY},
            config={"model": MODEL},
            preview_mode=False,
        )
        acknowledge_data_flows(self.connection, self.admin)

    def ask(self, messages, **kwargs):
        return generate(
            feature=CONNECTION_TEST,
            messages=messages,
            actor=self.admin,
            connection=self.connection,
            **kwargs,
        )

    def test_nothing_is_stored_and_text_comes_from_output(self):
        reply = self.ask([Message("user", "Reply with ready.")], system="Be brief.", max_tokens=5)
        body = self.openai.body()
        self.assertIs(body["store"], False)
        self.assertEqual(body["instructions"], "Be brief.")
        self.assertEqual(body["max_output_tokens"], 16)
        self.assertEqual(body["input"], [{"role": "user", "content": "Reply with ready."}])
        self.assertEqual((reply.text, reply.model, reply.output_tokens), ("ready", MODEL, 2))

    def test_tool_loop_sends_every_output_item_back(self):
        self.openai.output = [
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "fictional"},
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "find_matter",
                "arguments": '{"q": "x"}',
            },
        ]
        tools = [Tool("find_matter", "Find a matter.")]
        reply = self.ask([Message("user", "Find it.")], tools=tools)
        self.assertEqual(reply.tool_calls[0].arguments, {"q": "x"})
        self.assertEqual(self.openai.body()["tools"][0]["type"], "function")
        self.assertFalse(self.openai.body()["tools"][0]["strict"])
        self.openai.output = None
        self.ask(
            [
                Message("user", "Find it."),
                reply.as_message(),
                Message("tool", "Matter M-1 (fictional)", tool_call_id="call_1"),
            ],
            tools=tools,
        )
        sent = self.openai.body()["input"]
        self.assertEqual(sent[1]["type"], "reasoning")
        self.assertEqual(sent[2]["type"], "function_call")
        self.assertEqual(
            sent[3],
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "Matter M-1 (fictional)",
            },
        )

    def test_structured_output_is_strict(self):
        self.openai.output = [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": '{"summary": "Fictional"}'}],
            }
        ]
        schema = {
            "type": "object",
            "properties": {"summary": {"type": "string"}, "note": {"type": "string"}},
        }
        reply = self.ask([Message("user", "Summarize.")], schema=schema, schema_name="summary")
        self.assertEqual(reply.data, {"summary": "Fictional"})
        sent = self.openai.body()["text"]["format"]
        self.assertEqual((sent["name"], sent["strict"]), ("summary", True))
        self.assertEqual(sent["schema"]["required"], ["summary", "note"])
        self.assertEqual(strict_schema({"type": "string"}), {"type": "string"})
        self.openai.output = [
            {"type": "message", "content": [{"type": "output_text", "text": "nope"}]}
        ]
        with self.assertRaisesMessage(ConnectorError, "did not follow the requested format"):
            OpenAIConnector(self.connection).perform("generate", {"messages": [], "schema": schema})

    def test_errors(self):
        self.connection.config = {"model": ""}
        with self.assertRaisesMessage(ConnectorError, "Enter the model"):
            OpenAIConnector(self.connection).perform("generate", {"messages": []})
        with self.assertRaisesMessage(ConnectorError, "cannot draw"):
            OpenAIConnector(self.connection).perform("draw", {})
