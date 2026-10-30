"""AI work the worker runs: background features on a schedule, and answers on request.

Consult memos are checked every 15 minutes and task suggestions every hour, only
while the feature is on and a background connection is chosen.
"""

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django_tasks import task

from crm.connect.models import Connection
from crm.models import WorkItem

from . import assistant, memos, suggestions
from .client import AIUnavailable
from .models import AISettings, BackgroundRun, ConsultMemo, Conversation

logger = logging.getLogger(__name__)

INTERVALS = {
    memos.FEATURE.key: timedelta(minutes=15),
    suggestions.FEATURE.key: timedelta(hours=1),
}
# A queued or running run older than this is treated as abandoned.
STALE_RUN_AFTER = timedelta(hours=1)
KEEP_RUNS = 50


def run_is_due(feature, interval, now):
    last = BackgroundRun.objects.filter(feature=feature).first()
    if last is None:
        return True
    if last.status in (BackgroundRun.Status.QUEUED, BackgroundRun.Status.RUNNING):
        return last.created_at <= now - STALE_RUN_AFTER
    return last.created_at <= now - interval


def queue_due(sender=None, now=None, **kwargs):
    """Queue each background feature that is due; return how many. Runs on each worker tick."""
    now = now or timezone.now()
    ai_settings = AISettings.load()
    if ai_settings.background_connection_id is None:
        return 0
    queued = 0
    for feature, interval in INTERVALS.items():
        if not ai_settings.feature_on(feature) or not run_is_due(feature, interval, now):
            continue
        with transaction.atomic():
            run = BackgroundRun.objects.create(feature=feature)
            transaction.on_commit(lambda run_id=run.pk: run_background.enqueue(run_id))
        old = BackgroundRun.objects.filter(feature=feature).values_list("pk", flat=True)
        BackgroundRun.objects.filter(pk__in=list(old[KEEP_RUNS:])).delete()
        queued += 1
    return queued


def last_cursor(feature):
    previous = (
        BackgroundRun.objects.filter(
            feature=feature, status=BackgroundRun.Status.SUCCEEDED, cursor__isnull=False
        )
        .order_by("-finished_at")
        .first()
    )
    return previous.cursor if previous else None


@task()
def run_background(run_id):
    run = BackgroundRun.objects.filter(pk=run_id, status=BackgroundRun.Status.QUEUED).first()
    if run is None:
        return
    run.status = BackgroundRun.Status.RUNNING
    run.save(update_fields=["status"])
    try:
        if not AISettings.load().feature_on(run.feature):
            raise AIUnavailable("The feature was turned off.")
        if run.feature == memos.FEATURE.key:
            run.items = memos.prepare_due()
        else:
            run.items, run.cursor = suggestions.suggest(last_cursor(run.feature))
    except AIUnavailable as exc:
        run.status = BackgroundRun.Status.FAILED
        run.error = str(exc)[:500]
    except Exception as exc:
        logger.exception("Background AI run %s failed.", run.pk)
        run.status = BackgroundRun.Status.FAILED
        run.error = f"Unexpected {exc.__class__.__name__}"
    else:
        run.status = BackgroundRun.Status.SUCCEEDED
    run.finished_at = timezone.now()
    run.save()


@task()
def prepare_memo(appointment_pk, user_pk, connection_pk):
    """Draft one consult memo a staff member asked for, through their chosen connection."""
    appointment = WorkItem.objects.select_related("contact").filter(pk=appointment_pk).first()
    user = get_user_model().objects.filter(pk=user_pk, is_active=True).first()
    if appointment is None or appointment.contact is None or user is None:
        ConsultMemo.objects.filter(appointment_id=appointment_pk).update(
            status=ConsultMemo.Status.FAILED, error="The appointment or person is gone."
        )
        return
    try:
        memos.prepare(
            appointment, actor=user, connection=Connection.objects.filter(pk=connection_pk).first()
        )
    except AIUnavailable:
        pass  # The memo records why.
    except Exception as exc:
        # The memo records the failure; the log keeps no record identifiers.
        logger.exception("Preparing a consult memo failed.")
        ConsultMemo.objects.filter(appointment=appointment).update(
            status=ConsultMemo.Status.FAILED, error=f"Unexpected {exc.__class__.__name__}"
        )


@task()
def answer(conversation_id):
    conversation = (
        Conversation.objects.select_related("owner", "connection", "matter__primary_contact")
        .filter(pk=conversation_id, status=Conversation.Status.WORKING)
        .first()
    )
    if conversation is None:
        return
    try:
        assistant.respond(conversation)
    except Exception as exc:
        logger.exception("The assistant failed to answer in conversation %s.", conversation_id)
        assistant.finish(conversation, error=f"Unexpected {exc.__class__.__name__}")
