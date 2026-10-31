from django.test import TestCase

from crm.connect.base import ConnectorError
from crm.connect.testing import ConnectorContract

from . import ExampleMessagingConnector


class ExampleMessagingContractTests(ConnectorContract, TestCase):
    connector_class = ExampleMessagingConnector
    valid_credentials = {"api_key": "example-fictional"}
    invalid_credentials = {"api_key": "made-up"}
    outbound_examples = {
        "send_text": {"to": "+12025550144", "body": "Hello", "staff_email": ""},
        "send_template": {
            "to": "+12025550144",
            "template": "reply_request:en",
            "values": {"name": "Ana"},
            "staff_email": "",
        },
    }

    def test_refusals(self):
        connector = ExampleMessagingConnector(self.make_connection())
        for action, payload in (
            ("send_text", {"to": "+12025550144", "body": "refuse this"}),
            ("send_template", {"to": "+12025550144", "template": "missing:en", "values": {}}),
            ("send_fax", {"to": "+12025550144"}),
        ):
            with self.assertRaises(ConnectorError):
                connector.perform(action, payload)
        refusing = ExampleMessagingConnector(self.make_connection({"api_key": "example-refuse"}))
        with self.assertRaises(ConnectorError):
            refusing.message_templates()
