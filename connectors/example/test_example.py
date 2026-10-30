from django.test import TestCase

from crm.connect.testing import ConnectorContract

from . import ExampleConnector


class ExampleConnectorContractTests(ConnectorContract, TestCase):
    connector_class = ExampleConnector
    valid_credentials = {"api_key": "example-fictional-key"}
    invalid_credentials = {"api_key": "wrong-key"}
    sync_capabilities = {"texting": {"after": 0}}
    outbound_examples = {"send_message": {"body": "Fictional reminder"}}

    def test_sync_cursor_advances(self):
        connector = ExampleConnector(self.make_connection())
        first = connector.sync("texting", {})
        second = connector.sync("texting", first.cursor)
        self.assertEqual(first.records[-1].external_id, "example-message-3")
        self.assertEqual(second.records[0].external_id, "example-message-4")
