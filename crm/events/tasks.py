"""Event delivery, run by the database-backed worker."""

from datetime import timedelta

from django.utils import timezone
from django_tasks import task

from crm.connect import policy
from crm.connect.models import OutboundRequest
from crm.connect.tasks import mark_connection

from .models import EventDelivery

# Waits before each retry; a delivery that still fails after the last one gives up.
RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=8),
    timedelta(hours=24),
)
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1

DELIVERY_ACTION = "deliver_event"


def settle(delivery, status, error=""):
    delivery.status = status
    delivery.last_error = error[:500]
    delivery.next_attempt_at = None
    delivery.finished_at = timezone.now()
    # The event has been delivered or given up on; keep only the record of it.
    delivery.payload = None


def attempt(delivery):
    """Send one delivery through the outbound policy and decide what happens next."""
    delivery.attempts += 1
    outbound = policy.send(
        delivery.connection,
        DELIVERY_ACTION,
        {"id": str(delivery.pk), "event": delivery.payload},
        is_automation=True,
        summary=delivery.event_type,
    )
    sent = OutboundRequest.Status
    if outbound.status == sent.SENT:
        settle(delivery, EventDelivery.Status.SENT)
        mark_connection(delivery.connection)
    elif outbound.status == sent.PREVIEWED:
        settle(delivery, EventDelivery.Status.PREVIEWED)
    elif outbound.status == sent.FAILED or outbound.decision_code == policy.Codes.RATE_LIMITED:
        if outbound.status == sent.FAILED:
            mark_connection(delivery.connection, outbound.error)
        if delivery.attempts < MAX_ATTEMPTS:
            delivery.status = EventDelivery.Status.RETRYING
            delivery.last_error = outbound.error
            delivery.next_attempt_at = timezone.now() + RETRY_DELAYS[delivery.attempts - 1]
        else:
            settle(delivery, EventDelivery.Status.FAILED, outbound.error)
    else:
        settle(delivery, EventDelivery.Status.BLOCKED, outbound.error)
    delivery.save()
    if delivery.status == EventDelivery.Status.RETRYING:
        deliver_event.using(run_after=delivery.next_attempt_at).enqueue(str(delivery.pk))
    return delivery


@task()
def deliver_event(delivery_id):
    delivery = EventDelivery.objects.select_related("connection").filter(pk=delivery_id).first()
    if delivery is None or delivery.is_settled:
        return
    attempt(delivery)
