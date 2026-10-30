"""Anthropic: Claude models through the firm's own Claude Console API key.

Only Console API keys are accepted. Claude.ai subscription sign-ins are refused:
Anthropic does not permit apps to use or store Claude.ai credentials, and this
CRM never asks for them.

Requests use the Messages API with API version 2026-10-21, which takes no
sampling parameters such as temperature. Structured output uses
``output_config.format`` with a JSON schema. The newest models think adaptively
and, in a tool loop, need their content blocks back exactly, so each reply's
content is returned as ``raw`` and sent back unchanged.

API reference: https://platform.claude.com/docs/en/api/overview
"""

import json

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
from crm.connect.http import ProviderClient
from crm.connect.registry import register

API_URL = "https://api.anthropic.com/v1"
API_VERSION = "2026-10-21"
DEFAULT_MODEL = "claude-opus-5-5"


def check_key(key):
    if not key.startswith("sk-ant-api"):
        raise ConnectorError(
            "Use an API key from the Claude Console; it starts with sk-ant-api. Claude.ai "
            "subscription sign-ins cannot be used."
        )


def validate_workspace(value):
    if not value.replace("_", "").replace("-", "").isalnum() or len(value) > 100:
        raise ValidationError("Enter the workspace ID from the Claude Console.")


def schema_for_claude(schema):
    """Structured output needs additionalProperties false on every object."""
    if isinstance(schema, dict):
        fixed = {key: schema_for_claude(value) for key, value in schema.items()}
        if fixed.get("type") == "object":
            fixed.setdefault("additionalProperties", False)
        return fixed
    if isinstance(schema, list):
        return [schema_for_claude(item) for item in schema]
    return schema


def to_claude(message):
    """A core message as a Messages API message."""
    if message["role"] == "tool":
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": message["tool_call_id"],
                    "content": message["content"],
                }
            ],
        }
    if message["role"] == "assistant" and message.get("raw"):
        return {"role": "assistant", "content": message["raw"]}
    if message["role"] == "assistant" and message.get("tool_calls"):
        blocks = [{"type": "text", "text": message["content"]}] if message["content"] else []
        blocks += [
            {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
            for c in message["tool_calls"]
        ]
        return {"role": "assistant", "content": blocks}
    return {"role": message["role"], "content": message["content"]}


def merge_turns(messages):
    """Tool results answering one turn go together in a single user message."""
    merged = []
    for message in messages:
        if (
            merged
            and message["role"] == "user"
            and merged[-1]["role"] == "user"
            and isinstance(message["content"], list)
            and isinstance(merged[-1]["content"], list)
        ):
            merged[-1]["content"] += message["content"]
        else:
            merged.append(message)
    return merged


@register
class AnthropicConnector(Connector):
    key = "anthropic"
    name = "Anthropic"
    description = (
        "Power the CRM's AI features with Claude, through the firm's own Claude Console API key."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.AI_MODELS})
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "api_key",
                "Claude Console API key",
                help_text="Starts with sk-ant-api. Create it in the Claude Console under API "
                "keys. Claude.ai subscription sign-ins cannot be used.",
            ),
        ),
        docs_url="https://platform.claude.com/docs/en/manage-claude/authentication",
        label="API key",
    )
    settings_fields = (
        SettingField(
            "model",
            "Model",
            default=DEFAULT_MODEL,
            help_text="A Claude model ID, such as claude-opus-5-5 or claude-sonnet-5-5.",
        ),
        SettingField(
            "workspace_id",
            "Workspace ID",
            required=False,
            help_text="Only for a key that is not tied to one workspace.",
            validate=validate_workspace,
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "What each AI feature turned on in AI settings sends, such as the contact and "
            "matter details a feature works from",
        ),
        receives=("The model's answers",),
    )
    outbound_actions = {"generate": Capability.AI_MODELS}

    def __init__(self, connection):
        super().__init__(connection)
        headers = {
            "Accept": "application/json",
            "anthropic-version": API_VERSION,
            "x-api-key": self.credentials.get("api_key", ""),
        }
        if self.setting("workspace_id"):
            headers["anthropic-workspace-id"] = self.setting("workspace_id")
        self.client = ProviderClient(API_URL, name="Anthropic", headers=headers, timeout=120)

    def call(self, method, path, **kwargs):
        check_key(self.credentials.get("api_key", ""))
        try:
            return self.client.json(method, path, **kwargs)
        except ConnectorError as exc:
            if exc.status == 401:
                raise ConnectorError("Anthropic did not accept the API key.", status=401) from None
            if exc.status == 403:
                raise ConnectorError(
                    "Anthropic refused the request. Check the key's workspace and permissions.",
                    status=403,
                ) from None
            if exc.status == 400:
                raise ConnectorError(
                    "Anthropic could not use the request. Check the model on the connection.",
                    status=400,
                ) from None
            raise

    def test_connection(self):
        self.call("GET", "/models", params={"limit": 1})

    def perform(self, action, payload):
        if action != "generate":
            raise ConnectorError(f"Anthropic cannot {action}.")
        model = payload.get("model") or self.setting("model") or DEFAULT_MODEL
        body = {
            "model": model,
            "max_tokens": int(payload.get("max_tokens") or 2048),
            "messages": merge_turns([to_claude(m) for m in payload["messages"]]),
        }
        if payload.get("system"):
            body["system"] = payload["system"]
        if payload.get("tools"):
            body["tools"] = [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["parameters"],
                }
                for t in payload["tools"]
            ]
        if payload.get("schema"):
            body["output_config"] = {
                "format": {"type": "json_schema", "schema": schema_for_claude(payload["schema"])}
            }
        answer = self.call("POST", "/messages", json=body)
        blocks = answer.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        data = None
        if payload.get("schema") and answer.get("stop_reason") == "end_turn":
            try:
                data = json.loads(text)
            except ValueError:
                raise ConnectorError(
                    "Claude's answer did not follow the requested format."
                ) from None
        usage = answer.get("usage") or {}
        return {
            "id": answer.get("id", ""),
            "model": answer.get("model", model),
            "text": text,
            "tool_calls": [
                {"id": b["id"], "name": b["name"], "arguments": b.get("input") or {}}
                for b in blocks
                if b.get("type") == "tool_use"
            ],
            "data": data,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "stop_reason": answer.get("stop_reason", ""),
            "raw": blocks,
        }
