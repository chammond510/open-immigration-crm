"""A fictional connector that exercises the framework without any network access.

It is hidden from the catalog. Real connectors follow the same shape.
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
    SyncBatch,
    WebhookEnvelope,
    WebhookSpec,
)
from crm.connect.records import TextRecord
from crm.connect.registry import register

EPOCH = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
SIGNATURE_HEADER = "X-Example-Signature"
# A number in the 555-0100 to 555-0199 range North America reserves for fiction.
FICTIONAL_SENDER = "+12025550143"


def sign(body, secret):
    """The example service signs each delivery with HMAC-SHA256 of the raw body."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@register
class ExampleConnector(Connector):
    key = "example"
    name = "Example service"
    description = "A fictional service used to test the connector framework."
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.TEXTING})
    auth = ApiKeyAuth(
        fields=(CredentialField("api_key", "API key", help_text="Starts with example-"),)
    )
    data_flows = DataFlows(
        version=1,
        sends=("Fictional messages staff approve",),
        receives=("Fictional message events",),
    )
    outbound_actions = {"send_message": Capability.TEXTING}
    sync_capabilities = frozenset({Capability.TEXTING})
    webhooks = WebhookSpec(
        capabilities=(Capability.TEXTING,),
        instructions="Paste the URL and signing secret into the example service.",
    )
    hidden = True

    def test_connection(self):
        if not str(self.credentials.get("api_key", "")).startswith("example-"):
            raise ConnectorError("The example service rejected this API key.")

    def sync(self, capability, cursor):
        start = int((cursor or {}).get("after", 0))
        records = [
            TextRecord(
                external_id=f"example-message-{number}",
                occurred_at=EPOCH + timedelta(minutes=number),
                direction="inbound",
                remote=FICTIONAL_SENDER,
                body=f"Fictional message {number}",
            )
            for number in range(start + 1, start + 4)
        ]
        return SyncBatch(records=records, cursor={"after": start + 3})

    def perform(self, action, payload):
        if action != "send_message" or not payload.get("body"):
            raise ConnectorError("The example service needs a message body.")
        return f"example-sent-{len(payload['body'])}"

    def verify_webhook(self, body, headers, secret):
        signature = headers.get(SIGNATURE_HEADER, "")
        return bool(signature) and hmac.compare_digest(signature, sign(body, secret))

    def parse_webhook(self, body, headers):
        try:
            event = json.loads(body)
        except ValueError as exc:
            raise ConnectorError("The example event was not valid JSON.") from exc
        if not isinstance(event, dict) or not event.get("id"):
            raise ConnectorError("The example event had no id.")
        # Keep only what processing needs.
        payload = {
            "id": str(event["id"]),
            "from": str(event.get("from", ""))[:40],
            "body": str(event.get("body", ""))[:1000],
            "sent_at": str(event.get("sent_at", "")),
        }
        return WebhookEnvelope(
            event_id=payload["id"], capability=Capability.TEXTING, payload=payload
        )

    def handle_webhook(self, capability, payload):
        sent_at = payload.get("sent_at")
        occurred_at = datetime.fromisoformat(sent_at) if sent_at else EPOCH
        return [
            TextRecord(
                external_id=payload["id"],
                occurred_at=occurred_at,
                direction="inbound",
                remote=payload.get("from") or FICTIONAL_SENDER,
                body=payload.get("body", ""),
            )
        ]
