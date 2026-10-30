import json
from unittest import mock

import httpx
from django.core.exceptions import ValidationError
from django.test import TestCase

from crm.ai.client import Message, Tool, generate
from crm.ai.features import CONNECTION_TEST
from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection
from crm.connect.services import acknowledge_data_flows
from crm.connect.testing import ConnectorContract
from crm.tests.factories import make_user

from . import OpenAICompatibleConnector, validate_base_url

LOCAL = "http://localhost:11434/v1"


class FakeLocalModel:
    """Answers like a local model server's OpenAI-compatible API; it needs no key."""

    def __init__(self):
        self.requests = []
        self.message = {"role": "assistant", "content": "ready"}
        self.finish = "stop"

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": "fictional-local"}]})
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-fictional",
                "model": body["model"],
                "choices": [{"message": self.message, "finish_reason": self.finish}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 1},
            },
        )

    def body(self):
        return json.loads(self.requests[-1].content)


class FakeLocalMixin:
    def setUp(self):
        super().setUp()
        self.local = FakeLocalModel()
        transport = http.use_transport(httpx.MockTransport(lambda r: self.local(r)))
        transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)
        patcher = mock.patch.object(http, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)


class CompatibleContractTests(FakeLocalMixin, ConnectorContract, TestCase):
    connector_class = OpenAICompatibleConnector
    valid_credentials = {}
    invalid_credentials = {}
    connection_config = {"base_url": LOCAL, "model": "fictional-local"}
    outbound_examples = {
        "generate": {
            "messages": [{"role": "user", "content": "Hello", "tool_calls": [], "tool_call_id": ""}]
        }
    }

    def test_contract_invalid_credentials_fail_cleanly(self):
        connection = self.make_connection()
        connection.config = {"base_url": "http://models.example.test/v1", "model": "x"}
        with self.assertRaises(ConnectorError):
            OpenAICompatibleConnector(connection).test_connection()


class CompatibleTests(FakeLocalMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user()
        self.connection = Connection.objects.create(
            connector_key="openai_compatible",
            label="Office model",
            enabled_capabilities=["ai_models"],
            credentials={},
            config={"base_url": LOCAL, "model": "fictional-local"},
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

    def test_addresses(self):
        for good in (
            LOCAL,
            "http://192.168.1.20:1234/v1",
            "http://modelbox:8000/v1",
            "https://api.example.test/v1",
        ):
            validate_base_url(good)
        for bad in ("http://models.example.test/v1", "ftp://localhost/v1", "localhost:11434"):
            with self.assertRaises(ValidationError, msg=bad):
                validate_base_url(bad)

    def test_a_local_model_with_no_key(self):
        reply = self.ask([Message("user", "Reply with ready.")], system="Be brief.")
        request = self.local.requests[-1]
        self.assertEqual(str(request.url), f"{LOCAL}/chat/completions")
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(
            self.local.body()["messages"],
            [
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "Reply with ready."},
            ],
        )
        self.assertEqual((reply.text, reply.input_tokens), ("ready", 7))

    def test_tools_and_formats(self):
        self.local.message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "find_matter", "arguments": '{"q": "x"}'},
                }
            ],
        }
        self.local.finish = "tool_calls"
        tools = [Tool("find_matter", "Find a matter.")]
        reply = self.ask([Message("user", "Find it.")], tools=tools)
        self.assertEqual(reply.tool_calls[0].arguments, {"q": "x"})
        self.local.message, self.local.finish = {"role": "assistant", "content": '{"a": 1}'}, "stop"
        self.ask(
            [
                Message("user", "Find it."),
                reply.as_message(),
                Message("tool", "M-1", tool_call_id="call_1"),
            ],
            tools=tools,
            schema={"type": "object", "properties": {"a": {"type": "integer"}}},
        )
        body = self.local.body()
        self.assertEqual(body["messages"][1]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(
            body["messages"][2], {"role": "tool", "tool_call_id": "call_1", "content": "M-1"}
        )
        self.assertEqual(body["response_format"]["json_schema"]["strict"], True)
        self.local.message = {"role": "assistant", "content": "not json"}
        with self.assertRaisesMessage(ConnectorError, "did not follow the requested format"):
            OpenAICompatibleConnector(self.connection).perform(
                "generate", {"messages": [], "schema": {"type": "object"}}
            )

    def test_a_key_is_sent_when_given_and_a_model_is_needed(self):
        self.connection.credentials = {"api_key": "fictional-key"}
        OpenAICompatibleConnector(self.connection).test_connection()
        self.assertEqual(self.local.requests[-1].headers["Authorization"], "Bearer fictional-key")
        self.connection.config = {"base_url": LOCAL, "model": ""}
        with self.assertRaisesMessage(ConnectorError, "Enter the model"):
            OpenAICompatibleConnector(self.connection).perform("generate", {"messages": []})
