"""Turn CRM changes into events for the automation connections subscribed to them.

Events are queued only after the change commits. Each subscribed connection
gets its own ``EventDelivery``, which the worker sends through the outbound
policy (``crm.connect.policy.send``), so preview mode, the kill switch, and
the hourly limit apply.
"""

from collections.abc import Callable
from dataclasses import dataclass

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from crm.api import serializers
from crm.billing.models import Invoice, Payment
from crm.comms.models import Communication
from crm.connect.base import Capability
from crm.connect.models import Connection
from crm.models import Activity, Contact, IntakeSubmission, Matter, WorkItem
from crm.notices.models import CaseNotice, CaseStatusCheck

from .catalog import DETAIL_FULL, EVENT_TYPES
from .context import current_origin
from .models import EventDelivery


class SiteLinks:
    """Builds absolute links outside a request, from the address saved with the connection."""

    def __init__(self, site_url):
        self.site_url = (site_url or "").rstrip("/")

    def build_absolute_uri(self, path):
        return f"{self.site_url}{path}"


def intake_submission_record(submission, links):
    invite = submission.invite
    return {
        "id": str(submission.pk),
        "form": invite.intake_form.name,
        "contact": str(invite.contact_id),
        "matter": str(invite.matter_id) if invite.matter_id else None,
        "email": submission.email,
        "phone": submission.phone,
        "answers": submission.answers,
        "submitted_at": serializers.iso(submission.submitted_at),
    }


def payment_record(payment, links):
    return {
        "id": str(payment.pk),
        "invoice": str(payment.invoice_id),
        "invoice_number": payment.invoice.number,
        "received_on": serializers.iso(payment.received_on),
        "amount": serializers.money(payment.amount),
        "method": payment.method,
        "deposit_account": payment.deposit_account,
        "reference": payment.reference,
        "note": payment.note,
    }


@dataclass(frozen=True)
class ObjectSpec:
    name: str
    record: Callable
    web_url: str | None = None
    api_url: str | None = None
    related: Callable = lambda instance: {}


def contact_and_matter(instance):
    return {
        "contact": serializers.ref(getattr(instance, "contact_id", None)),
        "matter": serializers.ref(getattr(instance, "matter_id", None)),
    }


OBJECTS = {
    Contact: ObjectSpec("contact", serializers.contact, "contact_detail", "api_contact"),
    Matter: ObjectSpec(
        "matter",
        serializers.matter,
        "matter_detail",
        "api_matter",
        lambda matter: {"contact": str(matter.primary_contact_id)},
    ),
    WorkItem: ObjectSpec(
        "work_item", serializers.work_item, api_url="api_work_item", related=contact_and_matter
    ),
    Activity: ObjectSpec("note", serializers.note, api_url="api_note", related=contact_and_matter),
    Communication: ObjectSpec(
        "communication",
        serializers.communication,
        api_url="api_communication",
        related=contact_and_matter,
    ),
    CaseNotice: ObjectSpec(
        "notice", serializers.notice, api_url="api_notice", related=contact_and_matter
    ),
    CaseStatusCheck: ObjectSpec(
        "status_check",
        serializers.status_check,
        related=lambda check: {"matter": str(check.matter_id)},
    ),
    IntakeSubmission: ObjectSpec(
        "intake_submission",
        intake_submission_record,
        "intake_submission_detail",
        related=lambda submission: {
            "contact": str(submission.invite.contact_id),
            "matter": serializers.ref(submission.invite.matter_id),
        },
    ),
    Invoice: ObjectSpec(
        "invoice",
        serializers.invoice,
        "invoice_detail",
        "api_invoice",
        lambda invoice: {
            "contact": str(invoice.contact_id),
            "matter": serializers.ref(invoice.matter_id),
        },
    ),
    Payment: ObjectSpec(
        "payment", payment_record, related=lambda payment: {"invoice": str(payment.invoice_id)}
    ),
}


def subscribed(event_type):
    """Automation connections that chose this event; most installations have none."""
    connections = Connection.objects.exclude(status=Connection.Status.DISCONNECTED).only(
        "id", "enabled_capabilities", "config"
    )
    return [
        connection
        for connection in connections
        if Capability.AUTOMATION in connection.enabled_capabilities
        and event_type in (connection.config or {}).get("events", [])
    ]


def build_event(event_type, instance, connection, *, changes=None, origin=None, occurred_at):
    """The JSON body of one event, following the Standard Webhooks payload shape."""
    spec = OBJECTS[type(instance)]
    config = connection.config or {}
    links = SiteLinks(config.get("site_url"))
    data = {"object": spec.name, "id": str(instance.pk), **spec.related(instance)}
    if spec.web_url:
        data["web_url"] = links.build_absolute_uri(reverse(spec.web_url, args=[instance.pk]))
    if spec.api_url:
        data["api_url"] = links.build_absolute_uri(reverse(spec.api_url, args=[instance.pk]))
    if changes:
        data["changes"] = changes
    origin = origin or {"via": "crm"}
    data["via"] = origin.get("via", "crm")
    if origin.get("api_token"):
        data["api_token"] = origin["api_token"]
    if config.get("detail") == DETAIL_FULL:
        data["record"] = spec.record(instance, links)
    return {"type": event_type, "timestamp": occurred_at.isoformat(), "data": data}


def emit(event_type, instance, *, changes=None):
    """Queue ``event_type`` for ``instance`` once the current transaction commits.

    Saving the same object again in one transaction sends the event once, and a
    change to an object created in the same transaction is not sent separately.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown event type {event_type!r}.")
    connection_ids = [connection.pk for connection in subscribed(event_type)]
    if not connection_ids:
        return
    pending = instance.__dict__.setdefault("_events_pending", set())
    object_name = event_type.split(".", 1)[0]
    if event_type in pending or (
        event_type.endswith(".updated") and f"{object_name}.created" in pending
    ):
        return
    pending.add(event_type)
    origin = current_origin()
    occurred_at = timezone.now()
    model, pk = type(instance), instance.pk

    def queue():
        pending.discard(event_type)
        queue_deliveries(
            event_type,
            model,
            pk,
            connection_ids,
            changes=changes,
            origin=origin,
            occurred_at=occurred_at,
        )

    transaction.on_commit(queue)


def queue_deliveries(event_type, model, pk, connection_ids, *, changes, origin, occurred_at):
    from .tasks import deliver_event

    instance = model._default_manager.filter(pk=pk).first()
    if instance is None:
        return []
    deliveries = []
    for connection in Connection.objects.filter(pk__in=connection_ids):
        delivery = EventDelivery.objects.create(
            connection=connection,
            event_type=event_type,
            target_type=model.__name__,
            target_id=str(pk),
            payload=build_event(
                event_type,
                instance,
                connection,
                changes=changes,
                origin=origin,
                occurred_at=occurred_at,
            ),
        )
        deliver_event.enqueue(str(delivery.pk))
        deliveries.append(delivery)
    return deliveries
