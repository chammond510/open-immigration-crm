import tempfile
from datetime import UTC, datetime
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from django.urls import reverse

from crm.comms.models import Communication, CommunicationAttachment
from crm.comms.services import save_to_matter
from crm.connect.records import Attachment, TextRecord
from crm.connect.services import acknowledge_data_flows, create_connection
from crm.connect.signals import records_received
from crm.models import Activity, AuditLog, Document, MatterParty
from crm.tests.factories import make_contact, make_matter, make_user

AT = datetime(2026, 10, 31, 15, 0, tzinfo=UTC)
NUMBER = "+12025550144"
JPEG = b"\xff\xd8\xff\xe0fictional photo"
PDF = b"%PDF-1.4 fictional notice"


class AttachmentTestCase(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=Path(directory.name)))
        self.admin = make_user()
        self.staff = make_user("paralegal", superuser=False)
        self.contact = make_contact(self.staff, phone="(202) 555-0144")
        self.connection = create_connection(
            "example",
            label="Front desk texts",
            created_by=self.admin,
            capabilities=["texting"],
            credentials={"api_key": "example-fictional-key"},
        )
        acknowledge_data_flows(self.connection, self.admin)
        self.client.force_login(self.staff)

    def deliver(self, *attachments, external_id="in-1", body="Here it is.", remote=NUMBER):
        records_received.send(
            sender=None,
            connection=self.connection,
            capability="texting",
            records=[
                TextRecord(
                    external_id=external_id,
                    occurred_at=AT,
                    direction="inbound",
                    remote=remote,
                    body=body,
                    attachments=attachments,
                )
            ],
            source="webhook",
        )
        return Communication.objects.get(external_id=external_id)


class KeepingAttachmentsTests(AttachmentTestCase):
    def test_files_are_kept_on_the_terms_of_a_document_upload(self):
        message = self.deliver(
            Attachment("receipt notice.jpg", "image/jpeg", JPEG),
            Attachment("../../notice.pdf", "application/pdf", PDF),
            Attachment("voice.ogg", "audio/ogg", b"OggS fictional"),
            Attachment("fake.pdf", "application/pdf", JPEG),
            Attachment("", "application/pdf", PDF),
        )
        kept = list(message.attachments.all())
        self.assertEqual(
            [(a.original_filename, a.mime_type, a.size_bytes) for a in kept],
            [
                ("receipt notice.jpg", "image/jpeg", len(JPEG)),
                ("notice.pdf", "application/pdf", len(PDF)),
            ],
        )
        with kept[0].file.open("rb") as handle:
            self.assertEqual(handle.read(), JPEG)
        self.assertEqual(str(kept[0]), "receipt notice.jpg")
        # Stored under the CRM's own name, never the sender's.
        self.assertNotIn("receipt", kept[0].file.name)
        self.assertTrue(kept[0].file.name.startswith("communications/"))
        self.assertEqual(
            message.summary,
            "Here it is.\n\n"
            "[voice.ogg was not kept. Allowed file types: PDF, DOCX, PNG, JPG, and JPEG.]\n\n"
            "[fake.pdf was not kept. The file contents do not match its extension.]\n\n"
            "[attachment was not kept. Allowed file types: PDF, DOCX, PNG, JPG, and JPEG.]",
        )
        # A repeat of the same message keeps nothing twice.
        self.deliver(Attachment("receipt notice.jpg", "image/jpeg", JPEG))
        self.assertEqual(CommunicationAttachment.objects.count(), 2)

    @override_settings(DOCUMENT_UPLOAD_MAX_BYTES=10)
    def test_files_over_the_upload_limit_are_not_kept(self):
        message = self.deliver(Attachment("notice.pdf", "application/pdf", PDF))
        self.assertFalse(message.attachments.exists())
        self.assertIn(
            "[notice.pdf was not kept. The file exceeds the 0 MB limit.]", message.summary
        )

    def test_deleting_the_message_deletes_its_files(self):
        message = self.deliver(Attachment("notice.pdf", "application/pdf", PDF))
        name = message.attachments.get().file.name
        self.assertTrue(default_storage.exists(name))
        with self.captureOnCommitCallbacks(execute=True):
            message.delete()
        self.assertFalse(default_storage.exists(name))


class AttachmentPageTests(AttachmentTestCase):
    def setUp(self):
        super().setUp()
        self.matter = make_matter(self.contact, self.staff, title="Family petition")
        self.message = self.deliver(Attachment("notice.pdf", "application/pdf", PDF))
        self.attachment = self.message.attachments.get()
        self.save_url = reverse("communication_attachment_save", args=[self.attachment.pk])

    def test_staff_download_files_from_the_message(self):
        page = self.client.get(reverse("contact_detail", args=[self.contact.pk]))
        download = reverse("communication_attachment_download", args=[self.attachment.pk])
        self.assertContains(page, f'href="{download}">notice.pdf</a>')
        self.assertContains(page, "Save to a matter")
        listed = self.client.get(reverse("communication_list"))
        self.assertContains(listed, download)
        response = self.client.get(download)
        self.assertEqual(b"".join(response.streaming_content), PDF)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertTrue(
            AuditLog.objects.filter(action="communication_attachment.downloaded").exists()
        )
        self.attachment.file.delete(save=False)
        self.assertEqual(self.client.get(download).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(download).status_code, 302)

    def test_a_copy_is_saved_to_one_of_the_contacts_matters(self):
        page = self.client.get(self.save_url, {"matter": self.matter.pk})
        self.assertContains(page, "Save notice.pdf to a matter")
        self.assertContains(page, f'<option value="{self.matter.pk}" selected>')
        self.assertContains(page, 'value="notice"')
        response = self.client.post(
            self.save_url,
            {"matter": self.matter.pk, "title": "Receipt notice", "category": "immigration"},
            follow=True,
        )
        self.assertRedirects(response, reverse("matter_detail", args=[self.matter.pk]))
        self.assertContains(response, f"Saved to {self.matter} as a document.")
        document = Document.objects.get()
        self.assertEqual(
            (document.matter, document.contact, document.title, document.category),
            (self.matter, self.contact, "Receipt notice", "immigration"),
        )
        self.assertEqual(
            (document.original_filename, document.uploaded_by), ("notice.pdf", self.staff)
        )
        with document.file.open("rb") as handle:
            self.assertEqual(handle.read(), PDF)
        self.assertNotEqual(document.file.name, self.attachment.file.name)
        self.assertTrue(
            Activity.objects.filter(
                subject="Document saved from a message: Receipt notice"
            ).exists()
        )
        self.attachment.refresh_from_db()
        self.assertEqual(self.attachment.saved_document, document)
        page = self.client.get(reverse("contact_detail", args=[self.contact.pk]))
        self.assertContains(page, f"Saved to {self.matter.matter_number}")
        self.assertContains(self.client.get(self.save_url), "A copy was saved to")

    def test_only_the_contacts_own_matters(self):
        stranger = make_contact(first_name="Noor", email="noor@example.test", phone="+12025550145")
        theirs = make_matter(stranger, self.staff, title="Someone else's case")
        response = self.client.post(
            self.save_url, {"matter": theirs.pk, "title": "Notice", "category": "other"}
        )
        self.assertContains(response, "Select a valid choice.")
        self.assertFalse(Document.objects.exists())
        # A matter the contact is a party to counts.
        MatterParty.objects.create(matter=theirs, contact=self.contact, role="beneficiary")
        response = self.client.post(
            self.save_url, {"matter": theirs.pk, "title": "Notice", "category": "other"}
        )
        self.assertEqual(Document.objects.get().matter, theirs)
        with self.assertRaisesMessage(ValidationError, "one of the contact's matters"):
            save_to_matter(
                self.attachment,
                make_matter(stranger, self.staff),
                title="Notice",
                category="other",
                actor=self.staff,
            )

    def test_without_a_contact_or_a_matter(self):
        self.matter.delete()
        self.assertContains(self.client.get(self.save_url), "has no matters yet")
        unmatched = self.deliver(
            Attachment("notice.pdf", "application/pdf", PDF),
            external_id="in-2",
            remote="+12025550199",
        )
        attachment = unmatched.attachments.get()
        page = self.client.get(reverse("communication_attachment_save", args=[attachment.pk]))
        self.assertContains(page, "is not linked to a contact yet")
        self.assertContains(page, reverse("communication_match", args=[unmatched.pk]))
        listed = self.client.get(reverse("communication_list"), {"matching": "needed"})
        self.assertNotContains(listed, "Save to a matter")
