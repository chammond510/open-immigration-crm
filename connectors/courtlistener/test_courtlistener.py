import httpx
from django.test import TestCase
from django.urls import reverse

from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection
from crm.connect.services import acknowledge_data_flows
from crm.connect.testing import ConnectorContract
from crm.research.models import Authority, Search
from crm.tests.factories import make_contact, make_matter, make_user

from . import APPEALS, CourtListenerConnector, wait_words

TOKEN = "fictional-courtlistener-token"

CASE = {
    "absolute_url": "/opinion/101/example-v-fictional/",
    "caseName": "Example v. Fictional",
    "caseNameFull": "Example Respondent v. Fictional Attorney General",
    "citation": ["999 F.4th 101", "2025 WL 1"],
    "cluster_id": 101,
    "court": "Court of Appeals for the Ninth Circuit",
    "court_id": "ca9",
    "dateFiled": "2025-04-02",
    "docketNumber": "24-0001",
    "status": "Unpublished",
    "opinions": [
        {"id": 5, "snippet": "\nThe <mark>hardship</mark> standard is\n exceptional.\n"},
    ],
}
NO_CITATION = {
    "absolute_url": "/opinion/102/matter-of-example/",
    "caseName": "Matter of Example",
    "citation": [],
    "neutralCite": "",
    "cluster_id": 102,
    "court": "Board of Immigration Appeals",
    "dateFiled": "2019-11-20",
    "status": "Published",
    "opinions": [],
}


class FakeCourtListener:
    def __init__(self):
        self.requests = []
        self.status = 200
        self.results = [CASE, NO_CITATION, {"caseName": "No cluster id"}]

    def __call__(self, request):
        self.requests.append(request)
        if request.headers.get("Authorization") != f"Token {TOKEN}":
            return httpx.Response(401, json={"detail": "Invalid token."})
        if request.url.path.endswith("/api-usage/"):
            return httpx.Response(200, json={"current_usage": []})
        if self.status == 429:
            return httpx.Response(
                429,
                json={"detail": "Request was throttled."},
                headers={"Retry-After": "300"},
            )
        if self.status == 400:
            return httpx.Response(400, json={"detail": "Unbalanced parentheses"})
        return httpx.Response(200, json={"count": 2343, "next": None, "results": self.results})

    def params(self):
        return dict(self.requests[-1].url.params)


class FakeCourtListenerMixin:
    def setUp(self):
        super().setUp()
        self.courtlistener = FakeCourtListener()
        transport = http.use_transport(httpx.MockTransport(lambda r: self.courtlistener(r)))
        transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)


class CourtListenerContractTests(FakeCourtListenerMixin, ConnectorContract, TestCase):
    connector_class = CourtListenerConnector
    valid_credentials = {"api_token": TOKEN}
    invalid_credentials = {"api_token": "revoked"}
    outbound_examples = {"search_authorities": {"query": "hardship", "limit": 5}}


class CourtListenerTests(FakeCourtListenerMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.connector = CourtListenerConnector(
            Connection(
                connector_key="courtlistener",
                enabled_capabilities=["legal_research"],
                credentials={"api_token": TOKEN},
            )
        )

    def search(self, scope="", query="hardship"):
        return self.connector.perform(
            "search_authorities", {"query": query, "scope": scope, "limit": 20}
        )

    def test_results_and_courts(self):
        answer = self.search()
        self.assertEqual(answer["total"], 2343)
        first, second = answer["results"]
        self.assertEqual(
            first,
            {
                "id": "cluster:101",
                "citation": "999 F.4th 101",
                "title": "Example v. Fictional",
                "court": "Court of Appeals for the Ninth Circuit",
                "decided": "2025-04-02",
                "kind": "Case law",
                "standing": "Unpublished",
                "url": "https://www.courtlistener.com/opinion/101/example-v-fictional/",
                "excerpt": "The hardship standard is exceptional.",
            },
        )
        self.assertEqual((second["citation"], second["excerpt"]), ("", ""))
        params = self.courtlistener.params()
        self.assertEqual(
            {k: params[k] for k in ("q", "type", "order_by", "highlight", "court")},
            {
                "q": "hardship",
                "type": "o",
                "order_by": "score desc",
                "highlight": "on",
                "court": APPEALS,
            },
        )
        self.assertEqual((params["stat_Published"], params["stat_Unpublished"]), ("on", "on"))
        self.assertEqual(
            str(self.courtlistener.requests[-1].url).split("?")[0],
            "https://www.courtlistener.com/api/rest/v4/search/",
        )
        self.search("bia")
        self.assertEqual(self.courtlistener.params()["court"], "bia")
        self.search("all")
        self.assertNotIn("court", self.courtlistener.params())

    def test_limits_and_refusals_are_reported_at_once(self):
        self.courtlistener.status = 429
        with self.assertRaisesMessage(
            ConnectorError, "request limit. Try again in about 5 minutes"
        ):
            self.search()
        self.assertEqual(len(self.courtlistener.requests), 1)
        self.courtlistener.status = 400
        with self.assertRaisesMessage(ConnectorError, "could not run that search: Unbalanced"):
            self.search()
        self.connector.client.headers["Authorization"] = "Token revoked"
        with self.assertRaisesMessage(ConnectorError, "did not accept the API token"):
            self.search()
        with self.assertRaisesMessage(ConnectorError, "Write what to search for."):
            self.search(query=" ")
        with self.assertRaisesMessage(ConnectorError, "no action named"):
            self.connector.perform("send_text", {})
        self.assertEqual(wait_words("7200"), "in about 2 hours")
        self.assertEqual(wait_words("30"), "in about 1 minute")
        self.assertEqual(wait_words(""), "later")

    def test_slow_and_odd_answers(self):
        def slow(request):
            raise httpx.ReadTimeout("slow")

        with http.use_transport(httpx.MockTransport(slow)):
            with self.assertRaisesMessage(ConnectorError, "Try a narrower search"):
                self.search()
        with http.use_transport(httpx.MockTransport(lambda r: httpx.Response(200, text="nope"))):
            with self.assertRaisesMessage(ConnectorError, "not understood"):
                self.search()


class CourtListenerPageTests(FakeCourtListenerMixin, TestCase):
    def test_each_staff_member_searches_with_their_own_token(self):
        make_user()
        staff = make_user("paralegal", superuser=False)
        other = make_user("other", superuser=False)
        matter = make_matter(make_contact(staff), staff)
        self.client.force_login(staff)
        page = self.client.get(reverse("connector_detail", args=["courtlistener"]))
        self.assertContains(page, "do not allow sharing a token")
        self.client.post(
            reverse("connector_detail", args=["courtlistener"]),
            {
                "label": "My CourtListener",
                "capabilities": ["legal_research"],
                "credential_api_token": TOKEN,
            },
        )
        connection = Connection.objects.get()
        self.assertEqual((connection.owner, connection.status), (staff, Connection.Status.ACTIVE))
        Connection.objects.filter(pk=connection.pk).update(preview_mode=False)
        connection.refresh_from_db()
        acknowledge_data_flows(connection, make_user("reviewer"))
        response = self.client.post(
            reverse("research_search", args=[matter.pk]),
            {"query": "hardship", "target": f"{connection.pk}:bia"},
            follow=True,
        )
        self.assertContains(response, "999 F.4th 101")
        self.assertContains(response, "Unpublished")
        self.client.post(reverse("research_save", args=[Search.objects.get().pk]), {"index": 0})
        self.assertEqual(Authority.objects.get().external_id, "cluster:101")
        # Another staff member cannot search through this person's account.
        self.client.force_login(other)
        page = self.client.get(reverse("matter_detail", args=[matter.pk]))
        self.assertNotContains(page, f'value="{connection.pk}:')
        self.assertContains(page, "Connect your legal research service")
        self.assertContains(page, "from My CourtListener")  # The saved authority stays visible.
