"""BIA Edge: immigration case law and authorities, searched from a matter.

BIA Edge offers its research through an MCP server, so the connector calls its tools
over MCP with the firm's own API key (``bia_live_…``), sent as a bearer token. Keys
are issued to approved clients and need MCP access. The server is stateless, so each
search is one request with no session.

Each scope maps to one tool: court and agency cases (``search_cases``), unpublished
BIA and AAO decisions (``search_unpublished_cases``), statutes and regulations
(``search_statutes``), and policy and practice manuals (``search_references``).
Results come back as structured records. A busy or rate-limited answer comes back
as a result with ``ok: false``, so the connector waits as told and tries once more.

Links follow BIA Edge's presentation rules: only the official or preserved source
links it returns are shown, and a record without one is shown without a link.

Reference: the BIA Edge MCP server's structured output, schema version 2.0.
"""

import json

from crm.connect import http
from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
)
from crm.connect.mcp_client import MCPClient
from crm.connect.registry import register

MCP_URL = "https://mcp.biaedge.com/mcp"
KEY_PREFIX = "bia_live_"
# Scope -> (tool, extra arguments). The first scope is the default.
SCOPES = {
    "": ("search_cases", {}),
    "bia": ("search_cases", {"source": "bia"}),
    "circuit": ("search_cases", {"source": "circuit-courts"}),
    "unpublished": ("search_unpublished_cases", {}),
    "statutes": ("search_statutes", {}),
    "references": ("search_references", {}),
}
LABELS = (
    ("", "Precedent decisions and court cases"),
    ("bia", "BIA precedent decisions"),
    ("circuit", "Federal circuit courts"),
    ("unpublished", "Unpublished BIA and AAO decisions"),
    ("statutes", "Statutes and regulations"),
    ("references", "Policy and practice manuals"),
)
VALIDITY = {
    "caution": "Use with caution",
    "superseded_in_same_matter": "Superseded in the same matter",
}
# Errors BIA Edge says are worth one more try, after the wait it gives.
RETRYABLE = {"rate_limited", "server_busy"}
MAX_WAIT = 3
ERRORS = {
    "daily_quota_exceeded": "BIA Edge's daily limit for this key has been reached.",
    "rate_limited": "BIA Edge is limiting requests right now. Try again in a minute.",
    "server_busy": "BIA Edge is busy right now. Try again in a minute.",
}


def check_key(key):
    if not str(key or "").startswith(KEY_PREFIX):
        raise ConnectorError(f"A BIA Edge API key starts with {KEY_PREFIX}.")


def text_content(result):
    """The same structured result, from the text block, for a server that sends only text."""
    for part in result.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            try:
                return json.loads(part.get("text") or "")
            except ValueError:
                return None
    return None


def standing(record):
    """What BIA Edge says about the authority's weight, in a few words."""
    if record.get("validity_status") in VALIDITY:
        return VALIDITY[record["validity_status"]]
    authority = (record.get("metadata") or {}).get("authority") or {}
    if not isinstance(authority, dict):
        return ""
    if authority.get("non_precedential"):
        return "Not precedential"
    return str(authority.get("binding_level") or "").replace("_", " ").capitalize()


def result_from(record):
    """One BIA Edge record in core's research result shape."""
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    identifier = (
        record.get("source_id")
        or record.get("provision_id")
        or record.get("reference_id")
        or record.get("document_id")
        or record.get("id")
    )
    return {
        "id": str(identifier or ""),
        "citation": record.get("citation") or "",
        "title": record.get("title") or "",
        "court": metadata.get("court") or record.get("source_display") or "",
        "decided": str(record.get("date") or "")[:10],
        "kind": record.get("source_display") or "",
        "standing": standing(record),
        "url": record.get("url") or record.get("public_pdf_url") or "",
        "excerpt": record.get("text") or "",
    }


@register
class BIAEdgeConnector(Connector):
    key = "bia_edge"
    name = "BIA Edge"
    description = (
        "Search immigration case law, unpublished decisions, statutes, and policy manuals "
        "from a matter, and save the authorities that matter to it."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.LEGAL_RESEARCH})
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "api_key",
                "BIA Edge API key",
                help_text=f"Starts with {KEY_PREFIX}. BIA Edge issues keys to approved "
                "clients; the key needs MCP access.",
            ),
        ),
        label="API key",
    )
    data_flows = DataFlows(
        version=1,
        sends=("The words of each search staff run, and the source they choose",),
        receives=("Case names, citations, dates, courts, excerpts, and source links",),
    )
    outbound_actions = {"search_authorities": Capability.LEGAL_RESEARCH}
    search_scopes = LABELS

    def __init__(self, connection):
        super().__init__(connection)
        self.client = MCPClient(
            MCP_URL,
            name="BIA Edge",
            headers={"Authorization": f"Bearer {self.credentials.get('api_key', '')}"},
            # BIA Edge gives each search up to 25 seconds; one retry stays under a minute.
            timeout=27,
            retries=1,
            stateless=True,
        )

    def guarded(self, call, *args):
        check_key(self.credentials.get("api_key", ""))
        try:
            return call(*args)
        except ConnectorError as exc:
            if exc.status == 401:
                raise ConnectorError(
                    "BIA Edge did not accept the API key. Check that it is active and has MCP "
                    "access.",
                    status=401,
                ) from None
            if exc.status is None and "did not answer in time" in str(exc):
                raise ConnectorError(
                    "BIA Edge did not answer in time. Try the search again in a moment, or "
                    "narrow it.",
                ) from None
            raise

    def test_connection(self):
        tools = self.guarded(self.client.list_tools)
        if "search_cases" not in tools:
            raise ConnectorError("This BIA Edge key cannot search cases.")

    def search(self, tool, arguments):
        """Call a search tool and return its structured result, trying once more if told to."""
        for attempt in range(2):
            result = self.guarded(self.client.call_tool, tool, arguments)
            content = result.get("structuredContent") or text_content(result)
            if not isinstance(content, dict):
                raise ConnectorError("BIA Edge sent an answer that was not understood.")
            if content.get("ok", True):
                return content
            error = (content.get("errors") or [{}])[0]
            error = error if isinstance(error, dict) else {}
            code = str(error.get("code") or "")
            if code in RETRYABLE and attempt == 0:
                wait = (error.get("meta") or {}).get("retry_after_ms") or error.get(
                    "retry_after_ms"
                )
                http.sleep(min(float(wait or 1000) / 1000, MAX_WAIT))
                continue
            raise ConnectorError(
                ERRORS.get(code)
                or f"BIA Edge could not run the search ({code or 'no reason given'})."
            )

    def perform(self, action, payload):
        if action != "search_authorities":
            raise ConnectorError(f"BIA Edge has no action named {action}.")
        query = str(payload.get("query") or "").strip()
        if not query:
            raise ConnectorError("Write what to search for.")
        tool, extra = SCOPES.get(payload.get("scope") or "", SCOPES[""])
        limit = min(max(int(payload.get("limit") or 20), 1), 50)
        content = self.search(tool, {"query": query, "limit": limit, **extra})
        records = [record for record in content.get("records") or [] if isinstance(record, dict)]
        meta = content.get("meta") if isinstance(content.get("meta"), dict) else {}
        total = meta.get("total_hits")
        return {
            "id": str(meta.get("server_call_id") or ""),
            "results": [result_from(record) for record in records],
            "total": total if isinstance(total, int) else len(records),
        }
