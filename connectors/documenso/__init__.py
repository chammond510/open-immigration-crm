"""Documenso: send matter documents for signature, hosted at documenso.com or self-hosted.

The firm connects with an API token from its Documenso team, and the connector uses
Documenso's API v2 envelopes (API v1 is deprecated). Sending creates the envelope
from the PDF in one request, with each signer and a signature field for them at the
bottom of the last page, then distributes it, so Documenso emails the signers.
Cancelling cancels the pending envelope.

Documenso signs no webhook: each delivery carries the team's webhook secret in the
``X-Documenso-Secret`` header, compared in constant time, and its body is trusted
for nothing but the envelope's ID. The worker then asks the API where that envelope
stands, and once it is completed, downloads the signed PDF from the API.

API: https://docs.documenso.com/docs/developers/api/documents
Webhooks: https://docs.documenso.com/docs/developers/webhooks/verification
"""

import base64
import hmac
import json
import re
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from django.core.exceptions import ValidationError

from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
    SettingField,
    WebhookEnvelope,
    WebhookSpec,
)
from crm.connect.http import ProviderClient, check_url
from crm.connect.records import SignatureRecord
from crm.connect.registry import register

HOSTED = "https://app.documenso.com"
# Documenso's own limit on a document's size, so also the signed copy the CRM accepts.
MAX_PDF_BYTES = 50 * 1024 * 1024
# A signature field near the bottom of the last page, one row per signer, in percent
# of the page from its top-left corner.
FIELD_LEFT, FIELD_TOP, FIELD_WIDTH, FIELD_HEIGHT = 10, 85, 30, 5
PAGE = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")
FINISHED = {"COMPLETED": "completed", "REJECTED": "declined", "CANCELLED": "cancelled"}


def validate_address(value):
    try:
        check_url(value, allow_local_http=True)
    except ConnectorError:
        raise ValidationError(
            "Use the HTTPS address of your Documenso, or plain HTTP only on this computer or "
            "the firm's own network."
        ) from None
    parts = urlsplit(value)
    if parts.query or parts.path.strip("/"):
        raise ValidationError("Enter only the address, such as https://sign.example.test.")


def page_count(pdf):
    """How many pages the PDF has, from its page objects; at least one."""
    return max(len(PAGE.findall(pdf)), 1)


def path_id(value):
    return quote(str(value), safe="")


@register
class DocumensoConnector(Connector):
    key = "documenso"
    name = "Documenso"
    description = (
        "Send a matter's documents for signature through Documenso, hosted or self-hosted, "
        "and file the signed copy on the matter."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.ESIGNATURE})
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "api_key",
                "API token",
                help_text="From the Documenso team's settings, under API tokens. It starts "
                "with api_.",
            ),
        ),
        docs_url="https://docs.documenso.com/docs/developers/getting-started/authentication",
        label="API token",
    )
    settings_fields = (
        SettingField(
            "address",
            "Documenso address",
            default=HOSTED,
            help_text="Leave as it is for Documenso's hosted service. For the firm's own "
            "Documenso, its address.",
            validate=validate_address,
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "Each document staff send for signature: the PDF, its title, a message, and "
            "each signer's name and email",
        ),
        receives=(
            "Where each document stands: who has signed or declined, and the signed PDF "
            "once everyone has signed",
        ),
    )
    outbound_actions = {
        "send_for_signature": Capability.ESIGNATURE,
        "cancel_signature": Capability.ESIGNATURE,
    }
    webhooks = WebhookSpec(
        capabilities=(Capability.ESIGNATURE,),
        secret_source="crm",
        instructions="In Documenso, open the team's settings, then Webhooks, and add this "
        "address with the secret shown here. Choose the events for a document being "
        "signed, completed, rejected, and cancelled.",
    )

    def __init__(self, connection):
        super().__init__(connection)
        self.api = ProviderClient(
            (self.setting("address") or HOSTED).rstrip("/") + "/api/v2",
            name="Documenso",
            headers={
                "Accept": "application/json",
                "Authorization": str(self.credentials.get("api_key", "")),
            },
            allow_local_http=True,
            max_bytes=MAX_PDF_BYTES,
        )

    def request(self, method, path, **kwargs):
        try:
            return self.api.request(method, path, **kwargs)
        except ConnectorError as exc:
            if exc.status in (401, 403):
                raise ConnectorError(
                    "Documenso did not accept the API token. Make a new one and connect again.",
                    status=401,
                ) from None
            raise

    def call(self, method, path, **kwargs):
        response = self.request(method, path, **kwargs)
        try:
            return response.json() if response.content else {}
        except ValueError:
            raise ConnectorError("Documenso's answer was not understood.") from None

    def test_connection(self):
        # An envelope that cannot exist: any answer but a refused token means it works.
        self.request("GET", "/envelope/envelope_crm_connection_check", expected=(200, 400, 404))

    # Sending

    def send(self, payload):
        pdf = base64.b64decode(payload["pdf"])
        last_page = page_count(pdf)
        recipients = [
            {
                "name": signer["name"],
                "email": signer["email"],
                "role": "SIGNER",
                "fields": [
                    {
                        "type": "SIGNATURE",
                        "identifier": 0,
                        "page": last_page,
                        "positionX": FIELD_LEFT,
                        "positionY": FIELD_TOP - row * (FIELD_HEIGHT + 1),
                        "width": FIELD_WIDTH,
                        "height": FIELD_HEIGHT,
                    }
                ],
            }
            for row, signer in enumerate(payload["signers"])
        ]
        body = {
            "type": "DOCUMENT",
            "title": payload["title"],
            "recipients": recipients,
            "meta": {"subject": payload["title"], "message": payload.get("message", "")},
        }
        created = self.call(
            "POST",
            "/envelope/create",
            data={"payload": json.dumps(body)},
            files={"files": (payload.get("filename") or "document.pdf", pdf, "application/pdf")},
        )
        envelope_id = str(created.get("id") or "")
        if not envelope_id:
            raise ConnectorError("Documenso did not make the document.")
        self.call("POST", "/envelope/distribute", json={"envelopeId": envelope_id})
        return {"id": envelope_id}

    def perform(self, action, payload):
        if action == "send_for_signature":
            return self.send(payload)
        if action == "cancel_signature":
            response = self.request(
                "POST",
                "/envelope/cancel",
                json={"envelopeId": payload["document_id"]},
                expected=(200, 400, 404),
            )
            if response.status_code != 200:
                raise ConnectorError(
                    "Documenso could not cancel it. It may already be signed or cancelled."
                )
            return payload["document_id"]
        raise ConnectorError(f"Documenso cannot {action}.")

    # Progress

    def verify_webhook(self, body, headers, secret, *, url=""):
        given = str(headers.get("x-documenso-secret", ""))
        return bool(secret) and bool(given) and hmac.compare_digest(given, secret)

    def parse_webhook(self, body, headers):
        try:
            found = json.loads(body)
            envelope_id = str(found["payload"]["envelopeId"])
        except (ValueError, KeyError, TypeError):
            raise ConnectorError("The event was not understood.") from None
        event = str(found.get("event") or "")[:80]
        stamp = str(found.get("createdAt") or "")[:40]
        return WebhookEnvelope(
            event_id=f"{event}:{envelope_id}:{stamp}"[:255],
            capability=Capability.ESIGNATURE,
            payload={"envelope_id": envelope_id[:200]},
        )

    def handle_webhook(self, capability, payload):
        """Ask Documenso where the envelope stands; the delivery is only a hint."""
        envelope_id = payload["envelope_id"]
        response = self.request("GET", f"/envelope/{path_id(envelope_id)}", expected=(200, 404))
        if response.status_code == 404:
            return [self.record(envelope_id, "cancelled")]
        try:
            envelope = response.json()
        except ValueError:
            raise ConnectorError("Documenso's answer was not understood.") from None
        status = str(envelope.get("status") or "").upper()
        recipients = envelope.get("recipients") or []
        if status == "COMPLETED":
            return [self.record(envelope_id, "completed", signed_pdf=self.signed_pdf(envelope))]
        if status in FINISHED:
            declined = [r for r in recipients if str(r.get("signingStatus")).upper() == "REJECTED"]
            return [self.record(envelope_id, FINISHED[status], declined[0] if declined else None)]
        return [
            self.record(envelope_id, "signed", recipient)
            for recipient in recipients
            if str(recipient.get("signingStatus")).upper() == "SIGNED"
        ] or [self.record(envelope_id, "sent")]

    def signed_pdf(self, envelope):
        items = envelope.get("envelopeItems") or []
        if not items or not items[0].get("id"):
            raise ConnectorError("Documenso did not say which file was signed.")
        pdf = self.request(
            "GET",
            f"/envelope/item/{path_id(items[0]['id'])}/download",
            params={"version": "signed"},
            headers={"Accept": "application/pdf"},
        ).content
        if not pdf.startswith(b"%PDF-"):
            raise ConnectorError("Documenso's signed copy was not a PDF.")
        return pdf

    @staticmethod
    def record(envelope_id, status, recipient=None, *, signed_pdf=b""):
        return SignatureRecord(
            external_id=str(envelope_id),
            occurred_at=datetime.now(UTC),
            status=status,
            signer_email=str((recipient or {}).get("email") or ""),
            signed_pdf=signed_pdf,
        )
