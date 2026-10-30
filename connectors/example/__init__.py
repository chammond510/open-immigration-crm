"""A fictional connector that exercises the framework without any network access.

It is hidden from the catalog. Real connectors follow the same shape.
"""

from datetime import UTC, datetime, timedelta

from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
    Record,
    SyncBatch,
)
from crm.connect.registry import register

EPOCH = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


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
    hidden = True

    def test_connection(self):
        if not str(self.credentials.get("api_key", "")).startswith("example-"):
            raise ConnectorError("The example service rejected this API key.")

    def sync(self, capability, cursor):
        start = int((cursor or {}).get("after", 0))
        records = [
            Record(
                external_id=f"example-message-{number}",
                kind="message",
                occurred_at=EPOCH + timedelta(minutes=number),
                data={"body": f"Fictional message {number}"},
            )
            for number in range(start + 1, start + 4)
        ]
        return SyncBatch(records=records, cursor={"after": start + 3})

    def perform(self, action, payload):
        if action != "send_message" or not payload.get("body"):
            raise ConnectorError("The example service needs a message body.")
        return f"example-sent-{len(payload['body'])}"
