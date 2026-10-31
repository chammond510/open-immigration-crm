"""Matter documents copied to the firm's document storage, one way.

When the firm connects a document storage service (a firm-wide connection with the
Document storage capability), the worker copies each matter's documents into a
folder for that matter, inside the folder the firm chose there. The CRM stays the
record: nothing is read back, and changes made in the drive are not copied here.
A new document is copied when it is added; a renamed matter renames its folder;
deleting a document or matter in the CRM moves its copy to the drive's trash.
Documents not copied yet, such as those from before the firm connected or a copy
that failed, are copied by a run every 15 minutes, a batch at a time, recorded as
the connection's Document storage syncs.

Storage connectors declare three outbound actions, all run by the worker through
the outbound policy:

- ``save_folder``: ``{"folder_id": the provider's ID or "", "name", "crm_id"}``;
  makes the folder inside the connection's chosen folder, or renames it, and returns
  ``{"id"}``.
- ``save_file``: ``{"folder_id", "name", "mime_type", "content" (base64), "crm_id"}``;
  returns ``{"id"}``.
- ``trash_item``: ``{"item_id"}``; moves a file or folder to the trash, and treats
  one already gone as done.
"""

import base64
import re
from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.utils import timezone
from django_tasks import task

from crm.connect import policy, refs
from crm.connect.base import Capability
from crm.connect.models import Connection, ExternalRef, OutboundRequest, SyncRun
from crm.connect.scheduler import sync_is_due

from .models import Document, Matter

SAVE_FOLDER, SAVE_FILE, TRASH_ITEM = "save_folder", "save_file", "trash_item"
BATCH = 25
INTERVAL = timedelta(minutes=15)
UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


class NotCopied(Exception):
    """The connection could not take a copy now; a later run tries again."""


def storage_connections():
    """The firm's active document storage connections, oldest first."""
    return [
        connection
        for connection in Connection.objects.filter(
            status=Connection.Status.ACTIVE, owner__isnull=True
        ).order_by("created_at")
        if connection.connector_class is not None
        and Capability.STORAGE in connection.enabled_capabilities
        and connection.connector_class.outbound_actions.get(SAVE_FILE) == Capability.STORAGE
    ]


def safe_name(value):
    return " ".join(UNSAFE.sub(" ", value).split())[:200] or "Untitled"


def folder_name(matter):
    return safe_name(f"{matter.matter_number} {matter.title}")


def ref_for(connection, record):
    return ExternalRef.objects.filter(
        connection=connection,
        content_type=ContentType.objects.get_for_model(type(record)),
        object_id=str(record.pk),
    ).first()


def send(connection, action, payload, target, summary):
    outbound = policy.send(
        connection, action, payload, is_automation=True, target=target, summary=summary
    )
    if outbound.status != OutboundRequest.Status.SENT or not outbound.provider_ref:
        previewed = outbound.status == OutboundRequest.Status.PREVIEWED
        reason = "held in preview mode" if previewed else outbound.error
        raise NotCopied(reason or "not copied")
    return outbound.provider_ref


def matter_folder(connection, matter):
    """The matter's folder at the provider, made or renamed to match the matter."""
    ref, name = ref_for(connection, matter), folder_name(matter)
    if ref is not None and ref.etag == name:
        return ref.external_id
    folder_id = send(
        connection,
        SAVE_FOLDER,
        {"folder_id": ref.external_id if ref else "", "name": name, "crm_id": str(matter.pk)},
        matter,
        "Matter folder kept in the firm's document storage",
    )
    if ref is not None and ref.external_id != folder_id:
        ref.delete()
    refs.link(connection, matter, folder_id, name, created_by_crm=True)
    return folder_id


def copy(connection, document):
    """Copy one document; return True when it was copied now."""
    if ref_for(connection, document) is not None:
        return False
    folder_id = matter_folder(connection, document.matter)
    try:
        with document.file.open("rb") as handle:
            content = handle.read()
    except (OSError, ValueError):
        raise NotCopied("the document's file could not be read") from None
    file_id = send(
        connection,
        SAVE_FILE,
        {
            "folder_id": folder_id,
            "name": safe_name(document.original_filename or document.title),
            "mime_type": document.mime_type,
            "content": base64.b64encode(content).decode(),
            "crm_id": str(document.pk),
        },
        document,
        "Document copied to the firm's document storage",
    )
    refs.link(connection, document, file_id, created_by_crm=True)
    return True


@task()
def copy_document(document_id):
    document = Document.objects.select_related("matter").filter(pk=document_id).first()
    if document is None:
        return
    for connection in storage_connections():
        try:
            copy(connection, document)
        except NotCopied:
            pass  # The next run copies it.


@task()
def rename_folder(matter_id):
    matter = Matter.objects.filter(pk=matter_id).first()
    if matter is None:
        return
    for connection in storage_connections():
        if ref_for(connection, matter) is not None:
            try:
                matter_folder(connection, matter)
            except NotCopied:
                pass


@task()
def trash_copy(connection_id, item_id, model_name, record_pk):
    connection = Connection.objects.filter(pk=connection_id).first()
    if connection is None:
        return
    target = (Document if model_name == "document" else Matter)(pk=record_pk)
    policy.send(
        connection,
        TRASH_ITEM,
        {"item_id": item_id},
        is_automation=True,
        target=target,
        summary="Copy moved to the trash in the firm's document storage",
    )


def pending(connection, limit):
    """Documents with no copy at this connection yet, oldest first."""
    copied = set(
        ExternalRef.objects.filter(
            connection=connection, content_type=ContentType.objects.get_for_model(Document)
        ).values_list("object_id", flat=True)
    )
    found = []
    for document in Document.objects.select_related("matter").order_by("created_at"):
        if str(document.pk) not in copied:
            found.append(document)
            if len(found) == limit:
                break
    return found


@task()
def copy_pending(run_id):
    run = SyncRun.objects.select_related("connection").filter(pk=run_id).first()
    if run is None or run.status != SyncRun.Status.QUEUED:
        return
    run.status, run.started_at = SyncRun.Status.RUNNING, timezone.now()
    run.save(update_fields=["status", "started_at"])
    copied = 0
    try:
        for document in pending(run.connection, BATCH):
            copied += copy(run.connection, document)
    except NotCopied as exc:
        run.status, run.error = SyncRun.Status.FAILED, str(exc)[:500]
    else:
        run.status = SyncRun.Status.SUCCEEDED
    run.records_received, run.finished_at = copied, timezone.now()
    run.save(update_fields=["status", "error", "records_received", "finished_at"])


def queue_due(sender=None, now=None, **kwargs):
    """Queue a run for each storage connection that is due; return how many."""
    now = now or timezone.now()
    queued = 0
    for connection in storage_connections():
        if connection.preview_mode or not sync_is_due(
            connection, Capability.STORAGE, INTERVAL, now
        ):
            continue
        with transaction.atomic():
            run = SyncRun.objects.create(connection=connection, capability=Capability.STORAGE)
            transaction.on_commit(lambda run_id=run.pk: copy_pending.enqueue(run_id))
        queued += 1
    return queued


def document_saved(sender, instance, created=False, raw=False, **kwargs):
    if created and not raw and storage_connections():
        transaction.on_commit(lambda: copy_document.enqueue(str(instance.pk)))


def matter_saved(sender, instance, raw=False, **kwargs):
    if raw:
        return
    for connection in storage_connections():
        ref = ref_for(connection, instance)
        if ref is not None and ref.etag != folder_name(instance):
            transaction.on_commit(lambda: rename_folder.enqueue(str(instance.pk)))
            return


def record_deleted(sender, instance, **kwargs):
    """Trash the copies of a deleted document or matter, and forget them."""
    model_name = sender._meta.model_name
    found = ExternalRef.objects.filter(
        content_type=ContentType.objects.get_for_model(sender),
        object_id=str(instance.pk),
        created_by_crm=True,
    ).select_related("connection")
    record_pk = str(instance.pk)
    for ref in found:
        cls = ref.connection.connector_class
        if cls is not None and TRASH_ITEM in cls.outbound_actions:
            transaction.on_commit(
                lambda ref=ref: trash_copy.enqueue(
                    str(ref.connection_id), ref.external_id, model_name, record_pk
                )
            )
        ref.delete()


def connect():
    from crm.connect.signals import worker_ticked

    post_save.connect(document_saved, sender=Document, dispatch_uid="storage-document-saved")
    post_save.connect(matter_saved, sender=Matter, dispatch_uid="storage-matter-saved")
    post_delete.connect(record_deleted, sender=Document, dispatch_uid="storage-document-deleted")
    post_delete.connect(record_deleted, sender=Matter, dispatch_uid="storage-matter-deleted")
    worker_ticked.connect(queue_due, dispatch_uid="storage-copy-pending")
