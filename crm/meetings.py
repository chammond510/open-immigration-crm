"""Video meetings for appointments.

An appointment assigned to a staff member who connected a video meeting service
gets a meeting there, made by the worker through the outbound policy, and its join
link is kept on the appointment (``WorkItem.meeting_url``) for staff to share. The
meeting moves when the appointment's title or time changes, and is deleted when the
appointment is deleted, unscheduled, or given to someone else. A finished
appointment keeps its meeting. Only meetings the CRM made are changed, and a link
staff pasted by hand is never replaced.

Video connectors declare ``save_meeting`` and ``delete_meeting``, and list both in
``owner_sync_actions``, so they run only for their owner's own appointments. The
``save_meeting`` payload is ``{"meeting_id": the provider's ID or "", "title",
"starts_at" (ISO 8601), "duration_minutes", "time_zone", "crm_id"}``; it returns
``{"id", "join_url"}``, making a new meeting when ``meeting_id`` is empty or gone.
Only the title and time leave the CRM. Each meeting is updated only when they
change, since providers limit how often a meeting can be changed.
"""

import hashlib

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django_tasks import task

from crm.connect import policy, refs
from crm.connect.base import Capability
from crm.connect.models import Connection, ExternalRef, OutboundRequest

from .appointments import appointment_saved as calendar_saved
from .models import WorkItem

SAVE_MEETING, DELETE_MEETING = "save_meeting", "delete_meeting"
MEETING_MINUTES = 60


def video_connections(user):
    """The active video meeting services ``user`` connected, oldest first."""
    if user is None:
        return []
    return [
        connection
        for connection in Connection.objects.filter(
            owner=user, status=Connection.Status.ACTIVE
        ).order_by("created_at")
        if connection.connector_class is not None
        and Capability.VIDEO in connection.enabled_capabilities
        and connection.connector_class.outbound_actions.get(SAVE_MEETING) == Capability.VIDEO
    ]


def meeting_refs(item):
    """Links from this appointment to meetings the CRM made."""
    found = ExternalRef.objects.filter(
        content_type__app_label="crm",
        content_type__model="workitem",
        object_id=str(item.pk),
        created_by_crm=True,
    ).select_related("connection")
    return [
        ref
        for ref in found
        if ref.connection.connector_class is not None
        and DELETE_MEETING in ref.connection.connector_class.outbound_actions
    ]


def fingerprint(item):
    """What the meeting was last made from: its title and time."""
    sent = f"{item.title}\n{item.scheduled_for.isoformat()}"
    return hashlib.sha256(sent.encode()).hexdigest()


def meeting_payload(item, meeting_id):
    return {
        "meeting_id": meeting_id,
        "title": item.title,
        "starts_at": item.scheduled_for.isoformat(),
        "duration_minutes": MEETING_MINUTES,
        "time_zone": settings.TIME_ZONE,
        "crm_id": str(item.pk),
    }


def set_link(item, url):
    """Keep the link without saving the appointment, which would push it again."""
    item.meeting_url = url
    WorkItem.objects.filter(pk=item.pk).update(meeting_url=url)


@task()
def push_meeting(item_id):
    """Make, move, or remove the meeting for one appointment."""
    item = WorkItem.objects.filter(pk=item_id, kind=WorkItem.Kind.APPOINTMENT).first()
    if item is None:
        return
    wanted = video_connections(item.assigned_to)[:1] if item.scheduled_for else []
    connection = wanted[0] if wanted else None
    ref = None
    for found in meeting_refs(item):
        if connection is not None and found.connection_id == connection.pk and ref is None:
            ref = found
        else:
            remove_meeting(found.connection, found.external_id, item.pk)
            found.delete()
            set_link(item, "")
    if item.is_completed or connection is None:
        return
    if item.meeting_url and ref is None:
        # Staff pasted a link of their own.
        return
    if ref is not None and ref.etag == fingerprint(item):
        return
    outbound = policy.send(
        connection,
        SAVE_MEETING,
        meeting_payload(item, ref.external_id if ref else ""),
        is_automation=True,
        target=item,
        summary="Video meeting made for the staff member's appointment",
    )
    if outbound.status != OutboundRequest.Status.SENT or not outbound.provider_ref:
        return
    answer = outbound.response if isinstance(outbound.response, dict) else {}
    if ref is not None and ref.external_id != outbound.provider_ref:
        ref.delete()
    refs.link(connection, item, outbound.provider_ref, fingerprint(item), created_by_crm=True)
    url = str(answer.get("join_url") or "")
    if url.startswith("https://") and url != item.meeting_url:
        set_link(item, url)
        # Calendars that show the appointment show its link too.
        calendar_saved(WorkItem, item)


def remove_meeting(connection, meeting_id, item_pk):
    # The meeting is on its owner's own account, made for their appointment.
    target = WorkItem(pk=item_pk, assigned_to_id=connection.owner_id)
    policy.send(
        connection,
        DELETE_MEETING,
        {"meeting_id": meeting_id},
        is_automation=True,
        target=target,
        summary="Video meeting removed with the staff member's appointment",
    )


@task()
def remove_appointment_meeting(connection_id, meeting_id, item_pk):
    connection = Connection.objects.filter(pk=connection_id).first()
    if connection is not None:
        remove_meeting(connection, meeting_id, item_pk)


def appointment_saved(sender, instance, raw=False, **kwargs):
    if raw or instance.kind != WorkItem.Kind.APPOINTMENT:
        return
    if video_connections(instance.assigned_to) or meeting_refs(instance):
        transaction.on_commit(lambda: push_meeting.enqueue(instance.pk))


def appointment_deleted(sender, instance, **kwargs):
    if instance.kind != WorkItem.Kind.APPOINTMENT:
        return
    item_pk = instance.pk
    for ref in meeting_refs(instance):
        transaction.on_commit(
            lambda ref=ref: remove_appointment_meeting.enqueue(
                str(ref.connection_id), ref.external_id, item_pk
            )
        )
        ref.delete()


def connect():
    post_save.connect(appointment_saved, sender=WorkItem, dispatch_uid="meetings-push")
    post_delete.connect(appointment_deleted, sender=WorkItem, dispatch_uid="meetings-remove")
