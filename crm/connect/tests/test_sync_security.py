from dataclasses import replace
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from crm.comms.models import Communication
from crm.comms.records import store_one
from crm.connect.models import SyncRun
from crm.connect.records import EmailRecord
from crm.connect.registry import registry
from crm.connect.scheduler import queue_due_syncs, queue_sync, start_syncs
from crm.connect.services import acknowledge_data_flows, create_connection
from crm.connect.tasks import run_sync
from crm.tests.factories import make_contact, make_user


class SyncApprovalTests(TestCase):
    def setUp(self):
        self.manager = make_user()
        self.owner = make_user("associate", superuser=False)
        self.connection = create_connection(
            "google_workspace",
            label="Fictional mailbox",
            created_by=self.owner,
            owner=self.owner,
            capabilities=["mailbox"],
            credentials={"access_token": "fictional-unused-token"},
        )
        self.connector = registry.get("google_workspace")

    def test_unapproved_mailbox_never_schedules_or_contacts_the_provider(self):
        self.assertFalse(self.connection.data_flows_acknowledged)
        with mock.patch.object(self.connector, "sync") as sync:
            self.assertEqual(queue_due_syncs(), 0)
            self.assertEqual(start_syncs(self.connection), 0)
            self.assertIsNone(queue_sync(self.connection, "mailbox"))
            self.assertFalse(SyncRun.objects.exists())
            # A job queued before the fix must also be refused by the worker.
            run = SyncRun.objects.create(connection=self.connection, capability="mailbox")
            run_sync.call(run.pk)
            run.refresh_from_db()
            self.assertEqual(run.status, SyncRun.Status.SKIPPED)
            sync.assert_not_called()
        self.assertFalse(Communication.objects.exists())

    def test_approval_is_checked_again_when_a_queued_job_runs(self):
        acknowledge_data_flows(self.connection, self.manager)
        self.assertEqual(start_syncs(self.connection), 1)
        run = SyncRun.objects.get()
        updated_flows = replace(
            self.connector.data_flows, version=self.connector.data_flows.version + 1
        )
        with (
            mock.patch.object(self.connector, "data_flows", updated_flows),
            mock.patch.object(self.connector, "sync") as sync,
        ):
            run_sync.call(run.pk)
            sync.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, SyncRun.Status.SKIPPED)

    def test_disabling_a_capability_invalidates_queued_work(self):
        acknowledge_data_flows(self.connection, self.manager)
        run = queue_sync(self.connection, "mailbox")
        self.connection.enabled_capabilities = []
        self.connection.save()
        with mock.patch.object(self.connector, "sync") as sync:
            run_sync.call(run.pk)
            sync.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, SyncRun.Status.SKIPPED)

    def test_unmatched_mail_is_excluded_unless_explicitly_enabled(self):
        record = EmailRecord(
            external_id="fictional-message",
            occurred_at=timezone.now(),
            direction="inbound",
            remote="personal@example.test",
            subject="Private fictional email",
            snippet="Personal",
        )
        self.assertEqual(self.connection.config["unmatched"], "skip")
        self.assertIsNone(store_one(self.connection, record))
        self.connection.config = {}  # Older connections without the setting also fail closed.
        self.assertIsNone(store_one(self.connection, record))
        self.connection.config = {"unmatched": "keep"}
        self.assertIsNotNone(store_one(self.connection, record))

    def test_known_contact_email_is_still_filed_with_the_private_default(self):
        contact = make_contact(self.owner)
        record = EmailRecord(
            external_id="fictional-client-message",
            occurred_at=timezone.now(),
            direction="inbound",
            remote=contact.email,
            subject="Fictional appointment",
        )
        communication = store_one(self.connection, record)
        self.assertEqual(communication.contact, contact)
