import json
from unittest import mock

import httpx
from django.test import TestCase
from django.urls import reverse

from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.mcp_client import MCPClient, read_message
from crm.connect.models import Connection
from crm.connect.testing import ConnectorContract
from crm.research.models import Authority, Search
from crm.tests.factories import make_contact, make_matter, make_user

from . import BIAEdgeConnector

KEY = "bia_live_fictional-key"

CASE = {
    "kind": "case",
    "source_id": "case:101",
    "document_id": 101,
    "title": "Matter of Example-Respondent",
    "citation": "99 I&N Dec. 101 (BIA 2024)",
    "date": "2024-03-15",
    "source": "bia",
    "source_display": "BIA precedent decision",
    "url": "https://www.justice.gov/eoir/fictional-decision.pdf",
    "text": "A fictional holding about exceptional hardship.",
    "metadata": {
        "court": "Board of Immigration Appeals",
        "authority": {"binding_level": "binding"},
    },
}
CAUTION = {
    "kind": "case",
    "source_id": "case:102",
    "title": "Example v. Fictional",
    "citation": "999 F.4th 1 (9th Cir. 2025)",
    "date": "2025-01-02",
    "source_display": "Ninth Circuit",
    "public_pdf_url": "https://www.courtlistener.com/fictional.pdf",
    "validity_status": "caution",
}
UNPUBLISHED = {
    "kind": "unpublished_decision",
    "source_id": "unpublished:7",
    "title": "Fictional respondent (unpublished)",
    "date": "2023-06-01",
    "source_display": "Unpublished BIA decision",
    "metadata": {"authority": {"non_precedential": True}},
}


def envelope(*records, ok=True, errors=(), total=None):
    return {
        "ok": ok,
        "schema_version": "2.0",
        "kind": "search_results",
        "records": list(records),
        "warnings": [],
        "errors": list(errors),
        "meta": {
            "total_hits": total if total is not None else len(records),
            "server_call_id": "call-1",
        },
    }


class FakeBIAEdge:
    """BIA Edge's MCP server as documented: stateless, JSON answers, bearer keys."""

    def __init__(self):
        self.requests = []
        self.answers = []
        self.text_only = False

    def __call__(self, request):
        self.requests.append(request)
        if request.headers.get("Authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"error": "Unauthorized"})
        assert "application/json" in request.headers["Accept"]
        body = json.loads(request.content)
        if body["method"] == "tools/list":
            result = {"tools": [{"name": "search_cases"}, {"name": "search_statutes"}]}
        else:
            content = self.answers.pop(0) if self.answers else envelope(CASE)
            if isinstance(content, Exception):
                raise content
            text = [{"type": "text", "text": json.dumps(content)}]
            result = (
                {"content": text}
                if self.text_only
                else {"content": text, "structuredContent": content}
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def calls(self):
        return [
            json.loads(r.content)["params"]
            for r in self.requests
            if json.loads(r.content)["method"] == "tools/call"
        ]


class FakeBIAEdgeMixin:
    def setUp(self):
        super().setUp()
        self.bia = FakeBIAEdge()
        transport = http.use_transport(httpx.MockTransport(lambda r: self.bia(r)))
        transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)
        patcher = mock.patch.object(http, "sleep")
        self.slept = patcher.start()
        self.addCleanup(patcher.stop)


class BIAEdgeContractTests(FakeBIAEdgeMixin, ConnectorContract, TestCase):
    connector_class = BIAEdgeConnector
    valid_credentials = {"api_key": KEY}
    invalid_credentials = {"api_key": "bia_live_revoked"}
    outbound_examples = {"search_authorities": {"query": "exceptional hardship", "limit": 5}}


class BIAEdgeTests(FakeBIAEdgeMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.connection = Connection.objects.create(
            connector_key="bia_edge",
            label="BIA Edge",
            enabled_capabilities=["legal_research"],
            credentials={"api_key": KEY},
        )
        self.connector = BIAEdgeConnector(self.connection)

    def search(self, scope="", query="exceptional hardship"):
        return self.connector.perform(
            "search_authorities", {"query": query, "scope": scope, "limit": 80}
        )

    def test_results_and_scopes(self):
        self.bia.answers = [envelope(CASE, CAUTION, UNPUBLISHED, total=40)]
        answer = self.search()
        self.assertEqual(answer["total"], 40)
        first, second, third = answer["results"]
        self.assertEqual(
            first,
            {
                "id": "case:101",
                "citation": "99 I&N Dec. 101 (BIA 2024)",
                "title": "Matter of Example-Respondent",
                "court": "Board of Immigration Appeals",
                "decided": "2024-03-15",
                "kind": "BIA precedent decision",
                "standing": "Binding",
                "url": "https://www.justice.gov/eoir/fictional-decision.pdf",
                "excerpt": "A fictional holding about exceptional hardship.",
            },
        )
        self.assertEqual(
            (second["standing"], second["url"], second["court"]),
            ("Use with caution", "https://www.courtlistener.com/fictional.pdf", "Ninth Circuit"),
        )
        self.assertEqual((third["standing"], third["url"]), ("Not precedential", ""))
        request = self.bia.requests[-1]
        self.assertEqual(request.url, "https://mcp.biaedge.com/mcp")
        self.assertNotIn("Mcp-Session-Id", request.headers)
        self.assertEqual(
            self.bia.calls()[-1],
            {"name": "search_cases", "arguments": {"query": "exceptional hardship", "limit": 50}},
        )
        # Stateless: one request per search, with no initialize handshake.
        self.assertEqual(len(self.bia.requests), 1)

        for scope, tool, extra in (
            ("bia", "search_cases", {"source": "bia"}),
            ("circuit", "search_cases", {"source": "circuit-courts"}),
            ("unpublished", "search_unpublished_cases", {}),
            ("statutes", "search_statutes", {}),
            ("references", "search_references", {}),
            ("unknown", "search_cases", {}),
        ):
            self.search(scope)
            call = self.bia.calls()[-1]
            self.assertEqual(call["name"], tool)
            self.assertEqual({k: v for k, v in call["arguments"].items() if k == "source"}, extra)

    def test_text_only_answers_are_read_too(self):
        self.bia.text_only = True
        self.assertEqual(self.search()["results"][0]["id"], "case:101")

    def test_busy_answers_are_tried_once_more(self):
        busy = {"code": "server_busy", "retryable": True, "meta": {"retry_after_ms": 500}}
        self.bia.answers = [envelope(ok=False, errors=[busy]), envelope(CASE)]
        self.assertEqual(len(self.search()["results"]), 1)
        self.slept.assert_called_once_with(0.5)
        self.bia.answers = [envelope(ok=False, errors=[busy])] * 2
        with self.assertRaisesMessage(ConnectorError, "BIA Edge is busy right now."):
            self.search()
        quota = {"code": "daily_quota_exceeded"}
        self.bia.answers = [envelope(ok=False, errors=[quota])]
        with self.assertRaisesMessage(ConnectorError, "daily limit for this key"):
            self.search()
        self.bia.answers = [envelope(ok=False, errors=[{"code": "query_invalid"}])]
        with self.assertRaisesMessage(ConnectorError, "could not run the search (query_invalid)"):
            self.search()

    def test_keys_timeouts_and_odd_answers(self):
        self.connection.credentials = {"api_key": "sk-not-bia"}
        with self.assertRaisesMessage(ConnectorError, "starts with bia_live_"):
            BIAEdgeConnector(self.connection).test_connection()
        self.connection.credentials = {"api_key": "bia_live_revoked"}
        with self.assertRaisesMessage(ConnectorError, "did not accept the API key"):
            BIAEdgeConnector(self.connection).perform("search_authorities", {"query": "x"})
        self.bia.answers = [httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")]
        with self.assertRaisesMessage(
            ConnectorError, "did not answer in time. Try the search again"
        ):
            self.search()
        self.bia.answers = ["not a dict"]
        with self.assertRaisesMessage(ConnectorError, "not understood"):
            self.search()
        with self.assertRaisesMessage(ConnectorError, "Write what to search for."):
            self.search(query=" ")
        with self.assertRaisesMessage(ConnectorError, "no action named"):
            self.connector.perform("send_text", {})

    def test_a_key_without_case_search_is_refused(self):
        with mock.patch.object(MCPClient, "list_tools", return_value=["search_statutes"]):
            with self.assertRaisesMessage(ConnectorError, "cannot search cases"):
                self.connector.test_connection()


class MCPClientTests(TestCase):
    def test_sessions_event_streams_and_errors(self):
        seen = []

        def server(request):
            body = json.loads(request.content)
            seen.append((body.get("method"), request.headers.get("Mcp-Session-Id")))
            if body["method"] == "initialize":
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
                    headers={"Mcp-Session-Id": "session-1"},
                )
            if "id" not in body:
                return httpx.Response(202)
            if body["params"]["name"] == "broken":
                message = {"jsonrpc": "2.0", "id": body["id"], "error": {"message": "no such tool"}}
            elif body["params"]["name"] == "failing":
                result = {"isError": True, "content": [{"type": "text", "text": "Bad input"}]}
                message = {"jsonrpc": "2.0", "id": body["id"], "result": result}
            else:
                message = {"jsonrpc": "2.0", "id": body["id"], "result": {"content": []}}
            stream = f"event: message\ndata: {json.dumps(message)}\n\n"
            return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

        with http.use_transport(httpx.MockTransport(server)):
            client = MCPClient("https://mcp.example.test/mcp", name="Example")
            self.assertEqual(client.call_tool("search", {}), {"content": []})
            self.assertEqual(
                seen,
                [
                    ("initialize", None),
                    ("notifications/initialized", "session-1"),
                    ("tools/call", "session-1"),
                ],
            )
            with self.assertRaisesMessage(ConnectorError, "Example answered with no such tool."):
                client.call_tool("broken", {})
            with self.assertRaisesMessage(ConnectorError, "Example could not do that: Bad input"):
                client.call_tool("failing", {})

    def test_unreadable_answers(self):
        response = httpx.Response(
            200, text="data: not json\n\n", headers={"content-type": "text/event-stream"}
        )
        self.assertIsNone(read_message(response, 1))
        with http.use_transport(
            httpx.MockTransport(lambda r: httpx.Response(200, json={"id": 99}))
        ):
            client = MCPClient("https://mcp.example.test/mcp", name="Example", stateless=True)
            with self.assertRaisesMessage(ConnectorError, "not understood"):
                client.list_tools()


class BIAEdgeResearchPageTests(FakeBIAEdgeMixin, TestCase):
    def test_searching_from_a_matter(self):
        admin = make_user()
        matter = make_matter(make_contact(admin), admin)
        self.client.force_login(admin)
        page = self.client.get(reverse("connector_detail", args=["bia_edge"]))
        self.assertContains(page, "Starts with bia_live_.")
        self.client.post(
            reverse("connector_detail", args=["bia_edge"]),
            {
                "label": "BIA Edge",
                "capabilities": ["legal_research"],
                "credential_api_key": KEY,
                "acknowledge": "on",
            },
        )
        connection = Connection.objects.get()
        self.assertEqual(connection.status, Connection.Status.ACTIVE)
        Connection.objects.filter(pk=connection.pk).update(preview_mode=False)
        page = self.client.get(reverse("matter_detail", args=[matter.pk]))
        self.assertContains(page, "BIA Edge · Unpublished BIA and AAO decisions")
        self.bia.answers = [envelope(CASE, UNPUBLISHED)]
        response = self.client.post(
            reverse("research_search", args=[matter.pk]),
            {"query": "exceptional hardship", "target": f"{connection.pk}:bia"},
            follow=True,
        )
        self.assertContains(response, "99 I&amp;N Dec. 101 (BIA 2024)")
        self.assertContains(response, "No source link")
        search = Search.objects.get()
        self.client.post(reverse("research_save", args=[search.pk]), {"index": 0})
        self.assertEqual(Authority.objects.get().external_id, "case:101")
