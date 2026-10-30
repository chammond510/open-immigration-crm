from django.test import TestCase

from crm.connect.testing import ConnectorContract

from . import ExampleOAuthConnector


class ExampleOAuthConnectorContractTests(ConnectorContract, TestCase):
    connector_class = ExampleOAuthConnector
    valid_credentials = {"access_token": "example-token"}
    invalid_credentials = {"access_token": "revoked"}
