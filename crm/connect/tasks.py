"""Background work for connections, run by the database-backed worker."""

from django.utils import timezone
from django_tasks import task

from .base import ConnectorError
from .models import Connection, SyncRun, WebhookEvent
from .signals import records_received


def safe_error(exc):
    """Connector errors are written to be shown; anything else may echo client data."""
    if isinstance(exc, ConnectorError):
        return str(exc)[:500]
    return f"Unexpected {exc.__class__.__name__}"


def mark_connection(connection, error=""):
    now = timezone.now()
    if error:
        connection.last_error_at = now
        connection.last_error = error
        connection.save(update_fields=["last_error_at", "last_error", "updated_at"])
    else:
        connection.last_success_at = now
        connection.save(update_fields=["last_success_at", "updated_at"])


@task()
def run_sync(sync_run_id):
    run = SyncRun.objects.select_related("connection").filter(pk=sync_run_id).first()
    if run is None or run.status != SyncRun.Status.QUEUED:
        return
    connection = run.connection
    connector = connection.connector()
    if connector is None or connection.status != Connection.Status.ACTIVE:
        run.status = SyncRun.Status.SKIPPED
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "finished_at"])
        return
    previous = (
        SyncRun.objects.filter(
            connection=connection, capability=run.capability, status=SyncRun.Status.SUCCEEDED
        )
        .order_by("-finished_at")
        .first()
    )
    run.cursor_before = previous.cursor_after if previous else {}
    run.status = SyncRun.Status.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=["cursor_before", "status", "started_at"])
    try:
        batch = connector.sync(run.capability, run.cursor_before)
        records_received.send(
            sender=connector.__class__,
            connection=connection,
            capability=run.capability,
            records=batch.records,
            source="sync",
        )
    except Exception as exc:
        run.status = SyncRun.Status.FAILED
        run.error = safe_error(exc)
        mark_connection(connection, run.error)
    else:
        run.status = SyncRun.Status.SUCCEEDED
        run.cursor_after = batch.cursor
        run.records_received = len(batch.records)
        mark_connection(connection)
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "error", "cursor_after", "records_received", "finished_at"])


@task()
def process_webhook_event(event_id):
    event = WebhookEvent.objects.select_related("connection").filter(pk=event_id).first()
    if event is None or event.status == WebhookEvent.Status.PROCESSED:
        return
    connector = event.connection.connector()
    event.attempts += 1
    try:
        if connector is None:
            raise ConnectorError("This connection's connector is not installed.")
        records = connector.handle_webhook(event.capability, event.payload or {})
        records_received.send(
            sender=connector.__class__,
            connection=event.connection,
            capability=event.capability,
            records=records,
            source="webhook",
        )
    except Exception as exc:
        event.status = WebhookEvent.Status.FAILED
        event.error = safe_error(exc)
    else:
        event.status = WebhookEvent.Status.PROCESSED
        event.records_received = len(records)
        event.error = ""
        # The payload has done its job; keep only the record of receipt.
        event.payload = None
        event.processed_at = timezone.now()
    event.save()
