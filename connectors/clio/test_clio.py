import time
from datetime import date
from unittest import mock
from urllib.parse import parse_qs

import httpx
from django.test import TestCase
from django.urls import reverse

from crm.connect import http, oauth
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, ConnectorApp, SyncRun
from crm.connect.records import ContactRecord, MatterRecord
from crm.connect.services import acknowledge_data_flows
from crm.connect.tasks import run_sync
from crm.connect.testing import ConnectorContract
from crm.tests.factories import make_user

from . import ClioConnector, case_type

ATTORNEY = "attorney@example.test"  # The email make_user gives its superuser.


def signed_in():
    return {
        "access_token": "fictional-access",
        "refresh_token": "fictional-refresh",
        "token_type": "Bearer",
        "expires_at": time.time() + 3600,
    }


class FakeClio:
    """Clio Manage API v4, as its reference documents it, with fictional records."""

    def __init__(self, host="app.clio.com"):
        self.host = host
        self.requests = []
        self.status = None
        self.next_host = host
        self.contacts = [
            [
                {
                    "id": 10,
                    "type": "Person",
                    "name": "Lena Q. Fictional",
                    "first_name": "Lena",
                    "last_name": "Fictional",
                    "date_of_birth": "1990-03-31",
                    "primary_email_address": "lena@example.test",
                    "primary_phone_number": "202-555-0144",
                    "addresses": [
                        {"street": "1 Old Road", "city": "Elsewhere", "primary": False},
                        {
                            "street": "100 Sample St\nApt 4",
                            "city": "Arlington",
                            "province": "VA",
                            "postal_code": "22201",
                            "country": "United States",
                            "primary": True,
                        },
                    ],
                    "updated_at": "2026-09-30T16:15:42Z",
                },
                {"id": 11, "type": "Company", "name": "Fictional Employer LLC"},
            ],
            [
                {
                    "id": 12,
                    "type": "Person",
                    "name": "Nadia Fictional",
                    "first_name": "",
                    "last_name": "",
                    "updated_at": "2026-09-30T16:15:42-04:00",
                },
                {
                    "id": 13,
                    "type": "Person",
                    "name": "Cher",
                    "date_of_birth": "1990-13-45",
                    "updated_at": "not a time",
                },
            ],
        ]
        self.matters = [
            {
                "id": 20,
                "display_number": "00020-Fictional",
                "description": "Spousal petition\nWith the I-130A.",
                "status": "Open",
                "open_date": "2026-02-01",
                "client": {"id": 10},
                "practice_area": {"name": "Family immigration"},
                "responsible_attorney": {"email": ATTORNEY},
                "updated_at": "2026-09-15T18:20:00Z",
            },
            {
                "id": 21,
                "display_number": "00021",
                "description": "",
                "status": "Closed",
                "client": {"id": 12},
                "practice_area": None,
            },
            {"id": 22, "display_number": "00022", "status": "Archived", "client": None},
        ]
        self.relationships = [
            {"id": 30, "description": "Beneficiary", "matter": {"id": 20}, "contact": {"id": 12}},
            {"id": 31, "description": "", "matter": {"id": 20}, "contact": None},
        ]

    def calls(self, path):
        return [r for r in self.requests if r.url.path.endswith(path)]

    def page(self, request, pages, path):
        token = request.url.params.get("page_token")
        index = int(token) if token else 0
        body = {"data": pages[index], "meta": {"paging": {}}}
        if index + 1 < len(pages):
            # As Clio does, the next link repeats every parameter with the page token.
            body["meta"]["paging"]["next"] = (
                f"https://{self.next_host}/api/v4/{path}?fields=x&page_token={index + 1}"
            )
        return httpx.Response(200, json=body)

    def __call__(self, request):
        self.requests.append(request)
        assert request.url.host == self.host
        path = request.url.path
        if path == "/oauth/deauthorize":
            return httpx.Response(200)
        if request.headers.get("authorization") != "Bearer fictional-access":
            return httpx.Response(401, json={"error": {"type": "Unauthorized", "message": "x"}})
        if self.status:
            return httpx.Response(self.status, json={"error": {"type": "x", "message": "x"}})
        if path == "/api/v4/users/who_am_i.json":
            return httpx.Response(200, json={"data": {"id": 1, "name": "Fictional Attorney"}})
        if path == "/api/v4/contacts.json":
            return self.page(request, self.contacts, "contacts.json")
        if path == "/api/v4/matters.json":
            return self.page(request, [self.matters], "matters.json")
        assert path == "/api/v4/relationships.json", path
        return self.page(request, [self.relationships], "relationships.json")


class FakeClioMixin:
    def setUp(self):
        super().setUp()
        self.clio = FakeClio()
        self.enterContext(http.use_transport(httpx.MockTransport(self.clio)))
        # Clio's requests are spaced for its rate limit; the tests need not wait.
        self.enterContext(mock.patch.object(http, "sleep"))


class ClioContractTests(FakeClioMixin, ConnectorContract, TestCase):
    connector_class = ClioConnector
    valid_credentials = signed_in()
    invalid_credentials = {**signed_in(), "access_token": "expired"}
    sync_examples = {"migration": {}}


class ClioTests(FakeClioMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user()
        self.connection = Connection.objects.create(
            connector_key="clio",
            label="Clio",
            enabled_capabilities=["migration"],
            credentials=signed_in(),
        )

    def test_sync_reads_contacts_matters_and_their_links(self):
        batch = ClioConnector(self.connection).sync("migration", {})
        self.assertEqual(batch.cursor, {})
        contacts = [r for r in batch.records if isinstance(r, ContactRecord)]
        matters = [r for r in batch.records if isinstance(r, MatterRecord)]
        lena, company, nadia, cher = contacts
        self.assertEqual(
            (lena.first_name, lena.email, lena.phone, lena.date_of_birth),
            ("Lena", "lena@example.test", "202-555-0144", date(1990, 3, 31)),
        )
        self.assertEqual(
            (lena.address_line1, lena.address_line2, lena.city, lena.region, lena.postal_code),
            ("100 Sample St", "Apt 4", "Arlington", "VA", "22201"),
        )
        self.assertEqual(
            (company.company, company.first_name), ("Fictional Employer LLC", "Fictional Employer")
        )
        self.assertEqual(
            (nadia.first_name, nadia.last_name, nadia.company), ("Nadia", "Fictional", "")
        )
        # A one-word name, and a date Clio could not have meant, are taken as far as they go.
        self.assertEqual((cher.first_name, cher.last_name, cher.date_of_birth), ("Cher", "", None))
        spousal, closed = matters  # The matter with no client is left out.
        self.assertEqual(
            (spousal.title, spousal.description, spousal.number, spousal.status),
            ("Spousal petition", "With the I-130A.", "00020-Fictional", "open"),
        )
        self.assertEqual(
            (spousal.case_type, spousal.opened_on, spousal.responsible_email, spousal.parties),
            ("family", date(2026, 2, 1), ATTORNEY, (("12", "Beneficiary"),)),
        )
        self.assertEqual((closed.title, closed.status, closed.client), ("00021", "closed", "12"))
        for record in batch.records:
            self.assertEqual(record.problems(), [])

        first, second = self.clio.calls("/contacts.json")
        self.assertEqual(first.url.params["order"], "id(asc)")
        self.assertEqual(first.url.params["limit"], "200")
        self.assertIn("addresses{street", first.url.params["fields"])
        self.assertEqual(second.url.params["page_token"], "1")
        self.assertNotIn("order", self.clio.calls("/relationships.json")[0].url.params)

    def test_a_next_page_elsewhere_is_refused(self):
        self.clio.next_host = "elsewhere.example.test"
        with self.assertRaisesMessage(ConnectorError, "next page somewhere else"):
            ClioConnector(self.connection).sync("migration", {})
        self.assertEqual(len(self.clio.requests), 1)

    def test_refusals_explain_what_to_do(self):
        connector = ClioConnector(self.connection)
        self.clio.status = 403
        with self.assertRaisesMessage(ConnectorError, "read permission for Contacts and Matters"):
            connector.test_connection()
        self.clio.status = 500
        with self.assertRaises(ConnectorError):
            connector.test_connection()
        with self.assertRaisesMessage(ConnectorError, "does not sync"):
            connector.sync("texting", {})
        self.connection.credentials = {**signed_in(), "access_token": "expired"}
        with self.assertRaisesMessage(ConnectorError, "in the same region"):
            ClioConnector(self.connection).test_connection()

    def test_the_firms_region_is_used_throughout(self):
        app = ConnectorApp.objects.create(
            connector_key="clio",
            client_id="fictional-client",
            client_secret="fictional-secret",
            config={"region": "eu.app.clio.com"},
        )
        self.clio.host = self.clio.next_host = "eu.app.clio.com"
        connector = ClioConnector(self.connection)
        connector.test_connection()
        self.assertEqual(
            oauth.app_url(ClioConnector.auth.token_url, app), "https://eu.app.clio.com/oauth/token"
        )
        connector.revoke()
        [revoked] = self.clio.calls("/oauth/deauthorize")
        self.assertEqual(parse_qs(revoked.content.decode()), {"token": ["fictional-access"]})
        app.config = {"region": "clio.example.test"}
        app.save()
        with self.assertRaisesMessage(Exception, "Choose the region"):
            ClioConnector(self.connection).test_connection()

    def test_the_firm_app_takes_a_region(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse("connector_detail", args=["clio"]))
        self.assertContains(
            page, '<option value="eu.app.clio.com">Europe (eu.app.clio.com)</option>'
        )
        url = reverse("connector_app", args=["clio"])
        app = {"client_id": "fictional-client", "client_secret": "fictional-secret"}
        self.client.post(url, {**app, "setting_region": "clio.example.test"})
        self.assertFalse(ConnectorApp.objects.exists())
        self.client.post(url, {**app, "setting_region": "ca.app.clio.com"})
        self.assertEqual(ConnectorApp.objects.get().config, {"region": "ca.app.clio.com"})

    def test_practice_areas_suggest_case_types(self):
        cases = {
            "Asylum and refugee": "humanitarian",
            "Citizenship / Naturalization": "naturalization",
            "Immigration Court": "removal",
            "Waivers": "waiver",
            "BIA appeals": "appeal",
            "Business immigration (H-1B)": "employment",
            "Marriage-based green cards": "family",
            "General": "",
            None: "",
        }
        for area, expected in cases.items():
            with self.subTest(area=area):
                self.assertEqual(case_type(area), expected)

    def test_fetching_from_the_connection_page_makes_an_import(self):
        acknowledge_data_flows(self.connection, self.admin)
        self.client.force_login(self.admin)
        self.client.post(reverse("connection_sync", args=[self.connection.pk]))
        run = SyncRun.objects.get()
        with self.captureOnCommitCallbacks(execute=True):
            run_sync.call(run.pk)
        run.refresh_from_db()
        self.assertEqual((run.status, run.records_received), ("succeeded", 6))
        item = self.connection.imports.get()
        self.assertEqual(item.row_count, 4)  # Two matters, then the company and Cher.
        self.client.post(reverse("import_preview", args=[item.pk]))
        item.refresh_from_db()
        self.assertEqual((item.contacts_created, item.matters_created), (2, 2))
        page = self.client.get(reverse("connection_detail", args=[self.connection.pk]))
        self.assertContains(page, "2 contacts and 2 matters added")
