"""A fictional e-signature service for testing signatures without any network access.

It is hidden from the catalog. Sending answers with a fictional document ID made
from the title, and a title containing "refuse" is refused. Tests report progress
by sending ``SignatureRecord``s through ``records_received`` themselves.
"""

import hashlib

from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
)
from crm.connect.registry import register


@register
class ExampleEsignatureConnector(Connector):
    key = "example_esignature"
    name = "Example e-signature"
    description = "A fictional e-signature service used to test sending for signature."
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.ESIGNATURE})
    auth = ApiKeyAuth(fields=(CredentialField("api_key", "API key"),))
    data_flows = DataFlows(
        version=1, sends=("A PDF and its signers",), receives=("Fictional signing progress",)
    )
    outbound_actions = {
        "send_for_signature": Capability.ESIGNATURE,
        "cancel_signature": Capability.ESIGNATURE,
    }
    hidden = True

    def test_connection(self):
        if not str(self.credentials.get("api_key", "")).startswith("example-"):
            raise ConnectorError("The example e-signature service rejected this key.")

    def perform(self, action, payload):
        if action == "cancel_signature":
            return payload["document_id"]
        if action != "send_for_signature" or "refuse" in payload.get("title", ""):
            raise ConnectorError("The example e-signature service refused the document.")
        digest = hashlib.sha256(payload["title"].encode()).hexdigest()[:8]
        return {"id": f"example-{digest}"}
