import json
import tempfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import httpx
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse

from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, WebhookEvent
from crm.connect.tasks import process_webhook_event
from crm.connect.testing import ConnectorContract
from crm.models import Document, MatterParty
from crm.signatures.models import SignatureRequest
from crm.tests.factories import make_contact, make_matter, make_user

from . import HOSTED, DocumensoConnector, page_count, validate_address

KEY = "api_fictionaltoken01"
ORIGINAL = (
    b"%PDF-1.7\n1 0 obj << /Type /Pages >>\n2 0 obj << /Type /Page >>\n3 0 obj << /Type /Page >>"
)
SIGNED = b"%PDF-1.7 fictional signed copy"


class FakeDocumenso:
    def __init__(self):
        self.envelopes = {}
        self.requests = []
        self.uploads = []
        self.signed = SIGNED

    def calls(self, method, path=""):
        return [r for r in self.requests if r.method == method and r.url.path.endswith(path)]

    def add(self, envelope_id, status="PENDING", emails=("lena@example.test",)):
        self.envelopes[envelope_id] = {
            "id": envelope_id,
            "status": status,
            "recipients": [{"email": e, "signingStatus": "NOT_SIGNED"} for e in emails],
            "envelopeItems": [{"id": f"{envelope_id}_item"}],
        }

    def __call__(self, request):
        self.requests.append(request)
        if request.headers.get("authorization") != KEY:
            return httpx.Response(401, json={"message": "Unauthorized"})
        path = request.url.path.removeprefix("/api/v2")
        if path == "/envelope/create":
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: "
                + request.headers["content-type"].encode()
                + b"\r\n\r\n"
                + request.read()
            )
            parts = {
                part.get_param("name", header="content-disposition"): part
                for part in message.iter_parts()
            }
            payload = json.loads(parts["payload"].get_content())
            self.uploads.append((payload, parts["files"].get_content()))
            envelope_id = f"envelope_{len(self.envelopes) + 1}"
            self.add(envelope_id, "DRAFT", [r["email"] for r in payload["recipients"]])
            return httpx.Response(200, json={"id": envelope_id})
        if path == "/envelope/distribute":
            envelope = self.envelopes[json.loads(request.content)["envelopeId"]]
            envelope["status"] = "PENDING"
            return httpx.Response(200, json={"success": True, "id": envelope["id"]})
        if path == "/envelope/cancel":
            envelope = self.envelopes.get(json.loads(request.content)["envelopeId"])
            if envelope is None or envelope["status"] != "PENDING":
                return httpx.Response(400, json={"message": "Not pending"})
            envelope["status"] = "CANCELLED"
            return httpx.Response(200, json={"success": True})
        if path.startswith("/envelope/item/"):
            assert request.url.params["version"] == "signed"
            headers = {"Content-Type": "application/pdf"}
            return httpx.Response(200, content=self.signed, headers=headers)
        envelope = self.envelopes.get(path.rsplit("/", 1)[-1])
        if envelope is None:
            return httpx.Response(404, json={"message": "Envelope not found"})
        return httpx.Response(200, json=envelope)

    def sign(self, envelope_id, email):
        for recipient in self.envelopes[envelope_id]["recipients"]:
            if recipient["email"] == email:
                recipient["signingStatus"] = "SIGNED"


def event(name, envelope_id):
    return json.dumps(
        {
            "event": name,
            "payload": {"id": 7, "envelopeId": envelope_id, "status": "PENDING"},
            "createdAt": f"2026-11-03T15:00:0{len(name) % 10}.000Z",
            "webhookEndpoint": "https://crm.example.test/hooks/x/",
        }
    ).encode()


class FakeDocumensoMixin:
    def setUp(self):
        super().setUp()
        self.documenso = FakeDocumenso()
        self.enterContext(http.use_transport(httpx.MockTransport(self.documenso)))


class DocumensoContractTests(FakeDocumensoMixin, ConnectorContract, TestCase):
    connector_class = DocumensoConnector
    valid_credentials = {"api_key": KEY}
    invalid_credentials = {"api_key": "api_wrong"}
    connection_config = {"address": HOSTED}
    body_is_signed = False
    outbound_examples = {
        "send_for_signature": {
            "title": "Fee agreement",
            "filename": "fee.pdf",
            "pdf": "JVBERi0xLjcgZmljdGlvbmFs",
            "signers": [{"name": "Lena Fictional", "email": "lena@example.test"}],
            "message": "",
        },
        "cancel_signature": {"document_id": "envelope_existing"},
    }

    def setUp(self):
        super().setUp()
        self.documenso.add("envelope_existing")

    def make_webhook(self, secret):
        return event("DOCUMENT_OPENED", "envelope_existing"), {"x-documenso-secret": secret}


class DocumensoFlowTests(FakeDocumensoMixin, TestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=Path(directory.name)))
        self.admin = make_user()
        self.lena = make_contact(self.admin, first_name="Lena", last_name="Fictional")
        self.omar = make_contact(self.admin, first_name="Omar", last_name="Fictional")
        self.matter = make_matter(self.lena, self.admin)
        MatterParty.objects.create(matter=self.matter, contact=self.omar, role="Spouse")
        self.pdf = Document(
            matter=self.matter,
            contact=self.lena,
            title="Fee agreement",
            original_filename="fee-agreement.pdf",
            mime_type="application/pdf",
            size_bytes=len(ORIGINAL),
        )
        self.pdf.file.save("fee-agreement.pdf", ContentFile(ORIGINAL), save=False)
        self.pdf.save()
        self.client.force_login(self.admin)
        self.client.post(
            reverse("connector_detail", args=["documenso"]),
            {
                "label": "Firm Documenso",
                "capabilities": ["esignature"],
                "credential_api_key": KEY,
                "setting_address": HOSTED,
                "acknowledge": "on",
            },
        )
        self.connection = Connection.objects.get()
        Connection.objects.filter(pk=self.connection.pk).update(preview_mode=False)
        response = self.client.post(reverse("connection_webhooks", args=[self.connection.pk]))
        self.url, self.secret = response.context["webhook_url"], response.context["secret"]
        self.assertContains(response, "Choose the events for a document being signed")

    def send(self):
        return self.client.post(
            reverse("signature_send", args=[self.matter.pk]),
            {
                "document": str(self.pdf.pk),
                "title": "Fee agreement",
                "signers": [str(self.lena.pk), str(self.omar.pk)],
                "message": "Please sign.",
                "connection": str(self.connection.pk),
            },
            follow=True,
        )

    def deliver(self, body, secret=None):
        with self.captureOnCommitCallbacks(execute=True):
            answer = self.client.post(
                self.url,
                body,
                content_type="application/json",
                headers={"x-documenso-secret": self.secret if secret is None else secret},
            )
        for queued in WebhookEvent.objects.filter(status=WebhookEvent.Status.QUEUED):
            process_webhook_event.call(queued.pk)
        return answer

    def test_a_document_is_sent_signed_and_filed(self):
        self.assertEqual(self.connection.status, Connection.Status.ACTIVE)
        self.assertContains(self.send(), "Sent for signature through Firm Documenso.")
        payload, uploaded = self.documenso.uploads[0]
        self.assertEqual(uploaded, ORIGINAL)
        self.assertEqual((payload["type"], payload["title"]), ("DOCUMENT", "Fee agreement"))
        self.assertEqual(payload["meta"]["message"], "Please sign.")
        self.assertEqual(
            [(r["email"], r["role"]) for r in payload["recipients"]],
            [("lena@example.test", "SIGNER"), ("omar@example.test", "SIGNER")],
        )
        fields = [r["fields"][0] for r in payload["recipients"]]
        self.assertEqual({f["page"] for f in fields}, {2})  # The last of two pages.
        self.assertEqual([f["positionY"] for f in fields], [85, 79])
        self.assertEqual(self.documenso.envelopes["envelope_1"]["status"], "PENDING")
        sent = SignatureRequest.objects.get()
        self.assertEqual(sent.external_id, "envelope_1")

        # Opened: nothing changes. One signer signs, then both.
        self.assertEqual(self.deliver(event("DOCUMENT_OPENED", "envelope_1")).status_code, 200)
        self.documenso.sign("envelope_1", "lena@example.test")
        self.deliver(event("DOCUMENT_SIGNED", "envelope_1"))
        sent.refresh_from_db()
        self.assertEqual([s["status"] for s in sent.signers], ["signed", "pending"])
        self.documenso.envelopes["envelope_1"]["status"] = "COMPLETED"
        self.deliver(event("DOCUMENT_COMPLETED", "envelope_1"))
        sent.refresh_from_db()
        self.assertEqual(sent.status, "completed")
        with sent.signed_document.file.open("rb") as handle:
            self.assertEqual(handle.read(), SIGNED)
        download = self.documenso.calls("GET", "/envelope/item/envelope_1_item/download")
        self.assertEqual(len(download), 1)

    def test_deliveries_without_the_secret_are_refused(self):
        self.send()
        body = event("DOCUMENT_COMPLETED", "envelope_1")
        self.assertEqual(self.deliver(body, secret="wrong").status_code, 401)
        self.assertEqual(self.deliver(body, secret="").status_code, 401)
        self.assertEqual(SignatureRequest.objects.get().status, "sent")
        # A body that only claims completion changes nothing: Documenso says pending.
        self.deliver(body)
        self.assertEqual(SignatureRequest.objects.get().status, "sent")
        self.assertEqual(self.deliver(b'{"event": "X"}').status_code, 400)

    def test_rejected_cancelled_and_gone(self):
        self.send()
        self.documenso.envelopes["envelope_1"]["status"] = "REJECTED"
        self.documenso.envelopes["envelope_1"]["recipients"][1]["signingStatus"] = "REJECTED"
        self.deliver(event("DOCUMENT_REJECTED", "envelope_1"))
        sent = SignatureRequest.objects.get()
        self.assertEqual((sent.status, sent.signers[1]["status"]), ("declined", "declined"))
        SignatureRequest.objects.update(status="sent")
        del self.documenso.envelopes["envelope_1"]
        self.deliver(event("DOCUMENT_CANCELLED", "envelope_1"))
        self.assertEqual(SignatureRequest.objects.get().status, "cancelled")

    def test_staff_cancel_through_documenso(self):
        self.send()
        sent = SignatureRequest.objects.get()
        response = self.client.post(reverse("signature_cancel", args=[sent.pk]), follow=True)
        self.assertContains(response, "Signature request cancelled.")
        self.assertEqual(self.documenso.envelopes["envelope_1"]["status"], "CANCELLED")
        SignatureRequest.objects.update(status="sent")
        response = self.client.post(reverse("signature_cancel", args=[sent.pk]), follow=True)
        self.assertContains(response, "may already be signed or cancelled")


class DocumensoCheckTests(FakeDocumensoMixin, TestCase):
    def connector(self, **credentials):
        return DocumensoConnector(
            Connection(
                connector_key="documenso",
                credentials={"api_key": KEY, **credentials},
                config={"address": "https://sign.example.test/"},
            )
        )

    def test_answers_that_are_refused(self):
        connector = self.connector()
        connector.test_connection()
        self.assertEqual(
            str(self.documenso.requests[-1].url),
            "https://sign.example.test/api/v2/envelope/envelope_crm_connection_check",
        )
        with self.assertRaisesMessage(ConnectorError, "did not accept the API token"):
            self.connector(api_key="api_wrong").test_connection()
        with self.assertRaisesMessage(ConnectorError, "cannot send_text"):
            connector.perform("send_text", {})
        with self.assertRaisesMessage(ConnectorError, "which file was signed"):
            connector.signed_pdf({"envelopeItems": []})
        self.documenso.add("envelope_9", "COMPLETED")
        self.documenso.signed = b"<html>"
        with self.assertRaisesMessage(ConnectorError, "was not a PDF"):
            connector.signed_pdf(self.documenso.envelopes["envelope_9"])

    def test_addresses_and_pages(self):
        validate_address("https://sign.example.test")
        validate_address("http://192.168.1.20:3000")
        for bad in (
            "http://sign.example.test",
            "https://sign.example.test/api/v2",
            "https://x?y=1",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                validate_address(bad)
        self.assertEqual(page_count(ORIGINAL), 2)
        self.assertEqual(page_count(b"%PDF-1.7"), 1)
