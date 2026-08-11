import tempfile
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.models import (
    Activity,
    AuditLog,
    ChecklistItem,
    Contact,
    Document,
    IntakeForm,
    IntakeInvite,
    IntakeSubmission,
    Matter,
    MatterParty,
    TimeEntry,
    WorkItem,
)

from .factories import make_contact, make_matter, make_user


class AuthenticationTests(TestCase):
    def test_health_endpoint_is_minimal_and_public(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")

    def test_anonymous_redirected_and_nonstaff_forbidden_from_workspace(self):
        user = make_user("notstaff", superuser=False)
        user.is_staff = False
        user.save()
        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, f"{reverse('login')}?next=/")
        self.client.force_login(user)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)

    def test_security_headers_on_login(self):
        make_user()
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("default-src 'self'", response["Content-Security-Policy"])
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response["Permissions-Policy"], "camera=(), microphone=(), geolocation=()")
        self.assertContains(response, "Source code · AGPL-3.0 · No warranty")

    def test_authenticated_user_can_render_login_form(self):
        user = make_user()
        self.client.force_login(user)
        response = self.client.get(reverse("login"))
        self.assertContains(response, "Sign in")
        self.assertNotContains(response, "Primary navigation")


class WorkspaceViewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.contact = make_contact(
            self.user,
            phone="+1 555 0100",
            a_number="A000000001",
            stage=Contact.Stage.ACTIVE,
        )
        self.matter = make_matter(
            self.contact,
            self.user,
            receipt_number="IOE0000000001",
        )

    def test_core_pages_render(self):
        urls = [
            reverse("dashboard"),
            reverse("contact_list"),
            reverse("pipeline"),
            reverse("contact_detail", args=[self.contact.pk]),
            reverse("matter_list"),
            reverse("matter_detail", args=[self.matter.pk]),
            reverse("work_list"),
            reverse("intake_workspace"),
            reverse("firm_settings"),
            reverse("search") + "?q=Amina",
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)

    def test_superuser_can_render_django_admin(self):
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Site administration")

    def test_malformed_optional_query_ids_do_not_error(self):
        urls = [
            reverse("matter_create") + "?contact=not-a-uuid",
            reverse("work_create") + "?matter=not-a-uuid",
            reverse("work_create") + "?contact=%27%22",
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)

    def test_create_and_update_contact(self):
        create = self.client.post(
            reverse("contact_create"),
            {
                "first_name": "Lucas",
                "last_name": "Sample",
                "email": "lucas@example.test",
                "status": Contact.Status.PROSPECT,
                "stage": Contact.Stage.CONSULTATION,
                "assigned_to": self.user.pk,
            },
        )
        created = Contact.objects.get(email="lucas@example.test")
        self.assertRedirects(create, reverse("contact_detail", args=[created.pk]))
        self.assertTrue(created.activities.filter(subject="Contact created").exists())
        update = self.client.post(
            reverse("contact_update", args=[created.pk]),
            {
                "first_name": "Lucas",
                "last_name": "Sample",
                "email": "lucas@example.test",
                "status": Contact.Status.CLIENT,
                "stage": Contact.Stage.RETAINED,
                "assigned_to": self.user.pk,
            },
        )
        self.assertEqual(update.status_code, 302)
        self.assertTrue(created.activities.filter(kind=Activity.Kind.STATUS).exists())
    def test_create_update_and_party_matter(self):
        response = self.client.post(
            reverse("matter_create"),
            {
                "primary_contact": self.contact.pk,
                "title": "Naturalization example",
                "case_type": Matter.CaseType.NATURALIZATION,
                "status": Matter.Status.OPEN,
                "assigned_to": self.user.pk,
            },
        )
        matter = Matter.objects.get(title="Naturalization example")
        self.assertRedirects(response, reverse("matter_detail", args=[matter.pk]))
        update = self.client.post(
            reverse("matter_update", args=[matter.pk]),
            {
                "primary_contact": self.contact.pk,
                "title": matter.title,
                "case_type": matter.case_type,
                "status": Matter.Status.PENDING,
                "assigned_to": self.user.pk,
            },
        )
        self.assertEqual(update.status_code, 302)
        other = make_contact(self.user, first_name="Sam", last_name="Relative")
        self.client.post(
            reverse("matter_add_party", args=[matter.pk]),
            {"contact": other.pk, "role": "Derivative beneficiary", "notes": "Fictional"},
        )
        party = MatterParty.objects.get(matter=matter, contact=other)
        self.client.post(reverse("matter_remove_party", args=[party.pk]))
        self.assertFalse(MatterParty.objects.filter(pk=party.pk).exists())

    def test_activity_work_and_checklist_flows(self):
        activity_data = {
            "kind": Activity.Kind.CALL,
            "subject": "Discussed next steps",
            "body": "Fictional note",
            "occurred_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
        }
        self.client.post(reverse("contact_add_activity", args=[self.contact.pk]), activity_data)
        self.client.post(reverse("matter_add_activity", args=[self.matter.pk]), activity_data)
        self.assertEqual(Activity.objects.filter(subject="Discussed next steps").count(), 2)

        response = self.client.post(
            reverse("work_create"),
            {
                "kind": WorkItem.Kind.DEADLINE,
                "title": "File fictional response",
                "priority": WorkItem.Priority.HIGH,
                "contact": self.contact.pk,
                "matter": self.matter.pk,
                "assigned_to": self.user.pk,
                "scheduled_for": (timezone.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M"),
            },
        )
        self.assertRedirects(response, reverse("work_list"))
        item = WorkItem.objects.get(title="File fictional response")
        self.client.post(reverse("work_toggle", args=[item.pk]))
        item.refresh_from_db()
        self.assertTrue(item.is_completed)
        unsafe_redirect = self.client.post(
            reverse("work_toggle", args=[item.pk]),
            {"next": "https://example.invalid/phishing"},
        )
        self.assertRedirects(unsafe_redirect, reverse("work_list"))

        self.client.post(
            reverse("checklist_add", args=[self.matter.pk]),
            {"title": "Attorney review", "description": "Firm approved step", "position": 10},
        )
        checklist = ChecklistItem.objects.get(matter=self.matter)
        self.client.post(reverse("checklist_toggle", args=[checklist.pk]))
        checklist.refresh_from_db()
        self.assertTrue(checklist.is_completed)
        self.assertEqual(checklist.completed_by, self.user)

    def test_filters_search_and_csv_export(self):
        WorkItem.objects.create(title="Overdue", contact=self.contact, scheduled_for=timezone.now())
        responses = [
            self.client.get(reverse("contact_list"), {"q": "A000", "stage": "active"}),
            self.client.get(reverse("matter_list"), {"q": "IOE", "status": "open"}),
            self.client.get(
                reverse("work_list"), {"kind": "task", "owner": "mine", "completed": "1"}
            ),
            self.client.get(reverse("search"), {"q": self.matter.matter_number}),
        ]
        for response in responses:
            self.assertEqual(response.status_code, 200)
        export = self.client.get(reverse("export_csv"))
        self.assertEqual(export.status_code, 200)
        self.assertIn("text/csv", export["Content-Type"])
        self.assertIn(self.matter.matter_number.encode(), export.content)
        self.assertTrue(AuditLog.objects.filter(action="data.exported").exists())

    def test_csv_export_neutralizes_spreadsheet_formulas(self):
        make_contact(
            self.user,
            first_name="=HYPERLINK",
            last_name="Example",
            email="formula@example.test",
            phone="+15550199",
        )
        export = self.client.get(reverse("export_csv"))
        self.assertIn(b"'=HYPERLINK Example", export.content)
        self.assertIn(b"'+15550199", export.content)


class DocumentViewTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=Path(self.temp_dir.name))
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.temp_dir.cleanup)
        self.user = make_user()
        self.client.force_login(self.user)
        self.contact = make_contact(self.user)
        self.matter = make_matter(self.contact, self.user)

    def test_upload_download_and_delete_document(self):
        response = self.client.post(
            reverse("document_upload", args=[self.matter.pk]),
            {
                "title": "Fictional filing",
                "category": Document.Category.IMMIGRATION,
                "contact": self.contact.pk,
                "file": SimpleUploadedFile("sample.pdf", b"%PDF-1.7\nfictional"),
            },
        )
        self.assertRedirects(response, reverse("matter_detail", args=[self.matter.pk]))
        document = Document.objects.get()
        download = self.client.get(reverse("document_download", args=[document.pk]))
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download["Cache-Control"], "private, no-store")
        self.assertIn("attachment", download["Content-Disposition"])
        self.assertEqual(b"".join(download.streaming_content), b"%PDF-1.7\nfictional")
        stored = Path(document.file.path)
        self.client.post(reverse("document_delete", args=[document.pk]))
        self.assertFalse(Document.objects.exists())
        self.assertFalse(stored.exists())

    def test_rejects_document_for_unlinked_contact(self):
        other = make_contact(self.user, first_name="Other", last_name="Person")
        self.client.post(
            reverse("document_upload", args=[self.matter.pk]),
            {
                "title": "Wrong contact",
                "category": Document.Category.OTHER,
                "contact": other.pk,
                "file": SimpleUploadedFile("sample.pdf", b"%PDF-1.7\nfictional"),
            },
        )
        self.assertFalse(Document.objects.exists())


class IntakeViewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.contact = make_contact(self.user)
        self.matter = make_matter(self.contact, self.user)
        self.intake_form = IntakeForm.objects.create(
            name="Our questions",
            instructions="Firm supplied",
            questions=[{"key": "q1-goal", "label": "What is your goal?"}],
        )

    def test_create_edit_invite_submit_and_review(self):
        created = self.client.post(
            reverse("intake_form_create"),
            {
                "name": "New blank form",
                "questions_text": "First question?\nSecond question?",
                "is_active": "on",
            },
        )
        self.assertRedirects(created, reverse("intake_workspace"))
        new_form = IntakeForm.objects.get(name="New blank form")
        edited = self.client.post(
            reverse("intake_form_update", args=[new_form.pk]),
            {
                "name": "Edited firm form",
                "questions_text": "Only question?",
                "is_active": "on",
            },
        )
        self.assertRedirects(edited, reverse("intake_workspace"))

        response = self.client.post(
            reverse("intake_invite_create"),
            {
                "intake_form": self.intake_form.pk,
                "contact": self.contact.pk,
                "matter": self.matter.pk,
                "expires_in_days": 7,
            },
        )
        self.assertEqual(response.status_code, 200)
        url = response.context["intake_url"]
        token = url.rstrip("/").rsplit("/", 1)[-1]
        self.client.logout()
        opened = self.client.get(reverse("public_intake", args=[token]))
        self.assertEqual(opened.status_code, 200)
        self.assertContains(opened, "What is your goal?")
        invite = IntakeInvite.objects.get()
        invite.refresh_from_db()
        self.assertIsNotNone(invite.opened_at)
        submitted = self.client.post(
            reverse("public_intake", args=[token]),
            {"email": "updated@example.test", "phone": "555", "q1-goal": "A fictional answer"},
        )
        self.assertContains(submitted, "Thank you")
        self.assertEqual(IntakeSubmission.objects.count(), 1)
        used_again = self.client.get(reverse("public_intake", args=[token]))
        self.assertEqual(used_again.status_code, 410)

        self.client.force_login(self.user)
        submission = IntakeSubmission.objects.get()
        detail = self.client.get(reverse("intake_submission_detail", args=[submission.pk]))
        self.assertContains(detail, "A fictional answer")
        self.client.post(reverse("intake_submission_review", args=[submission.pk]))
        submission.refresh_from_db()
        self.assertEqual(submission.reviewed_by, self.user)

    def test_authenticated_staff_can_preview_public_intake(self):
        invite, token = IntakeInvite.issue(
            intake_form=self.intake_form,
            contact=self.contact,
            matter=self.matter,
            created_by=self.user,
            expires_at=timezone.now() + timedelta(days=1),
        )
        response = self.client.get(reverse("public_intake", args=[token]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "What is your goal?")
        self.assertNotContains(response, "Primary navigation")

    def test_revoke_and_expired_invites_are_unavailable(self):
        invite, token = IntakeInvite.issue(
            intake_form=self.intake_form,
            contact=self.contact,
            matter=self.matter,
            created_by=self.user,
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.client.post(reverse("intake_invite_revoke", args=[invite.pk]))
        self.client.logout()
        self.assertEqual(self.client.get(reverse("public_intake", args=[token])).status_code, 410)
        expired, expired_token = IntakeInvite.issue(
            intake_form=self.intake_form,
            contact=self.contact,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.assertFalse(expired.is_active)
        self.assertEqual(
            self.client.get(reverse("public_intake", args=[expired_token])).status_code,
            410,
        )


class TimeTrackingTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.contact = make_contact(self.user)
        self.matter = make_matter(self.contact, self.user)

    def test_start_switch_and_stop_timer(self):
        response = self.client.post(reverse("timer_start"), {"matter": str(self.matter.pk)})
        self.assertRedirects(response, reverse("matter_detail", args=[self.matter.pk]))
        entry = TimeEntry.objects.get(user=self.user, stopped_at__isnull=True)
        self.assertEqual(entry.matter, self.matter)
        self.assertEqual(entry.contact, self.contact)
        dashboard = self.client.get(reverse("dashboard"))
        self.assertContains(dashboard, "data-timer-started")
        other = make_contact(self.user, first_name="Lina", last_name="Sample")
        self.client.post(reverse("timer_start"), {"contact": str(other.pk)})
        entry.refresh_from_db()
        self.assertIsNotNone(entry.stopped_at)
        self.assertEqual(
            TimeEntry.objects.filter(user=self.user, stopped_at__isnull=True).count(), 1
        )
        self.client.post(reverse("timer_stop"), {"note": "Drafted declaration"})
        self.assertFalse(TimeEntry.objects.filter(stopped_at__isnull=True).exists())
        self.assertTrue(TimeEntry.objects.filter(note="Drafted declaration").exists())
        self.assertTrue(AuditLog.objects.filter(action="timer.started").exists())
        self.assertTrue(AuditLog.objects.filter(action="timer.stopped").exists())

    def test_timer_requires_target_and_staff(self):
        response = self.client.post(reverse("timer_start"), {})
        self.assertRedirects(response, reverse("dashboard"))
        self.assertFalse(TimeEntry.objects.exists())
        self.client.logout()
        response = self.client.post(reverse("timer_start"), {"matter": str(self.matter.pk)})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)
        self.assertFalse(TimeEntry.objects.exists())

    def test_one_running_timer_per_user_constraint(self):
        TimeEntry.objects.create(matter=self.matter, contact=self.contact, user=self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TimeEntry.objects.create(matter=self.matter, contact=self.contact, user=self.user)

    def test_matter_page_shows_time_total(self):
        TimeEntry.objects.create(
            matter=self.matter,
            contact=self.contact,
            user=self.user,
            started_at=timezone.now() - timedelta(minutes=90),
            stopped_at=timezone.now(),
        )
        response = self.client.get(reverse("matter_detail", args=[self.matter.pk]))
        self.assertContains(response, "total 1:30")


class FirstRunSetupTests(TestCase):
    def test_login_redirects_to_setup_when_no_accounts_exist(self):
        response = self.client.get(reverse("login"))
        self.assertRedirects(response, reverse("first_run_setup"))

    def test_setup_creates_superuser_once_then_disables(self):
        response = self.client.post(
            reverse("first_run_setup"),
            {
                "username": "founder",
                "email": "founder@example.test",
                "password1": "First-Setup-Password-88!",
                "password2": "First-Setup-Password-88!",
            },
        )
        self.assertRedirects(response, reverse("dashboard"))
        User = get_user_model()
        user = User.objects.get(username="founder")
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(
            AuditLog.objects.filter(action="user.first_admin_created", user=user).exists()
        )
        self.client.logout()
        self.assertRedirects(self.client.get(reverse("first_run_setup")), reverse("login"))
        response = self.client.post(
            reverse("first_run_setup"),
            {
                "username": "second",
                "password1": "Second-Setup-Password-88!",
                "password2": "Second-Setup-Password-88!",
            },
        )
        self.assertRedirects(response, reverse("login"))
        self.assertEqual(User.objects.count(), 1)

    def test_setup_rejects_weak_password(self):
        response = self.client.post(
            reverse("first_run_setup"),
            {"username": "founder", "password1": "short", "password2": "short"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.exists())


class AccountSecurityTests(TestCase):
    def test_login_lockout_after_repeated_failures(self):
        make_user("target")
        url = reverse("login")
        for _ in range(5):
            self.client.post(url, {"username": "target", "password": "wrong-password"})
        locked = self.client.post(url, {"username": "target", "password": "wrong-password"})
        self.assertEqual(locked.status_code, 429)
        still_locked = self.client.post(
            url, {"username": "target", "password": "Testing-Password-Only-42!"}
        )
        self.assertEqual(still_locked.status_code, 429)
        self.assertContains(locked, "temporarily locked", status_code=429)

    def test_password_change_flow_and_audit(self):
        user = make_user()
        self.client.force_login(user)
        response = self.client.post(
            reverse("password_change"),
            {
                "old_password": "Testing-Password-Only-42!",
                "new_password1": "Another-Testing-Password-77!",
                "new_password2": "Another-Testing-Password-77!",
            },
        )
        self.assertRedirects(response, reverse("password_change_done"))
        self.assertTrue(
            AuditLog.objects.filter(action="user.password_changed", user=user).exists()
        )
