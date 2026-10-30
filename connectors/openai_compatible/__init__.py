"""Any server that speaks the OpenAI-compatible Chat Completions API.

This covers models the firm runs on its own computers, such as Ollama, LM Studio,
or vLLM, and hosted services with the same API. Plain HTTP is allowed only to a
server on the firm's own network or this computer; anything else must be HTTPS.

The translation helpers here are shared with other connectors whose API is the
same, such as OpenRouter.
"""

import ipaddress
import json
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError

from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
    SettingField,
)
from crm.connect.http import ProviderClient, is_local
from crm.connect.registry import register


def validate_base_url(value):
    parts = urlsplit(value)
    host = parts.hostname or ""
    if parts.scheme == "https" and host:
        return
    if parts.scheme == "http" and (is_local(host) or local_name(host)):
        return
    raise ValidationError(
        "Enter the server's address, such as http://localhost:11434/v1 for Ollama. Addresses "
        "outside the firm's own network must use https://."
    )


def local_name(host):
    """Hostnames with no dot (a machine on the firm's network) count as local."""
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return bool(host) and "." not in host


def to_chat(message):
    """A core message as a Chat Completions message."""
    if message["role"] == "tool":
        return {
            "role": "tool",
            "tool_call_id": message["tool_call_id"],
            "content": message["content"],
        }
    if message["role"] == "assistant" and message.get("raw"):
        return message["raw"]
    if message["role"] == "assistant" and message.get("tool_calls"):
        return {
            "role": "assistant",
            "content": message["content"] or None,
            "tool_calls": [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])},
                }
                for call in message["tool_calls"]
            ],
        }
    return {"role": message["role"], "content": message["content"]}


def chat_body(payload, model):
    messages = [{"role": "system", "content": payload["system"]}] if payload.get("system") else []
    messages += [to_chat(m) for m in payload["messages"]]
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": int(payload.get("max_tokens") or 2048),
    }
    if payload.get("tools"):
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in payload["tools"]
        ]
    if payload.get("schema"):
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": payload.get("schema_name") or "answer",
                "schema": payload["schema"],
                "strict": True,
            },
        }
    return body


def chat_reply(answer, model, *, schema):
    """A Chat Completions answer in the core's reply shape."""
    choice = (answer.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except ValueError:
            arguments = {}
        calls.append(
            {"id": call.get("id", ""), "name": function.get("name", ""), "arguments": arguments}
        )
    text = message.get("content") or ""
    data = None
    if schema and choice.get("finish_reason") == "stop":
        try:
            data = json.loads(text)
        except ValueError:
            raise ConnectorError(
                "The model's answer did not follow the requested format."
            ) from None
    usage = answer.get("usage") or {}
    return {
        "id": answer.get("id", ""),
        "model": answer.get("model") or model,
        "text": text,
        "tool_calls": calls,
        "data": data,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "stop_reason": choice.get("finish_reason", ""),
        "raw": {k: v for k, v in message.items() if k in ("role", "content", "tool_calls")},
    }


@register
class OpenAICompatibleConnector(Connector):
    key = "openai_compatible"
    name = "OpenAI-compatible endpoint (including local models)"
    description = (
        "Power the CRM's AI features with a model the firm runs itself, such as through Ollama "
        "or LM Studio, or any service with an OpenAI-compatible API."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.AI_MODELS})
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "api_key",
                "API key",
                required=False,
                help_text="Leave blank for a local server that needs none, such as Ollama.",
            ),
        ),
        label="Address and model",
    )
    settings_fields = (
        SettingField(
            "base_url",
            "Server address",
            default="http://localhost:11434/v1",
            help_text="Ends in /v1. For Ollama on this computer: http://localhost:11434/v1.",
            validate=validate_base_url,
        ),
        SettingField(
            "model", "Model", help_text="The model's name on that server, such as llama3.3."
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "What each AI feature turned on in AI settings sends, to the server at the address "
            "given, which may be on the firm's own computers",
        ),
        receives=("The model's answers",),
    )
    outbound_actions = {"generate": Capability.AI_MODELS}

    def __init__(self, connection):
        super().__init__(connection)
        headers = {"Accept": "application/json"}
        if self.credentials.get("api_key"):
            headers["Authorization"] = f"Bearer {self.credentials['api_key']}"
        self.client = ProviderClient(
            self.setting("base_url"),
            name="The AI server",
            headers=headers,
            allow_local_http=True,
            timeout=300,
        )

    def call(self, method, path, **kwargs):
        validate_base_url(self.setting("base_url"))
        try:
            return self.client.json(method, path, **kwargs)
        except ConnectorError as exc:
            if exc.status == 401:
                raise ConnectorError(
                    "The AI server did not accept the API key.", status=401
                ) from None
            raise

    def test_connection(self):
        try:
            self.call("GET", "/models")
        except ValidationError as exc:
            raise ConnectorError(" ".join(exc.messages)) from None

    def perform(self, action, payload):
        if action != "generate":
            raise ConnectorError(f"The AI server cannot {action}.")
        model = payload.get("model") or self.setting("model")
        if not model:
            raise ConnectorError("Enter the model's name on the connection's page.")
        answer = self.call("POST", "/chat/completions", json=chat_body(payload, model))
        return chat_reply(answer, model, schema=payload.get("schema"))
