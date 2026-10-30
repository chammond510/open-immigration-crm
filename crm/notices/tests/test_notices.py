from datetime import date, datetime, time

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.models import Activity, AuditLog, MatterParty, WorkItem
from crm.notices.models import (
    CaseNotice,
    CaseStatusCheck,
    normalize_receipt_number,
    validate_receipt_number,
)
from crm.notices.services import delete_notice, latest_statuses, record_status, save_notice
from crm.tests.factories import make_contact, make_matter, make_user

RECEIPT = "IOE0912345678"


class ReceiptNumberTests(TestCase):
    def test_normalize_and_validate(self):
        self.assertEqual(normalize_receipt_number(" ioe-091 234 5678 "), RECEIPT)
        validate_receipt_number(RECEIPT)
        validate_receipt_number("")
        for bad in ("IOE09123", "1234567890123", "IOE091234567X"):
            with self.assertRaises(ValidationError):
                validate_receipt_number(bad)


class NoticeServiceTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.contact = make_contact(self.user)
        self.matter = make_matter(self.contact, self.user)

    def notice(self, **fields):
        options = {
            "matter": self.matter,
            "kind": "receipt",
            "notice_date": date(2026, 3, 2),
            **fields,
        }
        return save_notice(CaseNotice(**options), self.user)

    def test_receipt_notice_fills_blank_matter_fields_once(self):
        notice = self.notice(
            receipt_number="ioe 0912345678",
            form_type="I-130",
            uscis_received_date=date(2026, 2, 27),
            priority_date=date(2026, 2, 27),
        )
        self.matter.refresh_from_db()
        self.assertEqual(notice.receipt_number, RECEIPT)
        self.assertEqual(notice.contact, self.contact)
        self.assertEqual(self.matter.receipt_number, RECEIPT)
        self.assertEqual(self.matter.filing_date, date(2026, 2, 27))
        self.assertEqual(self.matter.priority_date, date(2026, 2, 27))
        activity = Activity.objects.get(matter=self.matter, kind=Activity.Kind.SYSTEM)
        self.assertEqual(activity.subject, "Receipt notice logged")
        self.assertIn(
            "Filled in the matter's receipt number, filing date, priority date", activity.body
        )
        self.assertEqual(str(notice), f"Receipt notice · I-130 · {RECEIPT}")
        self.assertTrue(AuditLog.objects.filter(action="notice.logged").exists())

        self.notice(receipt_number="EAC2190012345", uscis_received_date=date(2026, 3, 1))
        self.matter.refresh_from_db()
        self.assertEqual(self.matter.receipt_number, RECEIPT)
        self.assertEqual(self.matter.filing_date, date(2026, 2, 27))

    def test_rfe_due_date_becomes_a_deadline_that_follows_edits(self):
        notice = self.notice(kind="rfe", receipt_number=RECEIPT, response_due_date=date(2026, 6, 1))
        deadline = notice.deadline
        self.assertEqual(deadline.kind, WorkItem.Kind.DEADLINE)
        self.assertEqual(deadline.priority, WorkItem.Priority.HIGH)
        self.assertEqual(deadline.matter, self.matter)
        self.assertEqual(
            timezone.localtime(deadline.scheduled_for),
            timezone.make_aware(datetime.combine(date(2026, 6, 1), time(17))),
        )
        self.assertIn("response due", deadline.title)
        notice.response_due_date = date(2026, 6, 8)
        save_notice(notice, self.user)
        deadline.refresh_from_db()
        self.assertEqual(timezone.localtime(deadline.scheduled_for).date(), date(2026, 6, 8))
        notice.response_due_date = None
        save_notice(notice, self.user)
        self.assertFalse(WorkItem.objects.filter(pk=deadline.pk).exists())
        self.assertTrue(AuditLog.objects.filter(action="notice.updated").exists())

    def test_noid_is_urgent_and_completed_items_are_left_alone(self):
        notice = self.notice(kind="noid", response_due_date=date(2026, 6, 1))
        self.assertEqual(notice.deadline.priority, WorkItem.Priority.URGENT)
        WorkItem.objects.filter(pk=notice.deadline_id).update(is_completed=True)
        notice.refresh_from_db()
        notice.response_due_date = date(2026, 7, 1)
        save_notice(notice, self.user)
        notice.deadline.refresh_from_db()
        self.assertEqual(timezone.localtime(notice.deadline.scheduled_for).date(), date(2026, 6, 1))
        delete_notice(notice, self.user)
        self.assertTrue(WorkItem.objects.filter(pk=notice.deadline_id).exists())

    def test_appointments_become_calendar_items_and_delete_with_the_notice(self):
        when = timezone.make_aware(datetime(2026, 5, 4, 10, 30))
        notice = self.notice(
            kind="biometrics", appointment_at=when, appointment_location="Fictional ASC"
        )
        appointment = notice.appointment
        self.assertEqual(appointment.kind, WorkItem.Kind.APPOINTMENT)
        self.assertEqual(appointment.scheduled_for, when)
        self.assertEqual(appointment.description, "Fictional ASC")
        self.assertIsNone(notice.deadline)
        delete_notice(notice, self.user)
        self.assertFalse(WorkItem.objects.filter(pk=appointment.pk).exists())
        self.assertTrue(AuditLog.objects.filter(action="notice.deleted").exists())

    def test_status_checks_note_changes(self):
        first = record_status(
            self.matter,
            receipt_number="ioe0912345678",
            status_title="Case Was Received",
            actor=self.user,
        )
        self.assertFalse(first.changed)
        same = record_status(self.matter, receipt_number=RECEIPT, status_title="Case Was Received")
        self.assertFalse(same.changed)
        changed = record_status(
            self.matter,
            receipt_number=RECEIPT,
            status_title="Case Was Approved",
            status_text="Fictional approval text.",
        )
        self.assertTrue(changed.changed)
        subjects = list(
            Activity.objects.filter(kind=Activity.Kind.STATUS).values_list("subject", flat=True)
        )
        self.assertEqual(len(subjects), 2)
        self.assertIn(f"USCIS status for {RECEIPT}: Case Was Approved", subjects)
        other = record_status(
            self.matter, receipt_number="EAC2190012345", status_title="Case Was Received"
        )
        self.assertEqual({c.pk for c in latest_statuses(self.matter)}, {changed.pk, other.pk})
        self.assertEqual(str(changed), f"{RECEIPT}: Case Was Approved")
        with self.assertRaises(ValidationError):
            record_status(self.matter, receipt_number="bad", status_title="x")


class NoticeViewTests(TestCase):
    def setUp(self):
        self.admin = make_user()
        self.staff = make_user("paralegal", superuser=False)
        self.contact = make_contact(self.admin)
        self.matter = make_matter(self.contact, self.admin)
        self.client.force_login(self.staff)

    def test_matter_panel_and_nav(self):
        page = self.client.get(reverse("matter_detail", args=[self.matter.pk]))
        self.assertContains(page, "USCIS notices")
        self.assertContains(page, f"/notices/new/?matter={self.matter.pk}")
        self.assertContains(
            page, "Connect your case status source to check receipt numbers automatically."
        )
        self.assertContains(page, 'href="/notices/"')

    def test_log_edit_and_list(self):
        spouse = make_contact(first_name="Omar", email="omar@example.test")
        MatterParty.objects.create(matter=self.matter, contact=spouse, role="Beneficiary")
        form_page = self.client.get(reverse("notice_create"), {"matter": self.matter.pk})
        contact_choices = [str(c) for c in form_page.context["form"].fields["contact"].queryset]
        self.assertEqual(sorted(contact_choices), sorted([str(self.contact), str(spouse)]))
        response = self.client.post(
            reverse("notice_create"),
            {
                "matter": str(self.matter.pk),
                "kind": "rfe",
                "form_type": "I-130",
                "receipt_number": "IOE-0912345678",
                "notice_date": "2026-03-02",
                "response_due_date": "2026-06-01",
                "contact": str(spouse.pk),
            },
        )
        self.assertRedirects(
            response,
            reverse("matter_detail", args=[self.matter.pk]) + "#notices",
            fetch_redirect_response=False,
        )
        notice = CaseNotice.objects.get()
        self.assertEqual((notice.receipt_number, notice.contact), (RECEIPT, spouse))
        self.assertContains(self.client.get(reverse("work_list")), "response due")

        bad = self.client.post(
            reverse("notice_update", args=[notice.pk]),
            {"kind": "rfe", "receipt_number": "nope", "notice_date": "2026-03-02"},
        )
        self.assertContains(bad, "three letters followed by ten digits")
        self.client.post(
            reverse("notice_update", args=[notice.pk]),
            {
                "kind": "rfe",
                "receipt_number": RECEIPT,
                "notice_date": "2026-03-02",
                "response_due_date": "2026-06-15",
            },
        )
        notice.refresh_from_db()
        self.assertEqual(
            timezone.localtime(notice.deadline.scheduled_for).date(), date(2026, 6, 15)
        )

        listing = self.client.get(reverse("notice_list"))
        self.assertContains(listing, RECEIPT)
        self.assertContains(self.client.get(reverse("notice_list"), {"open": "responses"}), RECEIPT)
        self.assertNotContains(
            self.client.get(reverse("notice_list"), {"kind": "approval"}), RECEIPT
        )
        self.assertContains(
            self.client.get(reverse("notice_list"), {"q": "zzz"}), "No notices found"
        )

    def test_documents_are_limited_to_the_matter(self):
        other_matter = make_matter(make_contact(first_name="Lena", email="lena@example.test"))
        form_page = self.client.get(reverse("notice_create"), {"matter": self.matter.pk})
        self.assertEqual(list(form_page.context["form"].fields["document"].queryset), [])
        self.assertNotEqual(other_matter.pk, self.matter.pk)

    def test_delete_is_superuser_only(self):
        notice = save_notice(
            CaseNotice(matter=self.matter, kind="approval", notice_date=date(2026, 3, 2)),
            self.admin,
        )
        url = reverse("notice_delete", args=[notice.pk])
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.admin)
        self.assertContains(
            self.client.get(reverse("notice_update", args=[notice.pk])), "Delete notice"
        )
        self.client.post(url)
        self.assertFalse(CaseNotice.objects.exists())

    def test_record_status_from_the_matter_page(self):
        url = reverse("status_record", args=[self.matter.pk])
        response = self.client.post(
            url,
            {
                "receipt_number": RECEIPT,
                "status_title": "Case Was Received",
                "checked_at": "2026-03-05T09:00",
            },
            follow=True,
        )
        self.assertContains(response, "Status recorded.")
        self.assertContains(response, "Case Was Received")
        response = self.client.post(
            url,
            {
                "receipt_number": RECEIPT,
                "status_title": "Case Was Approved",
                "checked_at": "2026-04-05T09:00",
            },
            follow=True,
        )
        self.assertContains(response, "Status changed since the last check.")
        response = self.client.post(
            url,
            {"receipt_number": "bad", "status_title": "x", "checked_at": "2026-04-05T09:00"},
            follow=True,
        )
        self.assertContains(response, "three letters followed by ten digits")
        self.assertEqual(CaseStatusCheck.objects.count(), 2)
        self.assertEqual(CaseStatusCheck.objects.first().recorded_by, self.staff)
