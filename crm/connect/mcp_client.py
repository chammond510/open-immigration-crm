"""A small MCP client for connectors whose service offers its tools over MCP.

It speaks the streamable HTTP transport through ``ProviderClient``, so timeouts,
retries, size limits, and test transports work as for any other provider. Each
call starts a fresh session (``initialize``, then the ``initialized``
notification), unless the server is stateless and needs none. Answers may come as
JSON or as a server-sent event stream; either way the JSON-RPC result is returned.
"""

import json
from itertools import count

from .base import ConnectorError
from .http import ProviderClient

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "open-immigration-crm", "version": "1"}
SESSION_HEADER = "Mcp-Session-Id"


def read_message(response, request_id):
    """The JSON-RPC message answering ``request_id``, from JSON or an event stream."""
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        messages = []
        for event in response.text.split("\n\n"):
            data = "\n".join(
                line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
            )
            if data:
                messages.append(data)
    else:
        messages = [response.text]
    for raw in messages:
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    return None


class MCPClient:
    def __init__(self, url, *, name, headers=None, timeout=30, retries=2, stateless=False):
        self.url = url
        self.name = name
        self.stateless = stateless
        self.http = ProviderClient(
            name=name,
            headers={"Accept": "application/json, text/event-stream", **(headers or {})},
            timeout=timeout,
            retries=retries,
        )
        self.ids = count(1)

    def rpc(self, method, params, session_id=""):
        request_id = next(self.ids)
        headers = {"MCP-Protocol-Version": PROTOCOL_VERSION}
        if session_id:
            headers[SESSION_HEADER] = session_id
        response = self.http.request(
            "POST",
            self.url,
            json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            headers=headers,
        )
        message = read_message(response, request_id)
        if message is None:
            raise ConnectorError(f"{self.name} sent an answer that was not understood.")
        if "error" in message:
            error = message["error"] if isinstance(message["error"], dict) else {}
            detail = str(error.get("message") or "an error")[:200]
            raise ConnectorError(f"{self.name} answered with {detail}.")
        return message.get("result") or {}, response.headers.get(SESSION_HEADER, session_id)

    def start(self):
        """Open a session and return its ID, which may be empty for a stateless server."""
        if self.stateless:
            return ""
        _, session_id = self.rpc(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
        )
        headers = {"MCP-Protocol-Version": PROTOCOL_VERSION}
        if session_id:
            headers[SESSION_HEADER] = session_id
        self.http.request(
            "POST",
            self.url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=headers,
        )
        return session_id

    def list_tools(self):
        result, _ = self.rpc("tools/list", {}, self.start())
        return [tool.get("name") for tool in result.get("tools") or [] if isinstance(tool, dict)]

    def call_tool(self, name, arguments):
        """Call a tool and return its result: ``structuredContent`` when given, else ``content``."""
        result, _ = self.rpc("tools/call", {"name": name, "arguments": arguments}, self.start())
        if result.get("isError"):
            text = " ".join(
                str(part.get("text") or "")
                for part in result.get("content") or []
                if isinstance(part, dict)
            )
            raise ConnectorError(
                f"{self.name} could not do that: {text[:300] or 'no reason given'}"
            )
        return result
