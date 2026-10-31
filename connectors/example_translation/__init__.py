"""A fictional translation service for testing translation without any network access.

It is hidden from the catalog. Each "translation" is the text with the language it
was put into in brackets, such as ``[en] Mi esposo``, and every text is said to have
been Spanish. A text containing "refuse" is refused.
"""

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
class ExampleTranslationConnector(Connector):
    key = "example_translation"
    name = "Example translation"
    description = "A fictional translation service used to test translation."
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.TRANSLATION})
    auth = ApiKeyAuth(fields=(CredentialField("api_key", "API key"),))
    data_flows = DataFlows(
        version=1, sends=("The texts staff ask to translate",), receives=("Fictional translations",)
    )
    outbound_actions = {"translate": Capability.TRANSLATION}
    hidden = True

    def test_connection(self):
        if not str(self.credentials.get("api_key", "")).startswith("example-"):
            raise ConnectorError("The example translation service rejected this key.")

    def perform(self, action, payload):
        if action != "translate" or any("refuse" in text for text in payload["texts"]):
            raise ConnectorError("The example translation service refused the text.")
        return {
            "translations": [
                {"text": f"[{payload['language']}] {text}", "language": "es"}
                for text in payload["texts"]
            ]
        }
