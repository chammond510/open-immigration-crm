"""Queues due syncs and other periodic work, and records that a worker is alive."""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Connection, SyncRun, WorkerHeartbeat
from .signals import worker_ticked
from .tasks import run_sync

logger = logging.getLogger(__name__)

# A queued or running sync older than this is treated as abandoned.
STALE_RUN_AFTER = timedelta(hours=1)


def sync_is_due(connection, capability, interval, now):
    last = SyncRun.objects.filter(connection=connection, capability=capability).first()
    if last is None:
        return True
    if last.status in (SyncRun.Status.QUEUED, SyncRun.Status.RUNNING):
        return last.created_at <= now - STALE_RUN_AFTER
    return last.created_at <= now - interval


def queue_sync(connection, capability):
    with transaction.atomic():
        run = SyncRun.objects.create(connection=connection, capability=capability)
        transaction.on_commit(lambda run_id=run.pk: run_sync.enqueue(run_id))
    return run


def queue_due_syncs(now=None):
    """Queue one sync per due capability of each active connection; return how many."""
    now = now or timezone.now()
    queued = 0
    for connection in Connection.objects.filter(status=Connection.Status.ACTIVE):
        cls = connection.connector_class
        if cls is None or cls.sync_interval is None:
            continue
        for capability in sorted(set(connection.enabled_capabilities) & set(cls.sync_capabilities)):
            if sync_is_due(connection, capability, cls.sync_interval, now):
                queue_sync(connection, capability)
                queued += 1
    return queued


def start_syncs(connection, now=None):
    """Queue a sync of each capability the connection syncs, as staff asked, unless one is
    already under way; return how many were queued."""
    now = now or timezone.now()
    cls = connection.connector_class
    if cls is None or connection.status != Connection.Status.ACTIVE:
        return 0
    queued = 0
    for capability in sorted(set(connection.enabled_capabilities) & set(cls.sync_capabilities)):
        last = SyncRun.objects.filter(connection=connection, capability=capability).first()
        busy = last and last.status in (SyncRun.Status.QUEUED, SyncRun.Status.RUNNING)
        if not busy or last.created_at <= now - STALE_RUN_AFTER:
            queue_sync(connection, capability)
            queued += 1
    return queued


def tick(worker_id, now=None):
    now = now or timezone.now()
    WorkerHeartbeat.objects.update_or_create(worker_id=worker_id, defaults={"last_seen_at": now})
    queued = queue_due_syncs(now)
    for receiver, result in worker_ticked.send_robust(sender=None, now=now):
        if isinstance(result, Exception):
            logger.error("Periodic work in %s failed: %r", receiver.__qualname__, result)
        elif isinstance(result, int):
            queued += result
    return queued


def worker_is_running(now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=settings.WORKER_TICK_SECONDS * 3)
    return WorkerHeartbeat.objects.filter(last_seen_at__gte=cutoff).exists()
