import base64
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from crm import document_storage
from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, ExternalRef
from crm.connect.services import acknowledge_data_flows
from crm.connect.testing import ConnectorContract
from crm.models import Document
from crm.tests.factories import make_contact, make_matter, make_user

from . import FOLDER, GoogleDriveConnector, folder_id, validate_folder

PARENT = "FirmMatters0123456789"
LINK = f"https://drive.google.com/drive/folders/{PARENT}?usp=sharing"


def signed_in():
    return {
        "access_token": "fictional-access",
        "refresh_token": "fictional-refresh",
        "token_type": "Bearer",
        "expires_at": time.time() + 3600,
    }


class FakeDrive:
    def __init__(self):
        self.items = {
            PARENT: {"name": "Firm matters", "mimeType": FOLDER, "trashed": False, "add": True}
        }
        self.requests = []
        self.sessions = {}

    def calls(self, method, path=""):
        return [r for r in self.requests if r.method == method and path in r.url.path]

    def make(self, metadata, content=b""):
        item_id = f"drive{len(self.items):04d}abcdefghij"
        self.items[item_id] = {**metadata, "trashed": False, "content": content}
        return item_id

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={})
        if request.headers.get("authorization") != "Bearer fictional-access":
            return httpx.Response(401, json={"error": {"code": 401}})
        path = request.url.path
        if request.method == "PUT":
            # The upload session's address carries everything; it needs no other parameters.
            metadata = self.sessions.pop(request.url.params["upload_id"])
            return httpx.Response(200, json={"id": self.make(metadata, request.read())})
        assert request.url.params.get("supportsAllDrives") == "true"
        if path == "/upload/drive/v3/files":
            upload_id = f"u{len(self.sessions)}"
            self.sessions[upload_id] = json.loads(request.content)
            location = f"https://www.googleapis.com/upload/drive/v3/files?upload_id={upload_id}"
            return httpx.Response(200, headers={"Location": location})
        if path == "/drive/v3/files":
            return httpx.Response(200, json={"id": self.make(json.loads(request.content))})
        item_id = path.rsplit("/", 1)[-1]
        item = self.items.get(item_id)
        if item is None:
            return httpx.Response(404, json={"error": {"code": 404}})
        if request.method == "PATCH":
            item.update(json.loads(request.content))
            return httpx.Response(200, json={"id": item_id})
        return httpx.Response(
            200,
            json={
                "id": item_id,
                "name": item["name"],
                "mimeType": item["mimeType"],
                "trashed": item["trashed"],
                "capabilities": {
                    "canAddChildren": item.get("add", False),
                    "canTrashChildren": item.get("trash", item.get("add", False)),
                },
            },
        )


class FakeDriveMixin:
    def setUp(self):
        super().setUp()
        self.drive = FakeDrive()
        self.enterContext(http.use_transport(httpx.MockTransport(self.drive)))


class GoogleDriveContractTests(FakeDriveMixin, ConnectorContract, TestCase):
    connector_class = GoogleDriveConnector
    valid_credentials = signed_in()
    invalid_credentials = {**signed_in(), "access_token": "expired"}
    connection_config = {"folder": LINK}
    outbound_examples = {
        "save_folder": {"folder_id": "", "name": "MAT-2026-0001 Petition", "crm_id": "1"},
        "save_file": {
            "folder_id": PARENT,
            "name": "passport.pdf",
            "mime_type": "application/pdf",
            "content": base64.b64encode(b"%PDF-1.7").decode(),
            "crm_id": "2",
        },
        "trash_item": {"item_id": "goneAlready0123456"},
    }


class CopyTests(FakeDriveMixin, TestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=Path(directory.name)))
        self.admin = make_user()
        self.contact = make_contact(self.admin)
        self.matter = make_matter(self.contact, self.admin, title="Family petition")
        self.connection = Connection.objects.create(
            connector_key="google_drive",
            label="Firm Drive",
            enabled_capabilities=["storage"],
            credentials=signed_in(),
            config={"folder": LINK},
            preview_mode=False,
        )
        acknowledge_data_flows(self.connection, self.admin)
        for name in ("copy_document", "trash_copy", "rename_folder"):
            real = getattr(document_storage, name)
            fake = SimpleNamespace(enqueue=real.call)
            self.enterContext(mock.patch.object(document_storage, name, fake))

    def document(self, content=b"%PDF-1.7 fictional passport"):
        document = Document(
            matter=self.matter,
            contact=self.contact,
            title="Passport",
            original_filename="passport.pdf",
            mime_type="application/pdf",
            size_bytes=len(content),
        )
        document.file.save("passport.pdf", ContentFile(content), save=False)
        with self.captureOnCommitCallbacks(execute=True):
            document.save()
        return document

    def test_documents_are_copied_renamed_and_trashed_on_drive(self):
        document = self.document()
        folder_ref = ExternalRef.objects.get(content_type__model="matter")
        folder = self.drive.items[folder_ref.external_id]
        self.assertEqual(
            (folder["name"], folder["mimeType"], folder["parents"]),
            (f"{self.matter.matter_number} Family petition", FOLDER, [PARENT]),
        )
        self.assertEqual(folder["appProperties"], {"crmId": str(self.matter.pk)})
        file_ref = ExternalRef.objects.get(content_type__model="document")
        copied = self.drive.items[file_ref.external_id]
        self.assertEqual(copied["content"], b"%PDF-1.7 fictional passport")
        self.assertEqual(
            (copied["name"], copied["parents"], copied["mimeType"]),
            ("passport.pdf", [folder_ref.external_id], "application/pdf"),
        )
        start = self.drive.calls("POST", "/upload/")[-1]
        self.assertEqual(start.url.params["uploadType"], "resumable")
        self.assertEqual(start.headers["x-upload-content-length"], "27")

        self.matter.title = "Family petition, refiled"
        with self.captureOnCommitCallbacks(execute=True):
            self.matter.save()
        self.assertTrue(folder["name"].endswith("refiled"))

        with self.captureOnCommitCallbacks(execute=True):
            document.delete()
        self.assertTrue(copied["trashed"])
        with self.captureOnCommitCallbacks(execute=True):
            self.matter.delete()
        self.assertTrue(folder["trashed"])

    def test_a_folder_deleted_on_drive_is_made_again(self):
        self.document()
        folder_ref = ExternalRef.objects.get(content_type__model="matter")
        del self.drive.items[folder_ref.external_id]
        self.matter.title = "Renamed"
        with self.captureOnCommitCallbacks(execute=True):
            self.matter.save()
        folder_ref = ExternalRef.objects.get(content_type__model="matter")
        self.assertEqual(self.drive.items[folder_ref.external_id]["parents"], [PARENT])

    def test_checking_the_chosen_folder(self):
        connector = GoogleDriveConnector(self.connection)
        connector.test_connection()
        self.drive.items[PARENT]["trash"] = False
        with self.assertRaisesMessage(ConnectorError, "Content manager role"):
            connector.test_connection()
        self.drive.items[PARENT]["add"] = False
        with self.assertRaisesMessage(ConnectorError, "cannot add files"):
            connector.test_connection()
        self.drive.items[PARENT].update(add=True, mimeType="application/pdf")
        with self.assertRaisesMessage(ConnectorError, "not a folder in use"):
            connector.test_connection()
        del self.drive.items[PARENT]
        with self.assertRaisesMessage(ConnectorError, "could not find the chosen folder"):
            connector.test_connection()

    def test_drive_answers(self):
        connector = GoogleDriveConnector(self.connection)
        with mock.patch.object(
            FakeDrive, "__call__", lambda fake, request: httpx.Response(403, json={})
        ):
            with self.assertRaisesMessage(ConnectorError, "the drive has space"):
                connector.test_connection()
        with mock.patch.object(
            FakeDrive, "__call__", lambda fake, request: httpx.Response(200, headers={})
        ):
            with self.assertRaisesMessage(ConnectorError, "did not start the upload"):
                upload = GoogleDriveContractTests.outbound_examples["save_file"]
                connector.perform("save_file", upload)
        with self.assertRaisesMessage(ConnectorError, "cannot send_text"):
            connector.perform("send_text", {})
        connector.revoke()
        self.assertEqual(len(self.drive.calls("POST", "/revoke")), 1)

    def test_folder_links(self):
        self.assertEqual(folder_id(LINK), PARENT)
        self.assertEqual(folder_id(f" {PARENT} "), PARENT)
        validate_folder(LINK)
        for bad in ("", "https://drive.google.com/drive/my-drive", "short"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                validate_folder(bad)
