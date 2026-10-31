"""A fictional messaging service with a reply window, for testing without any network access.

It is hidden from the catalog. Like services that allow free-form messages only within
24 hours of the contact's last message, it declares a 24-hour reply window, and it has
two approved templates for messages outside it. A message containing "refuse" is
refused.
"""

import uuid
from datetime import timedelta

from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
    MessageTemplate,
    TemplateField,
)
from crm.connect.registry import register

TEMPLATES = (
    MessageTemplate(
        id="reply_request:en",
        name="reply_request",
        text="Hello {{name}}, this is your law firm. Please reply so we can talk about your case.",
        fields=(TemplateField("name", "Client's first name", "Ana"),),
        language="en",
        category="utility",
    ),
    MessageTemplate(
        id="office_closed:en",
        name="office_closed",
        text="Our office is closed today. We will write again tomorrow.",
        language="en",
        category="utility",
    ),
)


@register
class ExampleMessagingConnector(Connector):
    key = "example_messaging"
    name = "Example messaging"
    description = "A fictional messaging service used to test reply windows and templates."
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.TEXTING})
    auth = ApiKeyAuth(fields=(CredentialField("api_key", "API key"),))
    data_flows = DataFlows(
        version=1,
        sends=("The contact's phone number and each message staff send",),
        receives=("Fictional message IDs",),
    )
    outbound_actions = {"send_text": Capability.TEXTING, "send_template": Capability.TEXTING}
    reply_window = timedelta(hours=24)
    hidden = True

    def test_connection(self):
        if not str(self.credentials.get("api_key", "")).startswith("example-"):
            raise ConnectorError("The example messaging service rejected this key.")

    def message_templates(self):
        if "refuse" in str(self.credentials.get("api_key", "")):
            raise ConnectorError("The example messaging service would not list its templates.")
        return list(TEMPLATES)

    def perform(self, action, payload):
        words = payload.get("body", "") + " ".join(payload.get("values", {}).values())
        if action not in self.outbound_actions or "refuse" in words:
            raise ConnectorError("The example messaging service refused the message.")
        if action == "send_template" and payload["template"] not in {t.id for t in TEMPLATES}:
            raise ConnectorError("The example messaging service has no such template.")
        return f"example-message-{uuid.uuid4().hex}"
