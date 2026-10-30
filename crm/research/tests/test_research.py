from unittest import mock

from django.test import TestCase
from django.urls import reverse

from crm.connect.base import ConnectorError
from crm.connect.models import Connection, OutboundRequest
from crm.connect.registry import registry
from crm.connect.services import acknowledge_data_flows, create_connection
from crm.models import AuditLog
from crm.research import services
from crm.research.models import Authority, Search
from crm.tests.factories import make_contact, make_matter, make_user


class ResearchTestCase(TestCase):
    def setUp(self):
        self.admin = make_user()
        self.staff = make_user("paralegal", superuser=False)
        self.matter = make_matter(make_contact(self.staff), self.admin)
        self.connection = self.connect("Firm research")
        self.client.force_login(self.staff)
        self.page = reverse("matter_detail", args=[self.matter.pk])

    def connect(self, label, owner=None, preview=False):
        connection = create_connection(
            "example_research",
            label=label,
            created_by=self.admin,
            capabilities=["legal_research"],
            credentials={"api_key": "example-fictional"},
        )
        acknowledge_data_flows(connection, self.admin)
        Connection.objects.filter(pk=connection.pk).update(preview_mode=preview, owner=owner)
        connection.refresh_from_db()
        return connection

    def search(self, query, target=None):
        return self.client.post(
            reverse("research_search", args=[self.matter.pk]),
            {"query": query, "target": target or f"{self.connection.pk}:"},
            follow=True,
        )


class ResearchTests(ResearchTestCase):
    def test_search_save_and_remove(self):
        page = self.client.get(self.page)
        self.assertContains(page, "Legal research")
        self.assertContains(page, "Firm research · Board decisions")
        self.assertContains(page, "describe the legal issue rather than the client")

        response = self.search("exceptional hardship", f"{self.connection.pk}:board")
        found = Search.objects.get()
        self.assertRedirects(
            response,
            reverse("research_search_detail", args=[found.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual((found.status, found.scope, found.total), ("done", "board", 3))
        self.assertContains(response, "1 Fict. Dec. 101")
        self.assertContains(response, "Fictional Board")
        self.assertContains(response, 'href="https://research.example.test/decisions/1"')
        self.assertContains(response, 'rel="noopener noreferrer"')
        self.assertContains(response, "A fictional decision about exceptional hardship.")
        outbound = OutboundRequest.objects.get()
        self.assertEqual(
            (outbound.action, outbound.summary, outbound.target_type),
            ("search_authorities", "Legal research search", "Matter"),
        )
        self.assertNotIn("hardship", str(outbound.__dict__))

        save = reverse("research_save", args=[found.pk])
        response = self.client.post(save, {"index": 1, "note": "Hardship standard."}, follow=True)
        self.assertContains(response, "Saved 2 Fict. Dec. 102")
        self.assertContains(response, "Saved</span>")
        authority = Authority.objects.get()
        self.assertEqual(
            (authority.title, authority.court, str(authority.decided), authority.note),
            ("Matter of Example 2", "Fictional Board", "2022-03-12", "Hardship standard."),
        )
        self.assertEqual((authority.source, authority.saved_by), ("Firm research", self.staff))
        again = self.client.post(save, {"index": 1}, follow=True)
        self.assertContains(again, "was already saved")
        self.assertEqual(Authority.objects.count(), 1)
        self.assertTrue(AuditLog.objects.filter(action="authority.saved").exists())

        page = self.client.get(self.page)
        self.assertContains(page, "Matter of Example 2")
        self.assertContains(page, "Hardship standard.")
        self.assertContains(page, "exceptional hardship")  # Recent searches.

        removed = self.client.post(reverse("research_remove", args=[authority.pk]), follow=True)
        self.assertContains(removed, "Authority removed from the matter.")
        self.assertFalse(Authority.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action="authority.removed").exists())

    def test_bad_searches_and_saves(self):
        self.assertContains(self.search(""), "Write what to search for.")
        self.search("hardship")
        found = Search.objects.get()
        for index in ("", "9", "x"):
            response = self.client.post(
                reverse("research_save", args=[found.pk]), {"index": index}, follow=True
            )
            self.assertContains(response, "That result is no longer available.")
        self.assertFalse(Authority.objects.exists())

    def test_nothing_found_preview_and_failures(self):
        self.assertContains(self.search("nothing at all"), "Nothing was found.")
        Connection.objects.filter(pk=self.connection.pk).update(preview_mode=True)
        response = self.search("hardship")
        self.assertContains(response, "is in preview mode, so the search was not sent")
        self.assertEqual(Search.objects.first().status, "previewed")
        Connection.objects.filter(pk=self.connection.pk).update(preview_mode=False)
        with mock.patch.object(
            registry.get("example_research"),
            "perform",
            side_effect=ConnectorError("The service did not answer in time."),
        ):
            response = self.search("hardship")
        self.assertContains(
            response, "The search did not finish: The service did not answer in time."
        )

    def test_results_are_cleaned(self):
        answer = {
            "results": [
                {
                    "id": "1",
                    "title": "Kept",
                    "url": "http://insecure.example.test",
                    "decided": "soon",
                },
                {"id": "", "title": "No id"},
                {"id": "3"},
                "not a result",
            ],
            "total": -1,
        }
        with mock.patch.object(registry.get("example_research"), "perform", return_value=answer):
            found = services.search(
                self.matter, query="x", connection=self.connection, actor=self.staff
            )
        self.assertEqual(len(found.results), 1)
        self.assertEqual((found.results[0]["url"], found.results[0]["decided"]), ("", ""))
        self.assertIsNone(found.total)

    def test_old_searches_are_removed_but_authorities_stay(self):
        with mock.patch.object(services, "KEEP_SEARCHES", 2):
            for number in range(3):
                found = services.search(
                    self.matter, query=f"q{number}", connection=self.connection, actor=self.staff
                )
                if number == 0:
                    services.save(found, 0, actor=self.staff)
        self.assertEqual(list(Search.objects.values_list("query", flat=True)), ["q2", "q1"])
        self.assertEqual(Authority.objects.get().query, "q0")


class ResearchAccessTests(ResearchTestCase):
    def test_connections_people_may_use(self):
        mine = self.connect("Paralegal research", owner=self.staff)
        theirs = self.connect("Admin research", owner=self.admin)
        page = self.client.get(self.page)
        self.assertContains(page, "Paralegal research (yours)")
        self.assertNotContains(page, "Admin research")
        response = self.search("hardship", f"{theirs.pk}:")
        self.assertContains(response, "Choose a legal research connection you may use.")
        refused = self.search("hardship", f"{mine.pk}:not-a-scope")
        self.assertContains(refused, "Choose a legal research connection you may use.")
        found = services.search(
            self.matter, query="x", connection=mine, actor=self.staff, scope="not-a-scope"
        )
        self.assertEqual(found.scope, "")

    def test_without_a_connection_staff_add_authorities_by_hand(self):
        Connection.objects.update(status=Connection.Status.PAUSED)
        page = self.client.get(self.page)
        self.assertContains(page, "Connect your legal research service")
        add = reverse("research_add_by_hand", args=[self.matter.pk])
        refused = self.client.post(
            add, {"title": "Matter of Example", "url": "http://x.example.test"}, follow=True
        )
        self.assertContains(refused, "Use an https:// link.")
        response = self.client.post(
            add,
            {
                "citation": "1 Fict. Dec. 1",
                "title": "Matter of Example",
                "url": "https://research.example.test/1",
                "note": "Found in the library.",
            },
            follow=True,
        )
        self.assertContains(response, "Saved 1 Fict. Dec. 1")
        self.assertContains(response, "added by hand")
        self.assertEqual(Authority.objects.get().connector_key, "")
        refused = self.search("hardship")
        self.assertContains(refused, "Choose a legal research connection you may use.")
