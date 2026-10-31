from django.test import TestCase

from crm.connect.testing import ConnectorContract

from . import ExampleTranslationConnector


class ExampleTranslationContractTests(ConnectorContract, TestCase):
    connector_class = ExampleTranslationConnector
    valid_credentials = {"api_key": "example-fictional"}
    invalid_credentials = {"api_key": "made-up"}
    outbound_examples = {"translate": {"texts": ["Hola"], "language": "en"}}
