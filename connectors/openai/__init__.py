"""OpenAI: models through the firm's own OpenAI API key, with nothing stored at OpenAI.

Requests use the Responses API with ``store: false``, so OpenAI keeps no
application state for later retrieval. In a tool loop every output item,
including reasoning, is sent back as it came, because nothing is stored.
Structured output uses ``text.format`` with a strict JSON schema.

API reference: https://developers.openai.com/api/reference/resources/responses
"""

import json

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
from crm.connect.http import ProviderClient
from crm.connect.registry import register

API_URL = "https://api.openai.com/v1"


def strict_schema(schema):
    """Strict mode needs additionalProperties false and every property listed as required."""
    if isinstance(schema, dict):
        fixed = {key: strict_schema(value) for key, value in schema.items()}
        if fixed.get("type") == "object":
            fixed["additionalProperties"] = False
            fixed["required"] = list((fixed.get("properties") or {}).keys())
        return fixed
    if isinstance(schema, list):
        return [strict_schema(item) for item in schema]
    return schema


def to_items(message):
    """A core message as Responses API input items."""
    if message["role"] == "tool":
        return [
            {
                "type": "function_call_output",
                "call_id": message["tool_call_id"],
                "output": message["content"],
            }
        ]
    if message["role"] == "assistant" and message.get("raw"):
        return list(message["raw"])
    items = []
    if message["content"] or message["role"] != "assistant":
        items.append({"role": message["role"], "content": message["content"]})
    for call in message.get("tool_calls") or []:
        items.append(
            {
                "type": "function_call",
                "call_id": call["id"],
                "name": call["name"],
                "arguments": json.dumps(call["arguments"]),
            }
        )
    return items


@register
class OpenAIConnector(Connector):
    key = "openai"
    name = "OpenAI"
    description = (
        "Power the CRM's AI features with OpenAI models through the firm's own API key, with "
        "nothing stored at OpenAI for later retrieval."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.AI_MODELS})
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "api_key",
                "API key",
                help_text="From the OpenAI platform's API keys page, for the firm's project.",
            ),
        ),
        docs_url="https://developers.openai.com/api/reference/overview",
    )
    settings_fields = (
        SettingField(
            "model",
            "Model",
            help_text="An OpenAI model ID from the firm's model list.",
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "What each AI feature turned on in AI settings sends, with storing for later "
            "retrieval turned off",
        ),
        receives=("The model's answers",),
    )
    outbound_actions = {"generate": Capability.AI_MODELS}

    def __init__(self, connection):
        super().__init__(connection)
        self.client = ProviderClient(
            API_URL,
            name="OpenAI",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.credentials.get('api_key', '')}",
            },
            timeout=300,
        )

    def call(self, method, path, **kwargs):
        try:
            return self.client.json(method, path, **kwargs)
        except ConnectorError as exc:
            if exc.status == 401:
                raise ConnectorError("OpenAI did not accept the API key.", status=401) from None
            if exc.status == 400:
                raise ConnectorError(
                    "OpenAI could not use the request. Check the model on the connection.",
                    status=400,
                ) from None
            raise

    def test_connection(self):
        self.call("GET", "/models")

    def perform(self, action, payload):
        if action != "generate":
            raise ConnectorError(f"OpenAI cannot {action}.")
        model = payload.get("model") or self.setting("model")
        if not model:
            raise ConnectorError("Enter the model on the connection's page.")
        body = {
            "model": model,
            "input": [item for message in payload["messages"] for item in to_items(message)],
            "max_output_tokens": max(16, int(payload.get("max_tokens") or 2048)),
            "store": False,
        }
        if payload.get("system"):
            body["instructions"] = payload["system"]
        if payload.get("tools"):
            body["tools"] = [
                {
                    "type": "function",
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                    # Tools have optional arguments, which strict validation refuses.
                    "strict": False,
                }
                for t in payload["tools"]
            ]
        if payload.get("schema"):
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": payload.get("schema_name") or "answer",
                    "schema": strict_schema(payload["schema"]),
                    "strict": True,
                }
            }
        answer = self.call("POST", "/responses", json=body)
        output = answer.get("output") or []
        text = "".join(
            part.get("text", "")
            for item in output
            if item.get("type") == "message"
            for part in item.get("content") or []
            if part.get("type") == "output_text"
        )
        calls = []
        for item in output:
            if item.get("type") == "function_call":
                try:
                    arguments = json.loads(item.get("arguments") or "{}")
                except ValueError:
                    arguments = {}
                calls.append(
                    {
                        "id": item.get("call_id", ""),
                        "name": item.get("name", ""),
                        "arguments": arguments,
                    }
                )
        data = None
        if payload.get("schema") and answer.get("status") == "completed" and not calls:
            try:
                data = json.loads(text)
            except ValueError:
                raise ConnectorError(
                    "OpenAI's answer did not follow the requested format."
                ) from None
        usage = answer.get("usage") or {}
        return {
            "id": answer.get("id", ""),
            "model": answer.get("model") or model,
            "text": text,
            "tool_calls": calls,
            "data": data,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "stop_reason": "tool_use" if calls else answer.get("status", ""),
            "raw": output,
        }
