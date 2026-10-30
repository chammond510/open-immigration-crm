from datetime import UTC, date, datetime, timedelta

from django.test import TestCase
from django.urls import reverse

from crm.connect import lookups
from crm.connect.models import Connection
from crm.connect.records import CaseStatusRecord
from crm.connect.signals import records_received
from crm.models import Matter, WorkItem
from crm.notices.alerts import review_for
from crm.notices.models import CaseNotice, CaseStatusCheck, CaseStatusProblem
from crm.notices.services import record_status
from crm.notices.tracking import receipts_to_check, tracked_receipts
from crm.tests.factories import make_contact, make_matter, make_user

NOW = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)


def reading(receipt, title="", at=NOW, problem="", text=""):
    return CaseStatusRecord(
        external_id=f"{receipt}@{at:%Y%m%d%H%M}",
        occurred_at=at,
        receipt_number=receipt,
        status_title=title,
        status_text=text,
        problem=problem,
    )


class TrackingTestCase(TestCase):
    def setUp(self):
        self.contact_count = 0

    def matter(self, receipt="", owner=None, **kwargs):
        self.contact_count += 1
        name = f"Client{self.contact_count}"
        return make_matter(
            make_contact(first_name=name, email=f"{name.lower()}@example.test"),
            owner,
            receipt_number=receipt,
            **kwargs,
        )


class TrackedReceiptTests(TrackingTestCase):
    def test_only_receipts_on_one_open_matter_are_tracked(self):
        first = self.matter("ioe-0912-345601 ")
        CaseNotice.objects.create(
            matter=first,
            kind="receipt",
            receipt_number="IOE0912345602",
            notice_date=date(2026, 1, 5),
        )
        self.matter("IOE0912345603", status=Matter.Status.APPROVED)
        self.matter("IOE0912345604", status=Matter.Status.CLOSED)
        self.matter("IOE0912345605")
        self.matter("IOE0912345605")
        self.matter("not a receipt")
        self.assertEqual(tracked_receipts(), {"IOE0912345601", "IOE0912345602", "IOE0912345603"})

    def test_due_receipts_never_checked_first_then_oldest(self):
        for number in range(1, 5):
            self.matter(f"IOE091234560{number}")
        matter = Matter.objects.get(receipt_number="IOE0912345601")
        record_status(
            matter,
            receipt_number="IOE0912345601",
            status_title="Case Was Received",
            checked_at=NOW - timedelta(hours=30),
        )
        record_status(
            Matter.objects.get(receipt_number="IOE0912345602"),
            receipt_number="IOE0912345602",
            status_title="Case Was Received",
            checked_at=NOW - timedelta(hours=2),
        )
        CaseStatusProblem.objects.create(
            matter=Matter.objects.get(receipt_number="IOE0912345603"),
            receipt_number="IOE0912345603",
            kind="not_found",
            first_seen_at=NOW - timedelta(days=3),
            last_seen_at=NOW - timedelta(hours=1),
        )
        self.assertEqual(receipts_to_check(limit=10, now=NOW), ["IOE0912345604", "IOE0912345601"])
        self.assertEqual(receipts_to_check(limit=1, now=NOW), ["IOE0912345604"])
        self.assertEqual(lookups.due("case_status", limit=10), receipts_to_check(limit=10))
        self.assertEqual(lookups.due("calendar", limit=10), [])


class ReviewTaskTests(TrackingTestCase):
    def setUp(self):
        super().setUp()
        self.owner = make_user("paralegal", superuser=False)
        self.connection = Connection.objects.create(
            connector_key="uscis_case_status", label="USCIS", enabled_capabilities=["case_status"]
        )
        self.case = self.matter("IOE0912345601", self.owner)

    def deliver(self, *records):
        records_received.send(
            sender=None,
            connection=self.connection,
            capability="case_status",
            records=list(records),
            source="sync",
        )

    def test_a_change_asks_staff_to_review_it(self):
        self.deliver(reading("IOE0912345601", "Case Was Received", NOW - timedelta(days=1)))
        self.assertFalse(WorkItem.objects.exists())
        self.deliver(reading("IOE0912345601", "Interview Was Scheduled"))
        task = WorkItem.objects.get()
        self.assertEqual(task.kind, WorkItem.Kind.TASK)
        self.assertEqual(task.priority, WorkItem.Priority.HIGH)
        self.assertEqual(task.assigned_to, self.owner)
        self.assertEqual(task.contact, self.case.primary_contact)
        self.assertEqual(
            task.title,
            f"Review USCIS interview update · {self.case.matter_number} · IOE0912345601",
        )
        self.assertIn(
            'changed from "Case Was Received" to "Interview Was Scheduled"', task.description
        )
        self.assertIn("before telling the client what it means", task.description)

        # A later change updates the open task and raises its priority.
        self.deliver(reading("IOE0912345601", "Case Was Denied", NOW + timedelta(days=1)))
        task.refresh_from_db()
        self.assertEqual(WorkItem.objects.count(), 1)
        self.assertEqual(task.priority, WorkItem.Priority.URGENT)
        self.assertTrue(task.title.startswith("Review USCIS denial or rejection"))
        self.assertTrue(task.description.startswith("Oct 31: receipt IOE0912345601 changed"))
        self.assertIn("Interview Was Scheduled", task.description)

        # Once it is done, the next change gets a new task.
        task.is_completed = True
        task.save()
        self.deliver(reading("IOE0912345601", "Case Was Reopened", NOW + timedelta(days=2)))
        new = WorkItem.objects.get(is_completed=False)
        self.assertEqual(new.priority, WorkItem.Priority.NORMAL)
        self.assertTrue(new.title.startswith("Review USCIS update"))

    def test_routine_progress_and_manual_readings_need_no_task(self):
        self.deliver(reading("IOE0912345601", "Case Was Received", NOW - timedelta(days=1)))
        self.deliver(reading("IOE0912345601", "Case Is Being Actively Reviewed By USCIS"))
        record_status(
            self.case,
            receipt_number="IOE0912345601",
            status_title="Case Was Approved",
            actor=self.owner,
        )
        self.assertFalse(WorkItem.objects.exists())
        self.assertEqual(CaseStatusCheck.objects.filter(changed=True).count(), 2)

    def test_reviewer_falls_back_to_the_contacts_owner(self):
        self.owner.is_active = False
        self.owner.save()
        backup = make_user("backup", superuser=False)
        contact = self.case.primary_contact
        contact.assigned_to = backup
        contact.save()
        self.deliver(reading("IOE0912345601", "Case Was Received", NOW - timedelta(days=1)))
        self.deliver(reading("IOE0912345601", "Case Was Approved"))
        self.assertEqual(WorkItem.objects.get().assigned_to, backup)

    def test_problems_are_kept_until_a_status_is_recorded(self):
        self.deliver(reading("IOE0912345601", problem="unavailable", at=NOW - timedelta(days=1)))
        self.deliver(reading("IOE0912345601", problem="not_found", text="Not recognized."))
        problem = CaseStatusProblem.objects.get()
        self.assertEqual((problem.kind, problem.detail), ("not_found", "Not recognized."))
        self.assertEqual(problem.first_seen_at, NOW)
        self.assertEqual(problem.source_connection, self.connection)
        self.deliver(reading("IOE0912345601", problem="not_found", at=NOW + timedelta(days=1)))
        problem.refresh_from_db()
        self.assertEqual(problem.first_seen_at, NOW)
        self.assertEqual(problem.last_seen_at, NOW + timedelta(days=1))

        self.client.force_login(make_user())
        page = self.client.get(reverse("matter_detail", args=[self.case.pk]))
        self.assertContains(page, "USCIS did not recognize this receipt number")
        self.assertContains(page, "8 U.S.C. 1367")
        self.assertNotContains(page, "No status recorded yet.")

        self.deliver(reading("IOE0912345601", "Case Was Received", NOW + timedelta(days=2)))
        self.assertFalse(CaseStatusProblem.objects.exists())
        self.assertFalse(WorkItem.objects.exists())


class ReviewRuleTests(TestCase):
    def test_status_titles(self):
        cases = {
            "Case Was Received": None,
            "Case Is Being Actively Reviewed By USCIS": None,
            "Fingerprint Fee Was Received": None,
            "Response To USCIS' Request For Evidence Was Received": "Review response received",
            "Request for Additional Evidence Was Sent": "Review USCIS request for evidence",
            "Request for Initial Evidence Was Sent": "Review USCIS request for evidence",
            "Notice of Intent to Deny Was Sent": "Review USCIS denial or rejection",
            "Case Was Denied": "Review USCIS denial or rejection",
            "Case Rejected Because I Sent An Incorrect Fee": "Review USCIS denial or rejection",
            "Interview Was Scheduled": "Review USCIS interview update",
            "Fingerprints Were Taken": "Review USCIS biometrics update",
            "Case Was Transferred And A New Office Has Jurisdiction": "Review USCIS transfer",
            "Case Was Approved": "Review USCIS approval",
            "New Card Is Being Produced": "Review USCIS approval",
            "Card Was Delivered To Me By The Post Office": "Review USCIS approval",
            "Case Was Reopened": "Review USCIS update",
            "Perfectly Ordinary Update": "Review USCIS update",
        }
        for title, expected in cases.items():
            review = review_for(title)
            if expected is None:
                self.assertIsNone(review, title)
            else:
                self.assertTrue(review.title.startswith(expected), title)
