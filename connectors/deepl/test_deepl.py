import json

import httpx
from django.test import TestCase
from django.urls import reverse

from crm.comms.models import Communication
from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection
from crm.connect.services import acknowledge_data_flows, create_connection
from crm.connect.testing import ConnectorContract
from crm.tests.factories import make_contact, make_user
from crm.translations.models import Translation

from . import BATCH_TEXTS, DeepLConnector, batches

KEY = "fictional-pro-key-0000"


class FakeDeepL:
    """DeepL's API, as its reference documents it, translating by marking each text."""

    def __init__(self):
        self.requests = []
        self.status = None

    def __call__(self, request):
        self.requests.append(request)
        assert request.url.host == "api.deepl.com"
        if request.headers.get("authorization") != f"DeepL-Auth-Key {KEY}":
            return httpx.Response(403, json={"message": "Wrong endpoint or invalid key"})
        if self.status:
            return httpx.Response(self.status, json={"message": "Quota exceeded"})
        if request.url.path == "/v2/usage":
            return httpx.Response(200, json={"character_count": 0, "character_limit": 1000000})
        assert request.url.path == "/v2/translate"
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "translations": [
                    {"detected_source_language": "ES", "text": f"<{body['target_lang']}> {text}"}
                    for text in body["text"]
                ]
            },
        )


class FakeDeepLMixin:
    def setUp(self):
        super().setUp()
        self.deepl = FakeDeepL()
        self.enterContext(http.use_transport(httpx.MockTransport(self.deepl)))


class DeepLContractTests(FakeDeepLMixin, ConnectorContract, TestCase):
    connector_class = DeepLConnector
    valid_credentials = {"api_key": KEY}
    invalid_credentials = {"api_key": "fictional-revoked"}
    outbound_examples = {"translate": {"texts": ["Mi esposo es ciudadano."], "language": "en"}}


class DeepLTests(FakeDeepLMixin, TestCase):
    def connector(self, key=KEY):
        connection = Connection(connector_key="deepl", credentials={"api_key": key})
        return DeepLConnector(connection)

    def test_texts_are_translated_in_batches_inside_deepls_limits(self):
        texts = [f"Texto {number}" for number in range(BATCH_TEXTS + 5)]
        answer = self.connector().perform("translate", {"texts": texts, "language": "pt"})
        self.assertEqual(len(answer["translations"]), len(texts))
        self.assertEqual(
            answer["translations"][-1], {"text": f"<PT-BR> {texts[-1]}", "language": "es"}
        )
        first, second = self.deepl.requests
        body = json.loads(first.content)
        self.assertEqual((len(body["text"]), body["target_lang"]), (BATCH_TEXTS, "PT-BR"))
        self.assertIn("immigration law firm", body["context"])
        self.assertEqual(len(json.loads(second.content)["text"]), 5)
        long = ["x" * 60_000, "y" * 60_000, "z"]
        self.assertEqual([len(batch) for batch in batches(long)], [1, 2])

    def test_what_deepl_refuses(self):
        free = self.connector("fictional-free-key:fx")
        for attempt in (
            free.test_connection,
            lambda: free.perform("translate", {"texts": ["Hola"], "language": "en"}),
        ):
            with self.assertRaisesMessage(ConnectorError, "forbid sending personal data"):
                attempt()
        self.assertEqual(self.deepl.requests, [])  # Nothing was sent with a Free key.
        connector = self.connector()
        with self.assertRaisesMessage(ConnectorError, "does not translate into that language"):
            connector.perform("translate", {"texts": ["Bonjou"], "language": "ht"})
        with self.assertRaisesMessage(ConnectorError, "cannot"):
            connector.perform("detect", {})
        with self.assertRaisesMessage(ConnectorError, "Pro key allowed to translate"):
            self.connector("fictional-revoked").test_connection()
        self.deepl.status = 456
        with self.assertRaisesMessage(ConnectorError, "quota is used up"):
            connector.test_connection()
        self.deepl.status = 400
        with self.assertRaises(ConnectorError):
            connector.test_connection()

    def test_a_short_answer_is_refused(self):
        short = httpx.MockTransport(lambda request: httpx.Response(200, json={"translations": []}))
        with (
            http.use_transport(short),
            self.assertRaisesMessage(ConnectorError, "did not translate every text"),
        ):
            self.connector().perform("translate", {"texts": ["Hola"], "language": "en"})

    def test_staff_translate_a_message_with_deepl(self):
        admin = make_user()
        connection = create_connection(
            "deepl",
            label="Firm DeepL",
            created_by=admin,
            capabilities=["translation"],
            credentials={"api_key": KEY},
        )
        acknowledge_data_flows(connection, admin)
        Connection.objects.filter(pk=connection.pk).update(preview_mode=False)
        contact = make_contact(admin)
        message = Communication.objects.create(
            channel=Communication.Channel.TEXT, summary="Mi esposo es ciudadano.", contact=contact
        )
        self.client.force_login(admin)
        self.client.post(
            reverse("translate_communication", args=[message.pk]),
            {"translator": f"service:{connection.pk}", "language": "en"},
        )
        translation = Translation.objects.get()
        self.assertEqual(
            (translation.content, translation.source_language, translation.translator),
            ({"summary": "<EN-US> Mi esposo es ciudadano."}, "es", "DeepL"),
        )
        page = self.client.get(reverse("contact_detail", args=[contact.pk]))
        self.assertContains(page, "Machine translation into English from Spanish · DeepL")
