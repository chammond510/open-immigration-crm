"""DeepL: translation of client messages and intake answers, with a DeepL API Pro key.

DeepL's API Free terms forbid sending personal data through it, so a Free key (one
ending in ":fx") is refused. Under the Pro terms, DeepL deletes texts once they are
translated and does not use them to improve its services. The firm should also have
DeepL's data processing agreement in place. Texts go in batches of at most 50 and
100 KB, below DeepL's limits of 50 texts and 128 KiB a request.

API reference: https://developers.deepl.com/api-reference/translate/request-translation
"""

import json

from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
)
from crm.connect.http import ProviderClient
from crm.connect.registry import register

API_URL = "https://api.deepl.com"
# The CRM's language codes, and DeepL's for each as a target; DeepL wants a variant
# of English, Portuguese, and Chinese.
TARGETS = {
    "en": "EN-US",
    "es": "ES",
    "pt": "PT-BR",
    "fr": "FR",
    "zh": "ZH-HANS",
    "ar": "AR",
    "ru": "RU",
    "uk": "UK",
    "ko": "KO",
    "ja": "JA",
    "vi": "VI",
    "tr": "TR",
    "id": "ID",
}
BATCH_TEXTS = 50
BATCH_BYTES = 100_000
# Guides DeepL's choice of words; DeepL neither translates nor bills it.
CONTEXT = "Messages and form answers from clients of an immigration law firm."
FREE_KEY = (
    "This is a DeepL API Free key. DeepL's API Free terms forbid sending personal data, such "
    "as clients' messages, so use a DeepL API Pro key."
)


def batches(texts):
    """The texts in order, grouped to stay inside DeepL's limits for one request."""
    batch, size = [], 0
    for text in texts:
        length = len(json.dumps(text).encode())
        if batch and (len(batch) == BATCH_TEXTS or size + length > BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(text)
        size += length
    if batch:
        yield batch


@register
class DeepLConnector(Connector):
    key = "deepl"
    name = "DeepL"
    description = (
        "Translate client messages and intake answers when staff ask, with the firm's DeepL "
        "API Pro key. Translations are labeled and kept beside the originals."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.TRANSLATION})
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "api_key",
                "DeepL API Pro key",
                help_text="From the API keys page of the firm's DeepL Pro account. A key limited "
                "to translating text and reading usage is enough. API Free keys are refused: "
                "DeepL's terms for them forbid personal data.",
            ),
        ),
        docs_url="https://developers.deepl.com/docs/getting-started/auth",
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "The text of each message, or each intake answer, that staff ask to translate, "
            "and the language to translate it into",
        ),
        receives=("The translations, and the language each text was written in",),
    )
    outbound_actions = {"translate": Capability.TRANSLATION}

    def __init__(self, connection):
        super().__init__(connection)
        key = str(self.credentials.get("api_key", "")).strip()
        self.free = key.endswith(":fx")
        self.http = ProviderClient(
            API_URL, name="DeepL", headers={"Authorization": f"DeepL-Auth-Key {key}"}
        )

    def call(self, method, path, **kwargs):
        if self.free:
            raise ConnectorError(FREE_KEY)
        try:
            return self.http.json(method, path, **kwargs)
        except ConnectorError as exc:
            if exc.status == 403:
                raise ConnectorError(
                    "DeepL did not accept the key. Check it is a current DeepL API Pro key "
                    "allowed to translate text and read usage.",
                    status=403,
                ) from None
            if exc.status == 456:
                raise ConnectorError(
                    "The DeepL account's translation quota is used up.", status=456
                ) from None
            raise

    def test_connection(self):
        self.call("GET", "/v2/usage")

    def perform(self, action, payload):
        if action != "translate":
            raise ConnectorError(f"DeepL cannot {action}.")
        target = TARGETS.get(payload.get("language", ""))
        if target is None:
            raise ConnectorError("DeepL does not translate into that language.")
        translations = []
        for batch in batches(payload["texts"]):
            answer = self.call(
                "POST",
                "/v2/translate",
                json={"text": batch, "target_lang": target, "context": CONTEXT},
            )
            found = answer.get("translations")
            if not isinstance(found, list) or len(found) != len(batch):
                raise ConnectorError("DeepL did not translate every text.")
            translations += [
                {
                    "text": str(item.get("text", "")),
                    "language": str(item.get("detected_source_language", "")).lower(),
                }
                for item in found
            ]
        return {"translations": translations}
