"""Shared contract tests every connector must pass.

Mix ``ConnectorContract`` into a ``django.test.TestCase`` in the connector's
package and set the class attributes. The tests block network access, so
connectors must answer from recorded fixtures or a fake transport.
"""

import json
import socket
from datetime import datetime
from unittest import mock

from django.contrib.auth import get_user_model

from .base import Connector, ConnectorError, Record, SyncBatch, WebhookEnvelope
from .models import Connection
from .records import RECORD_TYPES
from .registry import manifest_problems


def _network_blocked(*args, **kwargs):
    raise AssertionError("Connector tests must not reach the network; use fixtures.")


class ConnectorContract:
    connector_class: type[Connector] | None = None
    valid_credentials: dict = {}
    invalid_credentials: dict = {}
    # Capability -> cursor to start from, for every capability the connector syncs.
    sync_examples: dict = {}
    # Outbound action -> payload that should succeed.
    outbound_examples: dict = {}
    webhook_secret = "fictional-webhook-secret"

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(socket.socket, "connect", _network_blocked)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_connection(self, credentials=None):
        cls = self.connector_class
        owner = None
        if cls.connection_scope == "user":
            owner = get_user_model().objects.create_user(
                username="contract-owner", email="contract-owner@example.test"
            )
        return Connection.objects.create(
            connector_key=cls.key,
            label=f"{cls.name} contract test",
            owner=owner,
            enabled_capabilities=sorted(cls.capabilities),
            credentials=self.valid_credentials if credentials is None else credentials,
        )

    def make_webhook(self, secret):
        """Return (body bytes, headers dict) for one validly signed fictional event.

        Required when the connector declares webhooks.
        """
        raise NotImplementedError

    def assert_records(self, records, capability=None):
        """Each record is the typed record its capability produces, with no problems."""
        expected = RECORD_TYPES.get(capability, ())
        for record in records:
            self.assertIsInstance(record, Record)
            self.assertTrue(record.external_id)
            self.assertIsInstance(record.occurred_at, datetime)
            self.assertIsNotNone(record.occurred_at.tzinfo, "Record times must be aware.")
            if expected:
                self.assertIsInstance(
                    record, expected, f"{capability} connectors return {expected[0].__name__}."
                )
                self.assertEqual(record.problems(), [])

    def test_contract_manifest_is_complete(self):
        self.assertEqual(manifest_problems(self.connector_class), [])

    def test_contract_valid_credentials_pass(self):
        self.connector_class(self.make_connection()).test_connection()

    def test_contract_invalid_credentials_fail_cleanly(self):
        connector = self.connector_class(self.make_connection(self.invalid_credentials))
        with self.assertRaises(ConnectorError):
            connector.test_connection()

    def test_contract_sync_returns_records(self):
        connector = self.connector_class(self.make_connection())
        self.assertEqual(set(self.sync_examples), set(self.connector_class.sync_capabilities))
        for capability, cursor in self.sync_examples.items():
            batch = connector.sync(capability, cursor)
            self.assertIsInstance(batch, SyncBatch)
            self.assertIsInstance(batch.cursor, dict)
            json.dumps(batch.cursor)
            self.assert_records(batch.records, capability)

    def test_contract_outbound_actions_return_references(self):
        connector = self.connector_class(self.make_connection())
        self.assertEqual(set(self.outbound_examples), set(self.connector_class.outbound_actions))
        for action, payload in self.outbound_examples.items():
            self.assertTrue(str(connector.perform(action, payload)))

    def test_contract_webhooks_verify_parse_and_handle(self):
        cls = self.connector_class
        if cls.webhooks is None:
            self.skipTest("This connector does not receive webhooks.")
        connector = cls(self.make_connection())
        body, headers = self.make_webhook(self.webhook_secret)
        self.assertTrue(connector.verify_webhook(body, headers, self.webhook_secret))
        self.assertFalse(connector.verify_webhook(body, headers, "a-different-secret"))
        self.assertFalse(connector.verify_webhook(body + b" ", headers, self.webhook_secret))
        self.assertFalse(connector.verify_webhook(body, {}, self.webhook_secret))
        envelope = connector.parse_webhook(body, headers)
        self.assertIsInstance(envelope, WebhookEnvelope)
        self.assertTrue(envelope.event_id)
        self.assertIn(envelope.capability, cls.webhooks.capabilities)
        json.dumps(envelope.payload)
        self.assert_records(
            connector.handle_webhook(envelope.capability, envelope.payload), envelope.capability
        )
