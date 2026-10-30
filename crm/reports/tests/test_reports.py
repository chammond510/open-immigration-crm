import importlib
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.apps import apps
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.api.models import ApiSettings, ApiToken
from crm.billing.models import Invoice, Payment
from crm.billing.services import add_line, issue_invoice, record_payment
from crm.comms.services import record_communication
from crm.customfields.models import CustomField
from crm.models import Activity, Contact, Matter, TimeEntry, WorkItem
from crm.notices.models import CaseNotice
from crm.notices.services import record_status, save_notice
from crm.reports import periods
from crm.reports.metrics import aging_bucket, build_report
from crm.reports.models import StageChange
from crm.tests.factories import make_contact, make_matter, make_user
from crm.workflows.models import WorkflowTemplate
from crm.workflows.services import apply_workflow, parse_outline, save_template

TODAY = date(2026, 5, 20)  # a Wednesday


def at(day, hour=10):
    return timezone.make_aware(datetime.combine(day, time(hour)))


class PeriodTests(TestCase):
    def test_choices(self):
        cases = {
            "week": (date(2026, 5, 18), TODAY),
            "month": (date(2026, 5, 1), TODAY),
            "last_month": (date(2026, 4, 1), date(2026, 4, 30)),
            "quarter": (date(2026, 4, 1), TODAY),
            "year": (date(2026, 1, 1), TODAY),
            "30d": (date(2026, 4, 21), TODAY),
            "90d": (date(2026, 2, 20), TODAY),
            "nonsense": (date(2026, 5, 1), TODAY),
        }
        for key, (start, end) in cases.items():
            period = periods.resolve(key, today=TODAY)
            self.assertEqual((period.start, period.end), (start, end), key)
        previous = periods.resolve("last_month", today=TODAY).previous()
        self.assertEqual((previous.start, previous.end), (date(2026, 3, 2), date(2026, 3, 31)))
        self.assertEqual(str(previous), "Mar 2, 2026 – Mar 31, 2026")

    def test_custom_dates(self):
        period = periods.resolve("custom", "2026-01-05", "2026-01-11", today=TODAY)
        self.assertEqual((period.key, period.days), ("custom", 7))
        self.assertEqual(period.end_at, at(date(2026, 1, 12), 0))
        for start, end in (("2026-01-11", "2026-01-05"), ("2026-02-30", "2026-03-01"), ("", "")):
            self.assertEqual(periods.resolve("custom", start, end, today=TODAY).key, "month")


class StageHistoryTests(TestCase):
    def test_stage_changes_are_recorded(self):
        contact = make_contact()
        contact.notes = "No stage change"
        contact.save()
        contact.stage = Contact.Stage.CONSULTATION
        contact.save()
        Contact.objects.get(pk=contact.pk).save()
        self.assertEqual(
            list(StageChange.objects.values_list("from_stage", "to_stage")),
            [("", "inquiry"), ("inquiry", "consultation")],
        )
        self.assertEqual(str(StageChange.objects.last()), "Amina Example entered Consultation")

    def test_backfill_from_stage_notes(self):
        backfill = importlib.import_module("crm.reports.migrations.0001_stage_history").backfill
        changed = make_contact()
        untouched = make_contact(first_name="Ben", stage="retained")
        StageChange.objects.all().delete()
        for subject, day in (
            ("Pipeline stage changed to Consultation", date(2026, 2, 1)),
            ("Pipeline stage changed to Active matter", date(2026, 3, 1)),
            ("Pipeline stage changed to Somewhere new", date(2026, 3, 2)),
        ):
            Activity.objects.create(
                contact=changed, kind="status", subject=subject, occurred_at=at(day)
            )
        backfill(apps, None)
        self.assertEqual(
            list(changed.stage_changes.values_list("from_stage", "to_stage")),
            [("", "consultation"), ("consultation", "active")],
        )
        entry = untouched.stage_changes.get()
        self.assertEqual((entry.to_stage, entry.changed_at), ("retained", untouched.created_at))


class ReportTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.period = periods.resolve("custom", "2026-05-01", "2026-05-31")
        self.inside, self.before = at(date(2026, 5, 10)), at(date(2026, 4, 10))

    def contact(self, name, created, **fields):
        contact = make_contact(first_name=name, email=f"{name.lower()}@example.test", **fields)
        Contact.objects.filter(pk=contact.pk).update(created_at=created)
        StageChange.objects.filter(contact=contact).update(changed_at=created)
        contact.refresh_from_db()
        return contact

    def move(self, contact, stage, when):
        contact.stage = stage
        contact.save()
        latest = contact.stage_changes.order_by("-pk").first()
        StageChange.objects.filter(pk=latest.pk).update(changed_at=when)

    def test_intake_counts_first_entries_and_sources(self):
        source = CustomField.objects.create(
            applies_to="contact",
            label="Referral source",
            key="referral_source",
            field_type="choice",
            choices="Website\nRadio",
        )
        ana = self.contact("Ana", self.inside, custom_fields={"referral_source": "Website"})
        ben = self.contact("Ben", self.inside)
        old = self.contact("Old", self.before, custom_fields={"referral_source": "Radio"})
        self.move(ana, "consultation", self.inside)
        self.move(ana, "retained", self.inside)
        self.move(ben, "consultation", self.inside)
        self.move(ben, "lost", self.inside)
        self.move(old, "consultation", self.before)
        self.move(old, "active", self.inside)
        self.move(old, "consultation", self.inside)

        report = build_report(self.period, include_billing=False)
        funnel = {row["key"]: row["count"] for row in report["funnel"]}
        self.assertEqual(funnel, {"new_leads": 2, "consultations": 2, "retained": 2, "lost": 1})
        self.assertEqual(report["retained_per_consult"], 100)
        self.assertEqual(report["funnel"][0]["share"], 100)
        self.assertEqual(report["source_field"], source)
        self.assertEqual(
            report["sources"],
            [
                {"value": "Website", "leads": 1, "retained": 1},
                {"value": "Not recorded", "leads": 1, "retained": 0},
                {"value": "Radio", "leads": 0, "retained": 1},
            ],
        )
        headline = {item["key"]: item for item in report["headline"]}
        self.assertEqual(
            (headline["new_leads"]["value"], headline["new_leads"]["previous"]), (2, 1)
        )
        self.assertEqual(headline["new_leads"]["change"], 1)
        self.assertNotIn("invoiced", headline)

    def test_matters_deadlines_uscis_communications_and_time(self):
        contact = self.contact("Ana", self.inside)
        matter = make_matter(contact, self.user, case_type="family")
        Matter.objects.filter(pk=matter.pk).update(created_at=self.inside)
        closed = make_matter(contact, title="Closed fictional matter", status="closed")
        Matter.objects.filter(pk=closed.pk).update(closed_at=self.inside)
        template = save_template(
            WorkflowTemplate(name="Fictional"), parse_outline("Gather\n- Item"), self.user
        )
        apply_workflow(matter, template, self.user)
        now = timezone.now()
        WorkItem.objects.create(title="Late", matter=matter, scheduled_for=now - timedelta(days=1))
        WorkItem.objects.create(
            title="Soon", matter=matter, kind="deadline", scheduled_for=now + timedelta(days=2)
        )
        done = WorkItem.objects.create(title="Done", matter=matter, is_completed=True)
        WorkItem.objects.filter(pk=done.pk).update(completed_at=self.inside)
        save_notice(
            CaseNotice(
                matter=matter,
                kind="rfe",
                notice_date=date(2026, 5, 3),
                response_due_date=timezone.localdate() + timedelta(days=60),
            ),
            self.user,
        )
        save_notice(
            CaseNotice(matter=matter, kind="receipt", notice_date=date(2026, 4, 3)), self.user
        )
        record_status(
            matter, receipt_number="IOE0912345678", status_title="Received", checked_at=self.inside
        )
        record_status(
            matter, receipt_number="IOE0912345678", status_title="Approved", checked_at=self.inside
        )
        record_communication(channel="call", contact=contact, occurred_at=self.inside)
        record_communication(channel="call", contact=contact, occurred_at=self.before)
        TimeEntry.objects.create(
            user=self.user,
            matter=matter,
            started_at=self.inside,
            stopped_at=self.inside + timedelta(minutes=90),
        )
        TimeEntry.objects.create(
            user=self.user,
            matter=matter,
            started_at=self.before,
            stopped_at=self.before + timedelta(hours=1),
        )

        report = build_report(self.period, include_billing=False)
        matters = report["matters"]
        self.assertEqual((matters["open"], matters["opened"], matters["closed"]), (1, 1, 1))
        self.assertEqual({r["key"]: r["count"] for r in matters["by_status"]}["open"], 1)
        self.assertEqual([r["key"] for r in matters["by_case_type"]], ["family"])
        self.assertEqual(
            matters["by_phase"], [{"workflow": "Fictional", "phase": "Gather", "count": 1}]
        )
        deadlines = report["deadlines"]
        self.assertEqual(deadlines["overdue"], 1)
        self.assertEqual([item.title for item in deadlines["due_soon"]], ["Soon"])
        self.assertEqual((deadlines["completed"], deadlines["open_responses"]), (1, 1))
        self.assertEqual(
            report["uscis"]["notices"],
            [{"key": "rfe", "label": "Request for evidence (RFE)", "count": 1}],
        )
        self.assertEqual(report["uscis"]["status_changes"], 1)
        calls = {row["key"]: row["count"] for row in report["communications"]}
        self.assertEqual((calls["call"], calls["email"]), (1, 0))
        self.assertEqual(report["time"]["hours"], Decimal("1.5"))
        self.assertEqual(report["time"]["by_user"][0]["hours"], Decimal("1.5"))
        self.assertEqual(report["time"]["unbilled_hours"], Decimal("2.5"))
        self.assertIsNone(report["billing"])

    def test_billing(self):
        contact = self.contact("Ana", self.inside)
        invoice = Invoice.objects.create(contact=contact, created_by=self.user)
        add_line(invoice, description="Fictional fee", unit_price=Decimal("1000"))
        issue_invoice(invoice, self.user)
        Invoice.objects.filter(pk=invoice.pk).update(
            issue_date=date(2026, 5, 2), due_date=TODAY - timedelta(days=45)
        )
        for amount, account, day in (
            ("300", "operating", date(2026, 5, 5)),
            ("200", "trust", date(2026, 4, 5)),
        ):
            record_payment(
                invoice,
                self.user,
                amount=amount,
                method="check",
                deposit_account=account,
                received_on=day,
            )
        voided = Payment.objects.create(
            invoice=invoice,
            amount=Decimal("50"),
            method="cash",
            deposit_account="operating",
            received_on=date(2026, 5, 6),
            voided_at=timezone.now(),
        )
        self.assertIsNotNone(voided.voided_at)

        billing = build_report(self.period, include_billing=True)["billing"]
        self.assertEqual(billing["invoiced"], Decimal("1000.00"))
        self.assertEqual(
            (billing["collected"], billing["collected_operating"], billing["collected_trust"]),
            (Decimal("300.00"), Decimal("300.00"), Decimal("0.00")),
        )
        self.assertEqual(billing["outstanding"], Decimal("500.00"))
        aging = {row["key"]: row["amount"] for row in billing["aging"]}
        expected_bucket = aging_bucket(TODAY - timedelta(days=45), timezone.localdate())
        self.assertEqual(aging[expected_bucket], Decimal("500.00"))
        self.assertEqual(
            billing["overdue"],
            Decimal("500.00") if expected_bucket != "current" else Decimal("0.00"),
        )
        self.assertEqual(
            [aging_bucket(TODAY - timedelta(days=n), TODAY) for n in (0, 10, 40, 70, 100)],
            ["current", "1_30", "31_60", "61_90", "90_plus"],
        )
        self.assertEqual(aging_bucket(None, TODAY), "current")


class DashboardPageTests(TestCase):
    def setUp(self):
        self.staff = make_user("paralegal", superuser=False)

    def test_staff_see_figures_without_billing(self):
        self.client.force_login(self.staff)
        page = self.client.get(reverse("operating_dashboard"))
        self.assertContains(page, "Operating dashboard")
        self.assertContains(page, 'href="/reports/"')
        self.assertContains(page, "This month")
        self.assertNotContains(page, "<h2>Billing</h2>", html=True)
        self.assertContains(page, "Add a contact custom field")
        self.assertNotContains(page, "Add a field</a>")
        page = self.client.get(
            reverse("operating_dashboard"),
            {"period": "custom", "start": "2026-01-01", "end": "2026-01-31"},
        )
        self.assertContains(page, "Jan 1, 2026 – Jan 31, 2026")
        self.assertContains(page, 'value="2026-01-01"')

    def test_billing_access_adds_billing(self):
        self.staff.user_permissions.add(Permission.objects.get(codename="access_billing"))
        CustomField.objects.create(
            applies_to="contact", label="Source", key="source", field_type="choice", choices="Web"
        )
        self.client.force_login(self.staff)
        page = self.client.get(reverse("operating_dashboard"), {"by": "source"})
        self.assertContains(page, "<h2>Billing</h2>", html=True)
        self.assertContains(page, "Outstanding by age")
        self.assertContains(page, "New leads and retentions by source.")


class ReportApiTests(TestCase):
    def setUp(self):
        self.user = make_user()
        ApiSettings.objects.update_or_create(pk=1, defaults={"enabled": True})
        make_contact()

    def get(self, *scopes, **params):
        _, token = ApiToken.issue(user=self.user, name="Fictional", scopes=scopes, lifetime_days=30)
        return self.client.get(
            "/api/v1/reports/summary/", params, headers={"Authorization": f"Bearer {token}"}
        )

    def test_summary(self):
        self.assertEqual(self.get("contacts:read").status_code, 403)
        body = self.get("reports:read", period="30d").json()
        self.assertEqual(body["period"]["key"], "30d")
        self.assertEqual(body["headline"]["new_leads"], {"value": 1, "previous": 0})
        self.assertEqual(body["intake"]["new_leads"], 1)
        self.assertEqual(body["time"]["hours"], "0.0")
        self.assertNotIn("billing", body)
        body = self.get("reports:read", "billing:read").json()
        self.assertEqual(body["billing"]["outstanding"], "0.00")
        self.assertEqual(body["headline"]["collected"], {"value": "0.00", "previous": "0.00"})
