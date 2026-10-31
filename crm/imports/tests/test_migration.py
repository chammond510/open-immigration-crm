from datetime import UTC, date, datetime, timedelta
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.connect import refs
from crm.connect.models import Connection, ConnectorApp, SyncRun
from crm.connect.records import ContactRecord, MatterRecord
from crm.connect.registry import registry
from crm.connect.scheduler import queue_due_syncs
from crm.connect.signals import records_received
from crm.imports import migration
from crm.imports.models import SpreadsheetImport
from crm.models import Activity, Contact, Matter, MatterParty
from crm.tests.factories import make_contact, make_user

NOW = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)


def person(external_id, first, last, **extra):
    return ContactRecord(
        external_id=external_id, occurred_at=NOW, first_name=first, last_name=last, **extra
    )


def matter(external_id, client, title, **extra):
    return MatterRecord(
        external_id=external_id, occurred_at=NOW, client=client, title=title, **extra
    )


RECORDS = [
    person("c1", "Lena", "Fictional", email="lena@example.test", date_of_birth=date(1990, 3, 31)),
    person("c2", "Omar", "Fictional", phone="202-555-0145", city="Arlington"),
    person("c3", "Amina", "Known", email="amina@example.test"),
    person("c4", "Nadia", "Fictional"),
    ContactRecord(external_id="c5", occurred_at=NOW, company="Fictional Employer LLC"),
    ContactRecord(external_id="c6", occurred_at=NOW, company="Fictional Translation Co."),
    matter(
        "m1",
        "c1",
        "Spousal petition",
        number="00001-Fictional",
        case_type="family",
        practice_area="Family immigration",
        opened_on=date(2026, 1, 5),
        responsible_email="attorney@example.test",
        parties=(("c4", "Beneficiary"), ("c5", "Employer"), ("c1", "Client")),
    ),
    matter("m2", "c1", "Waiver", status="pending"),
    matter("m3", "c2", "Naturalization", status="closed"),
    matter("m4", "c5", "H-1B petition", case_type="employment"),
    matter("m5", "c9", "Lost client"),
]


class MigrationCase(TestCase):
    def setUp(self):
        self.admin = make_user()
        self.client.force_login(self.admin)
        self.connection = Connection.objects.create(
            connector_key="clio",
            label="Clio",
            enabled_capabilities=["migration"],
            credentials={"access_token": "fictional"},
        )

    def fetch(self, records):
        records_received.send(
            sender=None,
            connection=self.connection,
            capability="migration",
            records=records,
            source="sync",
        )
        return self.connection.imports.filter(status="preparing").first()


class BuildImportTests(MigrationCase):
    def test_records_become_rows_to_review(self):
        item = self.fetch(RECORDS)
        self.assertEqual((item.connection, item.row_count), (self.connection, 8))
        self.assertTrue(item.filename.startswith("Clio Manage, fetched "))
        preview = self.client.get(reverse("import_preview", args=[item.pk]))
        self.assertEqual(preview.status_code, 200)  # Its columns are already matched.
        plan = preview.context["plan"]
        self.assertEqual(
            {row.number: row.reason for row in plan.skipped},
            {
                5: "Its client is a company; the CRM's contacts are people.",
                6: "Its client was not among the records fetched.",
                9: "A company; the CRM's contacts are people, so add it by hand if needed.",
            },
        )
        # m1, m2 and m3, then the contacts who are no matter's client: Amina and Nadia.
        self.assertEqual(
            [(row.number, row.name) for row in plan.rows if not row.reason],
            [
                (2, "Lena Fictional"),
                (3, "Lena Fictional"),
                (4, "Omar Fictional"),
                (7, "Amina Known"),
                (8, "Nadia Fictional"),
            ],
        )
        lena = plan.rows[0].contact_form.cleaned_data
        self.assertEqual((lena["status"], lena["stage"]), ("client", "active"))
        omar = plan.rows[2].contact_form.cleaned_data
        self.assertEqual((omar["status"], omar["stage"]), ("former", "closed"))
        nadia = plan.rows[6].contact_form.cleaned_data
        self.assertEqual((nadia["status"], nadia["stage"]), ("prospect", "inquiry"))
        self.assertEqual(plan.rows[1].same_as, 2)  # Lena's second matter.
        first = plan.rows[0].matter_form.cleaned_data
        self.assertEqual(first["assigned_to"], self.admin)
        self.assertEqual(
            first["description"],
            "Matter 00001-Fictional in Clio Manage.\nOpened 2026-01-05.\n"
            "Practice area: Family immigration.",
        )
        self.assertContains(preview, "From Clio Manage.")

    def test_nothing_new_and_other_capabilities(self):
        self.assertIsNone(migration.build_import(self.connection, []))
        # Only a company, which can never be imported: nothing to review.
        self.assertIsNone(migration.build_import(self.connection, [RECORDS[4]]))
        records_received.send(
            sender=None,
            connection=self.connection,
            capability="texting",
            records=RECORDS,
            source="sync",
        )
        self.assertFalse(SpreadsheetImport.objects.exists())

    def test_a_newer_fetch_replaces_one_not_confirmed_and_large_fetches_continue(self):
        first = self.fetch(RECORDS)
        with mock.patch.object(migration, "MAX_ROWS", 2):
            second = self.fetch(RECORDS)
        self.assertFalse(SpreadsheetImport.objects.filter(pk=first.pk).exists())
        self.assertEqual((second.row_count, second.options["later"]), (2, 6))
        self.assertEqual(sorted(second.refs), ["2", "3"])


class ImportFromConnectionTests(MigrationCase):
    def test_import_links_records_and_adds_parties(self):
        amina = make_contact(first_name="Amina", last_name="Original", email="amina@example.test")
        item = self.fetch(RECORDS)
        self.client.post(reverse("import_preview", args=[item.pk]))
        item.refresh_from_db()
        self.assertEqual((item.contacts_created, item.matters_created), (3, 3))
        self.assertEqual(item.options["contacts_linked"], 1)
        self.assertEqual(item.options["parties_added"], 1)
        lena, _ = refs.find(self.connection, Contact, "c1")
        self.assertEqual(lena.email, "lena@example.test")
        self.assertEqual(
            sorted(lena.matters.values_list("title", flat=True)), ["Spousal petition", "Waiver"]
        )
        spousal, _ = refs.find(self.connection, Matter, "m1")
        nadia, _ = refs.find(self.connection, Contact, "c4")
        self.assertEqual(
            list(MatterParty.objects.values_list("matter", "contact", "role")),
            [(spousal.pk, nadia.pk, "Beneficiary")],
        )
        self.assertEqual(refs.find(self.connection, Contact, "c3")[0], amina)
        amina.refresh_from_db()
        self.assertEqual(amina.last_name, "Original")  # Linked, never changed.
        self.assertTrue(
            Activity.objects.filter(subject="Contact imported from Clio Manage").exists()
        )
        report = self.client.get(reverse("import_report", args=[item.pk]))
        self.assertContains(report, "1 contact already in the CRM linked, and 1 matter party")

        # The next fetch brings only what is new: a matter for a client imported before.
        again = self.fetch([*RECORDS, matter("m6", "c2", "Renewal")])
        self.assertEqual(again.options["imported_before"], 3)
        self.assertEqual(again.row_count, 4)  # m4, m5, m6, and the second company.
        preview = self.client.get(reverse("import_preview", args=[again.pk]))
        [row] = [row for row in preview.context["plan"].rows if not row.reason]
        omar, _ = refs.find(self.connection, Contact, "c2")
        self.assertEqual((row.contact, row.contact_form), (omar, None))
        self.assertContains(preview, "3 matters imported before are left out.")
        self.client.post(reverse("import_preview", args=[again.pk]))
        self.assertEqual(omar.matters.count(), 2)

    def test_a_matter_imported_meanwhile_is_skipped(self):
        item = self.fetch(RECORDS[:7])
        contact = make_contact(first_name="Lena", email="other@example.test")
        refs.link(self.connection, Matter.objects.create(primary_contact=contact, title="x"), "m1")
        preview = self.client.get(reverse("import_preview", args=[item.pk]))
        self.assertIn("This matter was imported before.", preview.content.decode())

    def test_connection_page_lists_its_imports(self):
        item = self.fetch(RECORDS)
        page = self.client.get(reverse("connection_detail", args=[self.connection.pk]))
        self.assertContains(page, "Ready to review")
        self.assertContains(page, reverse("import_preview", args=[item.pk]))
        self.assertContains(page, "Fetch records to import")
        listing = self.client.get(reverse("import_list"))
        self.assertContains(listing, "rows from Clio Manage")


class OnDemandSyncTests(MigrationCase):
    def test_staff_start_the_sync_and_the_schedule_leaves_it(self):
        self.assertIsNone(registry.get("clio").sync_interval)
        self.assertEqual(queue_due_syncs(), 0)
        url = reverse("connection_sync", args=[self.connection.pk])
        self.assertRedirects(
            self.client.post(url), reverse("connection_detail", args=[self.connection.pk])
        )
        run = SyncRun.objects.get()
        self.assertEqual((run.capability, run.status), ("migration", "queued"))
        self.client.post(url)  # Already under way.
        self.assertEqual(SyncRun.objects.count(), 1)
        SyncRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - timedelta(hours=2))
        self.client.post(url)  # An abandoned run does not block a new one.
        self.assertEqual(SyncRun.objects.count(), 2)
        self.connection.status = Connection.Status.PAUSED
        self.connection.save()
        answer = self.client.post(url, follow=True)
        self.assertContains(answer, "not active")

    def test_app_settings_reach_the_connector(self):
        connector = self.connection.connector()
        self.assertEqual(connector.app_setting("region"), "")
        ConnectorApp.objects.create(
            connector_key="clio", client_id="fictional", config={"region": "eu.app.clio.com"}
        )
        self.assertEqual(connector.app_setting("region"), "eu.app.clio.com")
