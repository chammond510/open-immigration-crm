import tempfile
from datetime import UTC, datetime
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse

from crm.connect.models import Connection, OutboundRequest
from crm.connect.records import SignatureRecord
from crm.connect.services import acknowledge_data_flows, create_connection
from crm.connect.signals import records_received
from crm.models import Activity, AuditLog, Document, MatterParty
from crm.signatures import services
from crm.signatures.models import SignatureRequest
from crm.tests.factories import make_contact, make_matter, make_user

NOW = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
SIGNED_PDF = b"%PDF-1.7 fictional signed copy"


class SignatureTestCase(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=Path(directory.name)))
        self.admin = make_user()
        self.staff = make_user("paralegal", superuser=False)
        self.client_contact = make_contact(self.staff, first_name="Lena", last_name="Fictional")
        self.spouse = make_contact(self.staff, first_name="Omar", last_name="Fictional")
        self.no_email = make_contact(self.staff, first_name="Ana", email="")
        self.matter = make_matter(self.client_contact, self.staff)
        MatterParty.objects.create(matter=self.matter, contact=self.spouse, role="Spouse")
        MatterParty.objects.create(matter=self.matter, contact=self.no_email, role="Child")
        self.pdf = self.document("Fee agreement", "fee-agreement.pdf", b"%PDF-1.7 fictional")
        self.docx = self.document("Notes", "notes.docx", b"PK fictional", mime="application/x")
        self.connection = create_connection(
            "example_esignature",
            label="Firm signing",
            created_by=self.admin,
            capabilities=["esignature"],
            credentials={"api_key": "example-fictional"},
        )
        acknowledge_data_flows(self.connection, self.admin)
        Connection.objects.filter(pk=self.connection.pk).update(preview_mode=False)
        self.connection.refresh_from_db()
        self.client.force_login(self.staff)

    def document(self, title, filename, content, mime="application/pdf"):
        document = Document(
            matter=self.matter,
            contact=self.client_contact,
            title=title,
            category=Document.Category.IMMIGRATION,
            original_filename=filename,
            mime_type=mime,
            size_bytes=len(content),
        )
        document.file.save(filename, ContentFile(content), save=False)
        document.save()
        return document

    def send(self, **overrides):
        data = {
            "document": str(self.pdf.pk),
            "title": "",
            "signers": [str(self.client_contact.pk), str(self.spouse.pk)],
            "message": "Please sign by Friday.",
            "connection": str(self.connection.pk),
            **overrides,
        }
        return self.client.post(reverse("signature_send", args=[self.matter.pk]), data, follow=True)

    def report(self, status, **fields):
        sent = SignatureRequest.objects.get()
        records_received.send(
            sender=None,
            connection=self.connection,
            capability="esignature",
            records=[
                SignatureRecord(
                    external_id=fields.pop("external_id", sent.external_id),
                    occurred_at=NOW,
                    status=status,
                    **fields,
                )
            ],
            source="webhook",
        )
        sent.refresh_from_db()
        return sent


class SendTests(SignatureTestCase):
    def test_the_matter_page_offers_pdfs_and_contacts_with_email(self):
        page = self.client.get(reverse("matter_detail", args=[self.matter.pk]))
        self.assertContains(page, "Send a document for signature")
        self.assertContains(page, "Fee agreement")
        self.assertContains(page, "Omar Fictional (omar@example.test)")
        panel = page.content.decode().split('id="signatures"')[1].split("</section>")[0]
        self.assertIn(f'value="{self.pdf.pk}"', panel)
        self.assertNotIn(f'value="{self.docx.pk}"', panel)
        self.assertNotIn(f'value="{self.no_email.pk}"', panel)

    def test_sending_records_the_request(self):
        response = self.send()
        self.assertContains(response, "Sent for signature through Firm signing.")
        sent = SignatureRequest.objects.get()
        self.assertEqual(
            (sent.title, sent.status, sent.document, sent.sent_by),
            ("Fee agreement", "sent", self.pdf, self.staff),
        )
        self.assertTrue(sent.external_id.startswith("example-"))
        self.assertEqual(
            [(s["name"], s["email"], s["status"]) for s in sent.signers],
            [
                ("Lena Fictional", "lena@example.test", "pending"),
                ("Omar Fictional", "omar@example.test", "pending"),
            ],
        )
        request = OutboundRequest.objects.get()
        self.assertEqual(request.summary, "Document sent for signature to 2 signer(s)")
        self.assertTrue(
            Activity.objects.filter(subject="Sent for signature: Fee agreement").exists()
        )
        self.assertTrue(AuditLog.objects.filter(action="signature.sent").exists())
        self.assertContains(response, "Out for signature")
        self.assertContains(response, "Cancel")

    def test_what_is_not_sent(self):
        response = self.send(signers=[])
        self.assertContains(response, "Choose a PDF, at least one signer")
        response = self.send(title="Please refuse this")
        self.assertContains(
            response,
            "Nothing was sent: The example e-signature service refused the document.",
        )
        Connection.objects.filter(pk=self.connection.pk).update(preview_mode=True)
        response = self.send()
        self.assertContains(response, "Nothing was sent: the e-signature service is in preview")
        self.assertFalse(SignatureRequest.objects.exists())
        with self.assertRaisesMessage(ValidationError, "Only PDF documents"):
            services.send(
                self.connection,
                self.docx,
                signers=[self.spouse],
                title="",
                message="",
                actor=self.staff,
            )
        with self.assertRaisesMessage(ValidationError, "between 1 and 10"):
            services.send(
                self.connection,
                self.pdf,
                signers=[self.spouse] * 11,
                title="",
                message="",
                actor=self.staff,
            )
        self.pdf.file.delete(save=False)
        with self.assertRaisesMessage(ValidationError, "could not be read"):
            services.send(
                self.connection,
                self.pdf,
                signers=[self.spouse],
                title="",
                message="",
                actor=self.staff,
            )

    def test_without_a_connection_the_page_offers_one(self):
        self.connection.delete()
        page = self.client.get(reverse("matter_detail", args=[self.matter.pk]))
        self.assertContains(page, "Connect your e-signature service")
        self.assertNotContains(page, "Send a document for signature")


class ProgressTests(SignatureTestCase):
    def setUp(self):
        super().setUp()
        self.send(title="Retainer")

    def test_signing_progress_and_the_signed_copy(self):
        sent = self.report("sent")
        self.assertEqual(sent.status, "sent")
        sent = self.report("signed", signer_email="LENA@example.test")
        self.assertEqual([s["status"] for s in sent.signers], ["signed", "pending"])
        sent = self.report("completed", signed_pdf=SIGNED_PDF)
        self.assertEqual(sent.status, "completed")
        self.assertIsNotNone(sent.finished_at)
        signed = sent.signed_document
        self.assertEqual(
            (signed.title, signed.matter, signed.contact, signed.category),
            ("Retainer (signed)", self.matter, self.client_contact, "immigration"),
        )
        self.assertEqual(signed.mime_type, "application/pdf")
        with signed.file.open("rb") as handle:
            self.assertEqual(handle.read(), SIGNED_PDF)
        self.assertEqual(signed.original_filename, "retainer-signed.pdf")
        self.assertTrue(Activity.objects.filter(subject="Signed and filed: Retainer").exists())
        page = self.client.get(reverse("matter_detail", args=[self.matter.pk]))
        self.assertContains(page, "Retainer (signed)")
        self.assertContains(page, "Lena Fictional (signed), Omar Fictional (signed)")
        # A late or repeated report changes nothing.
        self.report("declined")
        self.report("completed", signed_pdf=SIGNED_PDF)
        self.assertEqual(Document.objects.filter(title="Retainer (signed)").count(), 1)

    def test_declined_cancelled_and_unknown_reports(self):
        self.report("completed", external_id="not-sent-here", signed_pdf=SIGNED_PDF)
        self.report("completed")  # No PDF: not applied.
        self.assertEqual(SignatureRequest.objects.get().status, "sent")
        sent = self.report("declined", signer_email="omar@example.test")
        self.assertEqual((sent.status, sent.signers[1]["status"]), ("declined", "declined"))
        self.assertTrue(Activity.objects.filter(subject="Signature declined: Retainer").exists())
        SignatureRequest.objects.update(status="sent")
        self.assertEqual(self.report("cancelled").status, "cancelled")

    def test_staff_can_cancel_an_open_request(self):
        sent = SignatureRequest.objects.get()
        url = reverse("signature_cancel", args=[sent.pk])
        response = self.client.post(url, follow=True)
        self.assertContains(response, "Signature request cancelled.")
        sent.refresh_from_db()
        self.assertEqual(sent.status, "cancelled")
        self.assertEqual(OutboundRequest.objects.first().summary, "Signature request cancelled")
        response = self.client.post(url, follow=True)
        self.assertContains(response, "Only a request that is out for signature")
        SignatureRequest.objects.update(status="sent")
        Connection.objects.filter(pk=self.connection.pk).update(preview_mode=True)
        response = self.client.post(url, follow=True)
        self.assertContains(response, "Nothing was cancelled")
