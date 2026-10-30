"""A fictional AI model for testing the AI interface without any network access.

It is hidden from the catalog. It answers with the last user message reversed,
calls the first tool it is offered when asked to, and fills a requested schema
with fictional values.
"""

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
        calls = []
        if payload.get("tools") and "use a tool" in last["content"]:
            calls = [{"id": "call-1", "name": payload["tools"][0]["name"], "arguments": {}}]
        return {
            "id": "example-reply",
            "model": "example-model-1",
            "text": last["content"][::-1],
            "tool_calls": calls,
            "data": fill(payload["schema"]) if payload.get("schema") else None,
            "input_tokens": len(str(payload["messages"])),
            "output_tokens": len(last["content"]),
            "stop_reason": "tool_use" if calls else "end_turn",
            "raw": {"echo": last["content"]},
        }
