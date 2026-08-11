import io
import zipfile
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from crm.forms import IntakeFormConfigForm, IntakeInviteForm, PublicIntakeForm, WorkItemForm
from crm.models import Activity, FirmProfile, IntakeForm, IntakeInvite, Matter, WorkItem
from crm.services import add_activity, audit, client_ip
from crm.validators import validate_document

from .factories import make_contact, make_matter, make_user


class ModelTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.contact = make_contact(self.user)

    def test_firm_singleton_and_numbering(self):
        firm = FirmProfile.load()
        firm.matter_prefix = "  case  "
        firm.save()
        first = make_matter(self.contact, self.user)
        second = make_matter(self.contact, self.user, title="Second fictional matter")
        year = timezone.localdate().year
        self.assertEqual(first.matter_number, f"CASE-{year}-0001")
        self.assertEqual(second.matter_number, f"CASE-{year}-0002")
        self.assertEqual(str(first), f"{first.matter_number} · {first.title}")

        invalid = FirmProfile(pk=2, name="Second firm")
        with self.assertRaises(ValidationError):
            invalid.save()

    def test_matter_closed_at_tracks_status(self):
        matter = make_matter(self.contact, self.user, status=Matter.Status.CLOSED)
        self.assertIsNotNone(matter.closed_at)
        matter.status = Matter.Status.OPEN
        matter.save()
        self.assertIsNone(matter.closed_at)

    def test_work_item_completion_and_overdue(self):
        item = WorkItem.objects.create(
            title="Deadline",
            kind=WorkItem.Kind.DEADLINE,
            contact=self.contact,
            scheduled_for=timezone.now() - timedelta(hours=1),
        )
        self.assertTrue(item.is_overdue)
        item.is_completed = True
        item.save()
        self.assertIsNotNone(item.completed_at)
        self.assertFalse(item.is_overdue)
        item.is_completed = False
        item.save()
        self.assertIsNone(item.completed_at)

    def test_activity_requires_link(self):
        activity = Activity(subject="Unlinked")
        with self.assertRaises(ValidationError):
            activity.full_clean()

    def test_intake_token_is_hashed_and_expires(self):
        intake_form = IntakeForm.objects.create(name="Firm questions")
        invite, raw_token = IntakeInvite.issue(
            intake_form=intake_form,
            contact=self.contact,
            expires_at=timezone.now() + timedelta(days=2),
            created_by=self.user,
        )
        self.assertNotEqual(raw_token, invite.token_hash)
        self.assertEqual(IntakeInvite.hash_token(raw_token), invite.token_hash)
        self.assertTrue(invite.is_active)
        invite.revoked_at = timezone.now()
        self.assertFalse(invite.is_active)

    def test_audit_and_activity_services(self):
        request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="198.51.100.1, 203.0.113.4")
        request.user = self.user
        self.assertEqual(client_ip(request), "203.0.113.4")
        audit(request, "contact.viewed", self.contact, detail="x" * 700)
        log = self.user.open_immigration_audit_logs.get()
        self.assertEqual(log.ip_address, "203.0.113.4")
        self.assertEqual(len(log.detail), 500)
        activity = add_activity(request=request, subject="Called", contact=self.contact)
        self.assertEqual(activity.created_by, self.user)

    def test_invalid_proxy_address_is_not_persisted(self):
        request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="not-an-ip")
        request.user = self.user
        self.assertIsNone(client_ip(request))
        audit(request, "contact.viewed", self.contact)
        self.assertIsNone(self.user.open_immigration_audit_logs.get().ip_address)


class FormTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.contact = make_contact(self.user)
        self.matter = make_matter(self.contact, self.user)

    def test_work_item_requires_contact_or_matter(self):
        form = WorkItemForm(
            data={
                "kind": WorkItem.Kind.TASK,
                "title": "Unlinked",
                "priority": WorkItem.Priority.NORMAL,
                "scheduled_for": "",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Link the item", str(form.non_field_errors()))

    def test_intake_builder_creates_plain_questions(self):
        form = IntakeFormConfigForm(
            data={
                "name": "Our intake",
                "instructions": "Firm supplied",
                "questions_text": "What is your goal?\nAny deadline?",
                "is_active": "on",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(len(saved.questions), 2)
        self.assertEqual(saved.questions[0]["label"], "What is your goal?")

    def test_intake_invite_matter_must_link_contact(self):
        other = make_contact(self.user, first_name="Other", last_name="Person")
        intake_form = IntakeForm.objects.create(name="Questions")
        form = IntakeInviteForm(
            data={
                "intake_form": intake_form.pk,
                "contact": other.pk,
                "matter": self.matter.pk,
                "expires_in_days": 7,
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("not linked", str(form.non_field_errors()))

    def test_public_form_preserves_labels_with_answers(self):
        intake_form = IntakeForm.objects.create(
            name="Questions",
            questions=[{"key": "q1-goal", "label": "What is your goal?"}],
        )
        form = PublicIntakeForm(
            data={"email": "new@example.test", "phone": "555", "q1-goal": "A response"},
            intake_form=intake_form,
        )
        self.assertTrue(form.is_valid())
        self.assertEqual(form.answers["q1-goal"]["answer"], "A response")


class DocumentValidationTests(TestCase):
    def test_pdf_png_and_jpeg_magic(self):
        samples = [
            ("sample.pdf", b"%PDF-1.7\ncontent", "application/pdf"),
            ("sample.png", b"\x89PNG\r\n\x1a\ncontent", "image/png"),
            ("sample.jpg", b"\xff\xd8\xffcontent", "image/jpeg"),
        ]
        for filename, content, expected in samples:
            with self.subTest(filename=filename):
                upload = SimpleUploadedFile(filename, content)
                self.assertEqual(validate_document(upload), expected)

    def test_docx_archive_structure(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("[Content_Types].xml", "types")
            archive.writestr("word/document.xml", "document")
        upload = SimpleUploadedFile("sample.docx", output.getvalue())
        self.assertIn("wordprocessingml", validate_document(upload))

    def test_rejects_extension_mismatch_unknown_and_oversize(self):
        with self.assertRaises(ValidationError):
            validate_document(SimpleUploadedFile("malware.exe", b"MZ"))
        with self.assertRaises(ValidationError):
            validate_document(SimpleUploadedFile("fake.pdf", b"not a PDF"))
        with override_settings(DOCUMENT_UPLOAD_MAX_BYTES=3):
            with self.assertRaises(ValidationError):
                validate_document(SimpleUploadedFile("large.pdf", b"%PDF-too-large"))
