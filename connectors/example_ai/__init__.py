"""A fictional AI model for testing the AI interface without any network access.

It is hidden from the catalog. It answers with the last user message reversed,
and fills a requested schema with fictional values. Asked to "use a tool", it calls
the first tool it is offered; asked to "use tool NAME", optionally followed by JSON
arguments, it calls that tool. After a tool answers, it says so and stops.
"""

import json
import re

from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
)
from crm.connect.registry import register


def fill(schema):
    """Fictional values that fit a simple JSON schema."""
    kind = schema.get("type")
    if kind == "object":
        return {name: fill(part) for name, part in (schema.get("properties") or {}).items()}
    if kind == "array":
        return [fill(schema.get("items") or {"type": "string"})]
    return {"integer": 1, "number": 1.5, "boolean": True}.get(kind, "fictional")


@register
class ExampleAIConnector(Connector):
    key = "example_ai"
    name = "Example AI model"
    description = "A fictional AI model used to test the AI interface."
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.AI_MODELS})
    auth = ApiKeyAuth(fields=(CredentialField("api_key", "API key"),))
    data_flows = DataFlows(version=1, sends=("Fictional prompts",), receives=("Fictional answers",))
    outbound_actions = {"generate": Capability.AI_MODELS}
    hidden = True

    def test_connection(self):
        if not str(self.credentials.get("api_key", "")).startswith("example-"):
            raise ConnectorError("The example AI model rejected this key.")

    def perform(self, action, payload):
        if action != "generate" or not payload.get("messages"):
            raise ConnectorError("The example AI model needs messages.")
        last = payload["messages"][-1]
        tools = {tool["name"] for tool in payload.get("tools") or []}
        call_id = f"call-{len(payload['messages'])}"
        named = re.search(r"use tool (\w+)\s*(\{.*\})?", last["content"], re.DOTALL)
        calls = []
        if last["role"] == "user" and named and named.group(1) in tools:
            arguments = json.loads(named.group(2)) if named.group(2) else {}
            calls = [{"id": call_id, "name": named.group(1), "arguments": arguments}]
        elif last["role"] == "user" and tools and "use a tool" in last["content"]:
            calls = [{"id": call_id, "name": payload["tools"][0]["name"], "arguments": {}}]
        text = "The tool answered." if last["role"] == "tool" else last["content"][::-1]
        return {
            "id": "example-reply",
            "model": "example-model-1",
            "text": text,
            "tool_calls": calls,
            "data": fill(payload["schema"]) if payload.get("schema") else None,
            "input_tokens": len(str(payload["messages"])),
            "output_tokens": len(last["content"]),
            "stop_reason": "tool_use" if calls else "end_turn",
            "raw": {"echo": last["content"]},
        }
