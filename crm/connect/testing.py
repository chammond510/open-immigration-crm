"""Shared contract tests every connector must pass.

Mix ``ConnectorContract`` into a ``django.test.TestCase`` in the connector's
package and set the class attributes. The tests block network access, so
connectors must answer from recorded fixtures or a fake transport.
"""

import socket
from datetime import datetime
from unittest import mock

from django.contrib.auth import get_user_model

from .base import Connector, ConnectorError, Record, SyncBatch
from .models import Connection
from .registry import manifest_problems


def _network_blocked(*args, **kwargs):
    raise AssertionError("Connector tests must not reach the network; use fixtures.")


class ConnectorContract:
    connector_class: type[Connector] | None = None
    valid_credentials: dict = {}
    invalid_credentials: dict = {}
    # Capability -> cursor to start from; listed capabilities must support sync.
    sync_capabilities: dict = {}
    # Outbound action -> payload that should succeed.
    outbound_examples: dict = {}

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
        for capability, cursor in self.sync_capabilities.items():
            batch = connector.sync(capability, cursor)
            self.assertIsInstance(batch, SyncBatch)
            self.assertIsInstance(batch.cursor, dict)
            for record in batch.records:
                self.assertIsInstance(record, Record)
                self.assertTrue(record.external_id)
                self.assertIsInstance(record.occurred_at, datetime)
                self.assertIsNotNone(record.occurred_at.tzinfo, "Record times must be aware.")

    def test_contract_outbound_actions_return_references(self):
        connector = self.connector_class(self.make_connection())
        self.assertEqual(set(self.outbound_examples), set(self.connector_class.outbound_actions))
        for action, payload in self.outbound_examples.items():
            self.assertTrue(str(connector.perform(action, payload)))
