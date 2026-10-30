"""Queues due syncs and records that a worker is alive."""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Connection, SyncRun, WorkerHeartbeat
from .tasks import run_sync

# A queued or running sync older than this is treated as abandoned.
STALE_RUN_AFTER = timedelta(hours=1)


def sync_is_due(connection, capability, interval, now):
    last = SyncRun.objects.filter(connection=connection, capability=capability).first()
    if last is None:
        return True
    if last.status in (SyncRun.Status.QUEUED, SyncRun.Status.RUNNING):
        return last.created_at <= now - STALE_RUN_AFTER
    return last.created_at <= now - interval


def queue_due_syncs(now=None):
    """Queue one sync per due capability of each active connection; return how many."""
    now = now or timezone.now()
    queued = 0
    for connection in Connection.objects.filter(status=Connection.Status.ACTIVE):
        cls = connection.connector_class
        if cls is None:
            continue
        for capability in sorted(set(connection.enabled_capabilities) & set(cls.sync_capabilities)):
            if not sync_is_due(connection, capability, cls.sync_interval, now):
                continue
            with transaction.atomic():
                run = SyncRun.objects.create(connection=connection, capability=capability)
                transaction.on_commit(lambda run_id=run.pk: run_sync.enqueue(run_id))
            queued += 1
    return queued


def tick(worker_id, now=None):
    now = now or timezone.now()
    WorkerHeartbeat.objects.update_or_create(worker_id=worker_id, defaults={"last_seen_at": now})
    return queue_due_syncs(now)


def worker_is_running(now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=settings.WORKER_TICK_SECONDS * 3)
    return WorkerHeartbeat.objects.filter(last_seen_at__gte=cutoff).exists()
