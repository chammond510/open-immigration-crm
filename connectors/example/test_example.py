import json

from django.test import TestCase

from crm.connect.base import ConnectorError
from crm.connect.testing import ConnectorContract

from . import SIGNATURE_HEADER, ExampleConnector, sign


def example_event(event_id="evt-1", body="Fictional reply", sent_at="2026-01-05T10:00:00+00:00"):
    return json.dumps({"id": event_id, "body": body, "sent_at": sent_at}).encode()


class ExampleConnectorContractTests(ConnectorContract, TestCase):
    connector_class = ExampleConnector
    valid_credentials = {"api_key": "example-fictional-key"}
    invalid_credentials = {"api_key": "wrong-key"}
    sync_examples = {"texting": {"after": 0}}
    outbound_examples = {"send_message": {"body": "Fictional reminder"}}

    def make_webhook(self, secret):
        body = example_event()
        return body, {SIGNATURE_HEADER: sign(body, secret)}

    def test_sync_cursor_advances(self):
        connector = ExampleConnector(self.make_connection())
        first = connector.sync("texting", {})
        second = connector.sync("texting", first.cursor)
        self.assertEqual(first.records[-1].external_id, "example-message-3")
        self.assertEqual(second.records[0].external_id, "example-message-4")

    def test_malformed_events_are_rejected(self):
        connector = ExampleConnector(self.make_connection())
        for body in (b"not json", b"[]", json.dumps({"body": "no id"}).encode()):
            with self.assertRaises(ConnectorError):
                connector.parse_webhook(body, {})

    def test_payload_keeps_only_what_processing_needs(self):
        connector = ExampleConnector(self.make_connection())
        body = json.dumps({"id": "evt-2", "body": "Hi", "sender_phone": "555-0100"}).encode()
        envelope = connector.parse_webhook(body, {})
        self.assertEqual(set(envelope.payload), {"id", "body", "sent_at"})
        record = connector.handle_webhook(envelope.capability, envelope.payload)[0]
        self.assertEqual(record.external_id, "evt-2")
