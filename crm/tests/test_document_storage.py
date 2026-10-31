import base64
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm import document_storage
from crm.connect.models import Connection, ExternalRef, OutboundRequest, SyncRun
from crm.connect.registry import registry
from crm.connect.services import acknowledge_data_flows, create_connection, needs_worker
from crm.models import Document

from .factories import make_contact, make_matter, make_user


def storage():
    return registry.get("example_storage").items


class DocumentStorageTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=Path(directory.name)))
        storage().clear()
        self.admin = make_user()
        self.contact = make_contact(self.admin, first_name="Lena", last_name="Fictional")
        self.matter = make_matter(self.contact, self.admin, title="Family petition")
        self.connection = create_connection(
            "example_storage",
            label="Firm drive",
            created_by=self.admin,
            capabilities=["storage"],
            credentials={"api_key": "example-fictional"},
        )
        acknowledge_data_flows(self.connection, self.admin)
        Connection.objects.filter(pk=self.connection.pk).update(preview_mode=False)
        self.connection.refresh_from_db()
        self.copy_document = document_storage.copy_document
        task = SimpleNamespace(enqueue=self.copy_document.call)
        self.enterContext(mock.patch.object(document_storage, "copy_document", task))

    def document(self, title="Passport", filename="passport.pdf", content=b"%PDF-1.7 x"):
        document = Document(
            matter=self.matter,
            contact=self.contact,
            title=title,
            original_filename=filename,
            mime_type="application/pdf",
            size_bytes=len(content),
        )
        document.file.save(filename, ContentFile(content), save=False)
        with self.captureOnCommitCallbacks(execute=True):
            document.save()
        return document

    def items(self, kind):
        return [item for item in storage().values() if item["kind"] == kind]

    def test_a_new_document_is_copied_into_the_matters_folder(self):
        document = self.document(filename="a:b/c?.pdf")
        [folder] = self.items("save_folder")
        [copied] = self.items("save_file")
        self.assertEqual(folder["name"], f"{self.matter.matter_number} Family petition")
        self.assertEqual(copied["name"], "a b c .pdf")
        self.assertEqual(base64.b64decode(copied["content"]), b"%PDF-1.7 x")
        self.assertEqual(copied["crm_id"], str(document.pk))
        self.assertEqual(ExternalRef.objects.count(), 2)
        # A second document reuses the folder.
        self.document("Birth certificate", "birth.pdf")
        self.assertEqual((len(self.items("save_folder")), len(self.items("save_file"))), (1, 2))
        requests = OutboundRequest.objects.values_list("summary", flat=True)
        self.assertIn("Document copied to the firm's document storage", requests)

    def test_renaming_the_matter_renames_its_folder(self):
        self.document()
        self.matter.title = "Family petition, refiled"
        task = SimpleNamespace(enqueue=document_storage.rename_folder.call)
        with mock.patch.object(document_storage, "rename_folder", task):
            with self.captureOnCommitCallbacks(execute=True):
                self.matter.save()
            with self.captureOnCommitCallbacks(execute=True) as unchanged:
                self.matter.save()
        [folder] = self.items("save_folder")
        self.assertTrue(folder["name"].endswith("Family petition, refiled"))
        self.assertEqual(unchanged, [])

    def test_deleting_moves_copies_to_the_trash(self):
        document = self.document()
        task = SimpleNamespace(enqueue=document_storage.trash_copy.call)
        with mock.patch.object(document_storage, "trash_copy", task):
            with self.captureOnCommitCallbacks(execute=True):
                document.delete()
            self.assertTrue(self.items("save_file")[0]["trashed"])
            with self.captureOnCommitCallbacks(execute=True):
                self.matter.delete()
        self.assertTrue(self.items("save_folder")[0]["trashed"])
        self.assertFalse(ExternalRef.objects.exists())
        document_storage.trash_copy.call(
            "00000000-0000-0000-0000-000000000000", "x", "document", "y"
        )

    def test_documents_not_copied_yet_are_copied_by_the_next_run(self):
        Connection.objects.filter(pk=self.connection.pk).update(preview_mode=True)
        self.document()
        self.document("Refuse me", "refuse.pdf")
        self.assertEqual(self.items("save_file"), [])
        self.assertEqual(document_storage.queue_due(), 0)  # Not in preview mode.
        Connection.objects.filter(pk=self.connection.pk).update(preview_mode=False)
        with self.captureOnCommitCallbacks(execute=True) as queued:
            self.assertEqual(document_storage.queue_due(), 1)
        self.assertEqual(len(queued), 1)
        run = SyncRun.objects.get()
        document_storage.copy_pending.call(run.pk)
        run.refresh_from_db()
        # The first is copied; the refused one stops the run, to be tried again later.
        self.assertEqual((run.status, run.records_received), ("failed", 1))
        self.assertIn("refused", run.error)
        self.assertTrue(needs_worker(self.connection))
        self.client.force_login(self.admin)
        page = self.client.get(reverse("connection_detail", args=[self.connection.pk]))
        self.assertContains(page, "Recent syncs")
        self.assertContains(page, "1 copied")
        self.assertEqual(document_storage.queue_due(), 0)  # Not due for 15 minutes.
        later = timezone.now() + timedelta(minutes=16)
        self.assertEqual(document_storage.queue_due(now=later), 1)
        Document.objects.filter(title="Refuse me").delete()
        run = SyncRun.objects.first()
        document_storage.copy_pending.call(run.pk)
        run.refresh_from_db()
        self.assertEqual((run.status, run.records_received), ("succeeded", 0))
        document_storage.copy_pending.call(run.pk)  # Already finished: nothing happens.

    def test_files_that_cannot_be_read_and_no_connection(self):
        document = self.document()
        ExternalRef.objects.filter(object_id=str(document.pk)).delete()
        document.file.delete(save=False)
        with self.assertRaisesMessage(document_storage.NotCopied, "could not be read"):
            document_storage.copy(self.connection, document)
        self.copy_document.call(str(document.pk))
        self.copy_document.call("00000000-0000-0000-0000-000000000000")
        document_storage.rename_folder.call("00000000-0000-0000-0000-000000000000")
        self.connection.delete()
        self.assertEqual(document_storage.storage_connections(), [])
        self.document("Later", "later.pdf")
        self.assertEqual(len(self.items("save_file")), 1)
