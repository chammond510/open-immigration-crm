from dataclasses import replace
from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import connection as db_connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.connect.base import ConnectionScope, ConnectorError
from crm.connect.models import Connection, OutboundRequest
from crm.connect.policy import Codes, evaluate, send
from crm.connect.registry import registry
from crm.connect.services import (
    acknowledge_data_flows,
    check_connection,
    create_connection,
    disconnect,
)
from crm.models import AuditLog
from crm.tests.factories import make_contact, make_user

# Core reaches connectors only through the registry, never by import.
ExampleConnector = registry.get("example")
VALID = {"api_key": "example-fictional-key"}


class PersonalExampleConnector(ExampleConnector):
    key = "personal_example"
    connection_scope = ConnectionScope.USER


def connect(user, **overrides):
    options = {
        "label": "Example",
        "created_by": user,
        "capabilities": ["texting"],
        "credentials": VALID,
        **overrides,
    }
    return create_connection(overrides.pop("connector_key", "example"), **options)


def ready_connection(user, **overrides):
    """An active, acknowledged example connection with preview mode off."""
    connection = connect(user, **overrides)
    acknowledge_data_flows(connection, user)
    connection.preview_mode = False
    connection.save()
    return connection


class ConnectionServiceTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_create_validates_against_the_manifest(self):
        with self.assertRaisesMessage(ValidationError, "No connector named"):
            create_connection("missing", label="X", created_by=self.user, capabilities=["texting"])
        with self.assertRaisesMessage(ValidationError, "Choose capabilities"):
            connect(self.user, capabilities=["payments"])
        with self.assertRaisesMessage(ValidationError, "Missing: API key"):
            connect(self.user, credentials={})
        with self.assertRaisesMessage(ValidationError, "whole firm"):
            connect(self.user, owner=self.user)
        with mock.patch.dict(registry._connectors, {"personal_example": PersonalExampleConnector}):
            with self.assertRaisesMessage(ValidationError, "one staff member"):
                create_connection(
                    "personal_example",
                    label="Mine",
                    created_by=self.user,
                    capabilities=["texting"],
                    credentials=VALID,
                )

    def test_credentials_are_encrypted_and_creation_is_audited(self):
        connection = connect(self.user, label="Encryption check")
        with db_connection.cursor() as cursor:
            # Look up by label: UUID parameters bind differently on SQLite and PostgreSQL.
            cursor.execute(
                "SELECT credentials FROM connect_connection WHERE label = %s",
                ["Encryption check"],
            )
            raw = cursor.fetchone()[0]
        self.assertNotIn("example-fictional-key", raw)
        self.assertEqual(Connection.objects.get(pk=connection.pk).credentials, VALID)
        self.assertTrue(connection.preview_mode)
        self.assertEqual(str(connection), "Encryption check")
        self.assertTrue(AuditLog.objects.filter(action="connection.created").exists())

    def test_acknowledgment_tracks_the_data_flows_version(self):
        connection = connect(self.user)
        self.assertFalse(connection.data_flows_acknowledged)
        acknowledge_data_flows(connection, self.user)
        self.assertTrue(connection.data_flows_acknowledged)
        self.assertEqual(connection.data_flows_acknowledged_by, self.user)
        newer = replace(ExampleConnector.data_flows, version=2)
        with mock.patch.object(ExampleConnector, "data_flows", newer):
            self.assertFalse(connection.data_flows_acknowledged)

    def test_check_connection_records_success_and_failure(self):
        connection = connect(self.user)
        self.assertTrue(check_connection(connection))
        self.assertEqual(connection.status, Connection.Status.ACTIVE)
        self.assertIsNotNone(connection.last_success_at)
        connection.credentials = {"api_key": "wrong"}
        connection.save()
        self.assertFalse(check_connection(connection))
        self.assertEqual(connection.status, Connection.Status.NEEDS_ATTENTION)
        self.assertIn("rejected this API key", connection.last_error)

    def test_missing_connector_is_reported(self):
        connection = Connection.objects.create(connector_key="uninstalled", label="Old")
        self.assertIsNone(connection.connector())
        self.assertFalse(connection.data_flows_acknowledged)
        with self.assertRaises(ValidationError):
            check_connection(connection)
        with self.assertRaises(ValidationError):
            acknowledge_data_flows(connection, self.user)

    def test_disconnect_deletes_credentials_even_if_revocation_fails(self):
        connection = connect(self.user)
        with mock.patch.object(
            ExampleConnector, "revoke", side_effect=ConnectorError("Provider unavailable")
        ):
            disconnect(connection, self.user)
        connection.refresh_from_db()
        self.assertIsNone(connection.credentials)
        self.assertEqual(connection.status, Connection.Status.DISCONNECTED)
        entry = AuditLog.objects.get(action="connection.disconnected")
        self.assertIn("Provider unavailable", entry.detail)


class OutboundPolicyTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.connection = ready_connection(self.user)

    def decide(self, connection=None, **kwargs):
        options = {"capability": "texting", "actor": self.user, **kwargs}
        return evaluate(connection or self.connection, **options).code

    def test_ready_connection_is_allowed(self):
        self.assertEqual(self.decide(), Codes.OK)

    def test_checks_in_order(self):
        with override_settings(CONNECTORS_OUTBOUND_ENABLED=False):
            self.assertEqual(self.decide(), Codes.KILL_SWITCH)
        missing = Connection.objects.create(connector_key="uninstalled", label="Old")
        self.assertEqual(self.decide(missing), Codes.NOT_INSTALLED)
        self.assertEqual(self.decide(capability="payments"), Codes.CAPABILITY_OFF)
        self.connection.status = Connection.Status.PAUSED
        self.assertEqual(self.decide(), Codes.INACTIVE)
        self.connection.status = Connection.Status.ACTIVE
        self.connection.data_flows_version = None
        self.assertEqual(self.decide(), Codes.NOT_ACKNOWLEDGED)

    def test_personal_connections_serve_only_their_owner(self):
        colleague = make_user("colleague", superuser=False)
        self.connection.owner = self.user
        self.assertEqual(self.decide(), Codes.OK)
        self.assertEqual(self.decide(actor=colleague), Codes.PERSONAL_CONNECTION)
        self.assertEqual(self.decide(is_automation=True), Codes.PERSONAL_CONNECTION)
        self.assertEqual(self.decide(actor=None), Codes.PERSONAL_CONNECTION)

    def test_admin_only_connections(self):
        self.connection.outbound_admin_only = True
        staff = make_user("paralegal", superuser=False)
        self.assertEqual(self.decide(actor=staff), Codes.ADMIN_ONLY)
        self.assertEqual(self.decide(actor=self.user), Codes.OK)
        self.assertEqual(self.decide(actor=None, is_automation=True), Codes.OK)

    @override_settings(CONNECTORS_OUTBOUND_HOURLY_LIMIT=1)
    def test_hourly_limit_counts_only_recent_sends(self):
        old = send(self.connection, "send_message", {"body": "First"}, actor=self.user)
        OutboundRequest.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(hours=2)
        )
        self.assertEqual(self.decide(), Codes.OK)
        send(self.connection, "send_message", {"body": "Second"}, actor=self.user)
        self.assertEqual(self.decide(), Codes.RATE_LIMITED)

    def test_preview_mode_is_checked_last(self):
        self.connection.preview_mode = True
        self.assertEqual(self.decide(), Codes.PREVIEW)


class SendTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.connection = ready_connection(self.user)

    def test_allowed_send_records_the_provider_reference(self):
        contact = make_contact(self.user)
        request = send(
            self.connection,
            "send_message",
            {"body": "Reminder"},
            actor=self.user,
            summary="Appointment reminder",
            target=contact,
        )
        self.assertEqual(request.status, OutboundRequest.Status.SENT)
        self.assertEqual(request.provider_ref, "example-sent-8")
        self.assertEqual(request.capability, "texting")
        self.assertEqual(request.target_type, "Contact")
        self.assertEqual(request.target_id, str(contact.pk))
        self.assertEqual(str(request), "example send_message (sent)")

    def test_preview_mode_records_without_performing(self):
        self.connection.preview_mode = True
        self.connection.save()
        with mock.patch.object(ExampleConnector, "perform") as perform:
            request = send(self.connection, "send_message", {"body": "Hi"}, actor=self.user)
        perform.assert_not_called()
        self.assertEqual(request.status, OutboundRequest.Status.PREVIEWED)

    def test_blocked_send_records_the_reason(self):
        self.connection.status = Connection.Status.PAUSED
        self.connection.save()
        request = send(self.connection, "send_message", {"body": "Hi"}, actor=self.user)
        self.assertEqual(request.status, OutboundRequest.Status.BLOCKED)
        self.assertEqual(request.decision_code, Codes.INACTIVE)
        self.assertIn("not active", request.error)

    def test_provider_failures_are_recorded_safely(self):
        request = send(self.connection, "send_message", {"body": ""}, actor=self.user)
        self.assertEqual(request.status, OutboundRequest.Status.FAILED)
        self.assertIn("needs a message body", request.error)
        with mock.patch.object(
            ExampleConnector, "perform", side_effect=ValueError("client name in message")
        ):
            request = send(self.connection, "send_message", {"body": "Hi"}, actor=self.user)
        self.assertEqual(request.error, "Unexpected ValueError")

    def test_unknown_action_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            send(self.connection, "send_fax", {}, actor=self.user)

    def test_send_through_uninstalled_connector_is_blocked(self):
        missing = Connection.objects.create(connector_key="uninstalled", label="Old")
        request = send(missing, "send_message", {}, actor=self.user)
        self.assertEqual(request.decision_code, Codes.NOT_INSTALLED)


class AdminTests(TestCase):
    def test_operators_can_view_connections_and_outbound_log(self):
        user = make_user()
        connection = ready_connection(user)
        send(connection, "send_message", {"body": "Hi"}, actor=user)
        self.client.force_login(user)
        for name in (
            "admin:connect_connection_changelist",
            "admin:connect_outboundrequest_changelist",
        ):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        page = self.client.get(reverse("admin:connect_connection_change", args=[connection.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "example-fictional-key")
