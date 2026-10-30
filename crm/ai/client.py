"""One way to ask an AI model, whichever provider the firm connected.

Core knows only the ``generate`` action that AI model connectors declare. The
payload is ``{"model", "system", "messages", "tools", "schema", "schema_name",
"max_tokens"}``. Each message is ``{"role": "user" | "assistant" | "tool",
"content", "tool_calls", "tool_call_id"}``, and each tool ``{"name",
"description", "parameters"}``, where parameters is a JSON schema. The connector
returns ``{"id", "model", "text", "tool_calls", "data", "input_tokens",
"output_tokens", "stop_reason", "raw"}``, where ``data`` is the parsed answer when
a schema was asked for, and ``raw`` is the provider's own copy of the assistant's
turn. Core passes ``raw`` back unchanged on that assistant message in a later
request, since some providers need their reasoning returned exactly, as in a tool loop.

Every request goes through the outbound policy and is logged with the feature,
the person, and the connection that paid for it; prompts and answers are not
logged. A staff member's request runs through a connection they may use. Work
no one is waiting on runs only through the firm's chosen background connection,
never a personal one.
"""

from dataclasses import dataclass, field

from crm.connect import policy
from crm.connect.base import Capability
from crm.connect.models import Connection, OutboundRequest
from crm.connect.services import usable_connections

from . import features
from .models import AIRequest, AISettings

GENERATE = "generate"


class AIUnavailable(Exception):
    """The request did not run; the message says why and is safe to show."""


@dataclass(frozen=True)
class Message:
    role: str  # user, assistant, or tool
    content: str = ""
    tool_calls: tuple = ()  # ToolCall, on an assistant message
    tool_call_id: str = ""  # On a tool message: the call it answers
    raw: object = None  # On an assistant message: the provider's own copy, from a Reply


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class Reply:
    text: str
    tool_calls: tuple
    data: dict | None
    model: str
    input_tokens: int | None
    output_tokens: int | None
    stop_reason: str
    raw: object = None

    def as_message(self):
        """This reply as the assistant's turn in a follow-up request."""
        return Message(
            role="assistant", content=self.text, tool_calls=self.tool_calls, raw=self.raw
        )


def ai_connections(user):
    """AI model connections ``user`` may ask through: the firm's and their own."""
    return usable_connections(user, GENERATE, Capability.AI_MODELS)


def firm_ai_connections():
    return [
        connection
        for connection in Connection.objects.filter(
            status=Connection.Status.ACTIVE, owner__isnull=True
        )
        if connection.connector_class is not None
        and Capability.AI_MODELS in connection.enabled_capabilities
        and connection.connector_class.outbound_actions.get(GENERATE) == Capability.AI_MODELS
    ]


def message_payload(message):
    return {
        "role": message.role,
        "content": message.content,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in message.tool_calls
        ],
        "tool_call_id": message.tool_call_id,
        "raw": message.raw,
    }


def reply_from(answer):
    calls = tuple(
        ToolCall(
            id=str(c.get("id", "")), name=str(c.get("name", "")), arguments=c.get("arguments") or {}
        )
        for c in answer.get("tool_calls") or []
    )
    return Reply(
        text=str(answer.get("text") or ""),
        tool_calls=calls,
        data=answer.get("data"),
        model=str(answer.get("model") or ""),
        input_tokens=answer.get("input_tokens"),
        output_tokens=answer.get("output_tokens"),
        stop_reason=str(answer.get("stop_reason") or ""),
        raw=answer.get("raw"),
    )


def generate(
    *,
    feature,
    messages,
    system="",
    tools=(),
    schema=None,
    schema_name="answer",
    actor=None,
    connection=None,
    max_tokens=2048,
):
    """Ask a model; return a Reply, or raise AIUnavailable saying why it did not run.

    With ``actor``, it is that staff member's request, through ``connection``. Without
    one it is background work, through the firm's background connection.
    """
    ai_settings = AISettings.load()
    if feature != features.CONNECTION_TEST and not ai_settings.feature_on(feature):
        raise AIUnavailable("This AI feature is off. A superuser can turn it on in AI settings.")
    if actor is None:
        connection = ai_settings.background_connection
        if connection is None or connection not in firm_ai_connections():
            raise AIUnavailable("Choose the connection for background work in AI settings.")
    elif connection is None or connection not in ai_connections(actor):
        raise AIUnavailable("Choose an AI connection you may use.")
    found = features.get(feature)
    outbound = policy.send(
        connection,
        GENERATE,
        {
            "model": "",
            "system": system,
            "messages": [message_payload(m) for m in messages],
            "tools": [
                {"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in tools
            ],
            "schema": schema,
            "schema_name": schema_name,
            "max_tokens": max_tokens,
        },
        actor=actor,
        is_automation=actor is None,
        summary=f"AI: {found.label if found else feature}",
    )
    answer = getattr(outbound, "response", None)
    answer = answer if isinstance(answer, dict) else {}
    AIRequest.objects.create(
        feature=feature,
        connection=connection,
        connector_key=connection.connector_key,
        actor=actor,
        is_background=actor is None,
        model=str(answer.get("model") or "")[:120],
        status=outbound.status,
        input_tokens=answer.get("input_tokens"),
        output_tokens=answer.get("output_tokens"),
        error=outbound.error,
    )
    if outbound.status == OutboundRequest.Status.PREVIEWED:
        raise AIUnavailable(
            f"{connection.label} is in preview mode, so nothing was sent to the model. A "
            "superuser can turn preview mode off on the connection's page."
        )
    if outbound.status != OutboundRequest.Status.SENT:
        raise AIUnavailable(outbound.error or "The request was not sent.")
    return reply_from(answer)
