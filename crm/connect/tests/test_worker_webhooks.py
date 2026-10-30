import json
import threading
from dataclasses import replace
from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import connection as db_connection
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from crm.connect.base import ConnectorError, WebhookEnvelope
from crm.connect.models import Connection, SyncRun, WebhookEvent, WorkerHeartbeat
from crm.connect.registry import registry
from crm.connect.scheduler import queue_due_syncs, tick, worker_is_running
from crm.connect.services import create_connection, disconnect, enable_webhooks
from crm.connect.signals import records_received
from crm.connect.tasks import process_webhook_event, run_sync
from crm.tests.factories import make_user

# Core reaches connectors only through the registry, never by import.
ExampleConnector = registry.get("example")
SIGNATURE_HEADER = "X-Example-Signature"


def example_sign(body, secret):
    import hashlib
    import hmac

    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def event_body(event_id="evt-1", body="Fictional reply"):
    return json.dumps(
        {"id": event_id, "body": body, "sent_at": "2026-01-05T10:00:00+00:00"}
    ).encode()


def make_connection(user):
    return create_connection(
        "example",
        label="Example",
        created_by=user,
        capabilities=["texting"],
        credentials={"api_key": "example-fictional-key"},
    )


def enqueued(task):
    return DBTaskResult.objects.filter(task_path=task.module_path).count()


class Received:
    """Collects records_received signals for the duration of a test."""

    def __init__(self, testcase):
        self.calls = []
        records_received.connect(self.receive)
        testcase.addCleanup(records_received.disconnect, self.receive)

    def receive(self, sender, **kwargs):
        self.calls.append(kwargs)


class EnableWebhooksTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.connection = make_connection(self.user)

    def test_token_is_hashed_secret_is_encrypted_and_rotation_works(self):
        token, secret = enable_webhooks(self.connection, self.user)
        self.connection.refresh_from_db()
        self.assertNotEqual(self.connection.webhook_token_hash, token)
        self.assertEqual(len(self.connection.webhook_token_hash), 64)
        self.assertEqual(self.connection.webhook_secret, secret)
        with db_connection.cursor() as cursor:
            cursor.execute(
                "SELECT webhook_secret FROM connect_connection WHERE label = %s", ["Example"]
            )
            self.assertNotIn(secret, cursor.fetchone()[0])
        new_token, new_secret = enable_webhooks(self.connection, self.user)
        self.assertNotEqual(new_token, token)
        self.assertNotEqual(new_secret, secret)

    def test_requirements(self):
        with mock.patch.object(ExampleConnector, "webhooks", None):
            with self.assertRaises(ValidationError):
                enable_webhooks(self.connection, self.user)
        provider_spec = replace(ExampleConnector.webhooks, secret_source="provider")
        with mock.patch.object(ExampleConnector, "webhooks", provider_spec):
            with self.assertRaisesMessage(ValidationError, "Paste the signing secret"):
                enable_webhooks(self.connection, self.user)
            _, secret = enable_webhooks(self.connection, self.user, provider_secret="from-provider")
        self.assertEqual(secret, "from-provider")

    def test_disconnect_retires_the_webhook_url(self):
        token, _ = enable_webhooks(self.connection, self.user)
        disconnect(self.connection, self.user)
        self.connection.refresh_from_db()
        self.assertIsNone(self.connection.webhook_token_hash)
        self.assertIsNone(self.connection.webhook_secret)
        response = self.client.post(
            reverse("connector_webhook", args=[token]), data=b"{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 404)


class WebhookEndpointTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.connection = make_connection(self.user)
        self.token, self.secret = enable_webhooks(self.connection, self.user)
        self.url = reverse("connector_webhook", args=[self.token])
        # Providers do not send CSRF tokens; the endpoint must not need one.
        self.client = Client(enforce_csrf_checks=True)

    def deliver(self, body, signature=None):
        signature = example_sign(body, self.secret) if signature is None else signature
        return self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            headers={SIGNATURE_HEADER: signature},
        )

    def test_valid_event_is_stored_encrypted_and_queued_once(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.deliver(event_body())
        self.assertEqual(response.status_code, 200)
        event = WebhookEvent.objects.get()
        self.assertEqual(event.event_id, "evt-1")
        self.assertEqual(event.status, WebhookEvent.Status.QUEUED)
        self.assertEqual(event.payload["body"], "Fictional reply")
        with db_connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM connect_webhookevent WHERE event_id = 'evt-1'")
            self.assertNotIn("Fictional reply", cursor.fetchone()[0])
        self.assertEqual(enqueued(process_webhook_event), 1)

        with self.captureOnCommitCallbacks(execute=True):
            duplicate = self.deliver(event_body())
        self.assertContains(duplicate, "duplicate")
        self.assertEqual(WebhookEvent.objects.count(), 1)
        self.assertEqual(enqueued(process_webhook_event), 1)

    def test_rejections_store_nothing(self):
        self.assertEqual(self.deliver(event_body(), signature="forged").status_code, 401)
        self.assertEqual(self.deliver(event_body(), signature="").status_code, 401)
        self.assertEqual(self.deliver(b"not json").status_code, 400)
        missing_id = json.dumps({"body": "no id"}).encode()
        self.assertEqual(self.deliver(missing_id).status_code, 400)
        with override_settings(CONNECTOR_WEBHOOK_MAX_BYTES=10):
            self.assertEqual(self.deliver(event_body()).status_code, 413)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        unknown = reverse("connector_webhook", args=["not-a-real-token"])
        self.assertEqual(
            self.client.post(unknown, data=b"{}", content_type="application/json").status_code, 404
        )
        self.assertEqual(WebhookEvent.objects.count(), 0)

    def test_inactive_or_webhookless_connections_are_not_found(self):
        Connection.objects.filter(pk=self.connection.pk).update(status=Connection.Status.PAUSED)
        self.assertEqual(self.deliver(event_body()).status_code, 404)
        Connection.objects.filter(pk=self.connection.pk).update(status=Connection.Status.ACTIVE)
        with mock.patch.object(ExampleConnector, "webhooks", None):
            self.assertEqual(self.deliver(event_body()).status_code, 404)

    def test_events_for_undeclared_capabilities_are_rejected(self):
        wrong = WebhookEnvelope(event_id="evt-9", capability="payments", payload={})
        with mock.patch.object(ExampleConnector, "parse_webhook", return_value=wrong):
            self.assertEqual(self.deliver(event_body()).status_code, 400)


class WebhookProcessingTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.connection = make_connection(self.user)
        self.received = Received(self)

    def event(self, **overrides):
        options = {
            "connection": self.connection,
            "event_id": "evt-1",
            "capability": "texting",
            "payload": {"id": "evt-1", "body": "Hi", "sent_at": "2026-01-05T10:00:00+00:00"},
            **overrides,
        }
        return WebhookEvent.objects.create(**options)

    def test_processing_emits_records_and_drops_the_payload(self):
        event = self.event()
        process_webhook_event.call(event.pk)
        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.PROCESSED)
        self.assertEqual(event.records_received, 1)
        self.assertIsNone(event.payload)
        self.assertEqual(event.attempts, 1)
        self.assertEqual(self.received.calls[0]["source"], "webhook")
        self.assertEqual(self.received.calls[0]["records"][0].external_id, "evt-1")
        process_webhook_event.call(event.pk)
        self.assertEqual(len(self.received.calls), 1)

    def test_failures_are_recorded_safely(self):
        event = self.event(payload={"id": "evt-1", "sent_at": "not a date"})
        process_webhook_event.call(event.pk)
        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.FAILED)
        self.assertEqual(event.error, "Unexpected ValueError")
        self.assertIsNotNone(event.payload)
        missing = self.event(
            connection=Connection.objects.create(connector_key="uninstalled", label="Old"),
            event_id="evt-2",
        )
        process_webhook_event.call(missing.pk)
        missing.refresh_from_db()
        self.assertIn("not installed", missing.error)
        process_webhook_event.call(999999)


class SchedulerTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.connection = make_connection(self.user)

    def test_due_syncs_are_queued_once(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(queue_due_syncs(), 1)
        self.assertEqual(enqueued(run_sync), 1)
        run = SyncRun.objects.get()
        self.assertEqual((run.capability, run.status), ("texting", SyncRun.Status.QUEUED))
        self.assertEqual(queue_due_syncs(), 0)

    def test_interval_and_stale_runs(self):
        run = SyncRun.objects.create(
            connection=self.connection, capability="texting", status=SyncRun.Status.SUCCEEDED
        )
        now = run.created_at
        self.assertEqual(queue_due_syncs(now), 0)
        self.assertEqual(queue_due_syncs(now + ExampleConnector.sync_interval), 1)
        SyncRun.objects.exclude(pk=run.pk).update(created_at=now - timedelta(hours=2))
        SyncRun.objects.filter(pk=run.pk).delete()
        self.assertEqual(queue_due_syncs(now), 1)

    def test_only_active_installed_sync_capabilities_are_scheduled(self):
        Connection.objects.filter(pk=self.connection.pk).update(status=Connection.Status.PAUSED)
        Connection.objects.create(
            connector_key="uninstalled", label="Old", enabled_capabilities=["texting"]
        )
        self.assertEqual(queue_due_syncs(), 0)
        Connection.objects.filter(pk=self.connection.pk).update(status=Connection.Status.ACTIVE)
        with mock.patch.object(ExampleConnector, "sync_capabilities", frozenset()):
            self.assertEqual(queue_due_syncs(), 0)

    @override_settings(WORKER_TICK_SECONDS=60)
    def test_heartbeat(self):
        self.assertFalse(worker_is_running())
        tick("worker-a")
        self.assertTrue(worker_is_running())
        self.assertEqual(SyncRun.objects.count(), 1)
        self.assertFalse(worker_is_running(timezone.now() + timedelta(minutes=4)))
        self.assertEqual(str(WorkerHeartbeat.objects.get())[:8], "worker-a")


class RunSyncTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.connection = make_connection(self.user)
        self.received = Received(self)

    def queue(self):
        return SyncRun.objects.create(connection=self.connection, capability="texting")

    def test_successful_sync_records_cursor_and_emits_records(self):
        first = self.queue()
        run_sync.call(first.pk)
        first.refresh_from_db()
        self.assertEqual(first.status, SyncRun.Status.SUCCEEDED)
        self.assertEqual(first.cursor_after, {"after": 3})
        self.assertEqual(first.records_received, 3)
        self.assertEqual(self.received.calls[0]["source"], "sync")
        self.connection.refresh_from_db()
        self.assertIsNotNone(self.connection.last_success_at)

        second = self.queue()
        run_sync.call(second.pk)
        second.refresh_from_db()
        self.assertEqual(second.cursor_before, {"after": 3})
        self.assertEqual(self.received.calls[1]["records"][0].external_id, "example-message-4")
        run_sync.call(second.pk)
        self.assertEqual(len(self.received.calls), 2)

    def test_failures_are_recorded_safely(self):
        run = self.queue()
        with mock.patch.object(
            ExampleConnector, "sync", side_effect=ConnectorError("Provider is down")
        ):
            run_sync.call(run.pk)
        run.refresh_from_db()
        self.assertEqual((run.status, run.error), (SyncRun.Status.FAILED, "Provider is down"))
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.last_error, "Provider is down")

        def explode(sender, **kwargs):
            raise RuntimeError("client detail")

        records_received.connect(explode)
        self.addCleanup(records_received.disconnect, explode)
        run = self.queue()
        run_sync.call(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.error, "Unexpected RuntimeError")

    def test_inactive_connections_are_skipped(self):
        run = self.queue()
        Connection.objects.filter(pk=self.connection.pk).update(status=Connection.Status.PAUSED)
        run_sync.call(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, SyncRun.Status.SKIPPED)
        self.assertEqual(self.received.calls, [])


class RunWorkerCommandTests(TestCase):
    @override_settings(WORKER_TICK_SECONDS=0)
    def test_scheduler_thread_ticks_while_the_task_worker_runs(self):
        ticked = threading.Event()

        def fake_tick(worker_id):
            ticked.set()
            return 0

        def fake_worker(name):
            self.assertEqual(name, "db_worker")
            self.assertTrue(ticked.wait(timeout=5))

        with (
            mock.patch("crm.connect.management.commands.run_worker.tick", side_effect=fake_tick),
            mock.patch(
                "crm.connect.management.commands.run_worker.call_command", side_effect=fake_worker
            ),
        ):
            from django.core.management import call_command

            call_command("run_worker", worker_id="test-worker")
        self.assertTrue(ticked.is_set())


class AdminTests(TestCase):
    def test_worker_and_webhook_admin_pages_load(self):
        user = make_user()
        connection = make_connection(user)
        SyncRun.objects.create(connection=connection, capability="texting")
        WebhookEvent.objects.create(
            connection=connection, event_id="evt-1", capability="texting", payload={"body": "Hi"}
        )
        WorkerHeartbeat.objects.create(worker_id="worker-a", last_seen_at=timezone.now())
        self.client.force_login(user)
        for name in ("syncrun", "webhookevent", "workerheartbeat"):
            response = self.client.get(reverse(f"admin:connect_{name}_changelist"))
            self.assertEqual(response.status_code, 200)
        event = WebhookEvent.objects.get()
        page = self.client.get(reverse("admin:connect_webhookevent_change", args=[event.pk]))
        self.assertNotContains(page, "Hi</")
