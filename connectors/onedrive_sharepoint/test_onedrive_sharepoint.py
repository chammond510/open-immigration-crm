import base64
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import unquote

import httpx
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from crm import document_storage
from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, ConnectorApp, ExternalRef
from crm.connect.services import acknowledge_data_flows
from crm.connect.testing import ConnectorContract
from crm.models import Document
from crm.tests.factories import make_contact, make_matter, make_user

from . import (
    CHUNK,
    SIMPLE_UPLOAD_LIMIT,
    OneDriveSharePointConnector,
    graph_name,
    share_id,
    validate_link,
)

LINK = "https://firm.sharepoint.example.test/sites/Clients/Shared%20Documents/Matters"
DRIVE = "b!fictionalDrive01"
PARENT = "01FICTIONALPARENT"
TENANT = "0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0"


def signed_in():
    return {
        "access_token": "fictional-access",
        "refresh_token": "fictional-refresh",
        "token_type": "Bearer",
        "expires_at": time.time() + 3600,
    }


class FakeGraph:
    def __init__(self):
        self.items = {PARENT: {"name": "Matters", "folder": {}, "parent": None}}
        self.requests = []
        self.sessions = {}

    def calls(self, method, path=""):
        return [r for r in self.requests if r.method == method and path in r.url.path]

    def make(self, item):
        item_id = f"01ITEM{len(self.items):04d}"
        self.items[item_id] = item
        return item_id

    def answer(self, item_id, status=200):
        item = self.items[item_id]
        body = {"id": item_id, "name": item["name"], "parentReference": {"driveId": DRIVE}}
        if "folder" in item:
            body["folder"] = {"childCount": 0}
        return httpx.Response(status, json=body)

    def unique(self, name, parent):
        taken = {i["name"] for i in self.items.values() if i.get("parent") == parent}
        return name if name not in taken else f"{name} 1"

    def __call__(self, request):
        self.requests.append(request)
        path = unquote(request.url.path)
        if request.url.host == "upload.example.test":
            assert "authorization" not in request.headers
            session = self.sessions[path]
            session["content"] += request.read()
            start, end_total = request.headers["content-range"].split(" ")[1].split("-")
            end, total = (int(x) for x in end_total.split("/"))
            if end + 1 < total:
                return httpx.Response(202, json={"nextExpectedRanges": [f"{end + 1}-"]})
            item_id = self.make(
                {
                    "name": session["name"],
                    "parent": session["parent"],
                    "content": session["content"],
                }
            )
            return self.answer(item_id, 201)
        if request.headers.get("authorization") != "Bearer fictional-access":
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        if path.startswith("/v1.0/shares/"):
            if path != f"/v1.0/shares/{share_id(LINK)}/driveItem":
                return httpx.Response(404, json={"error": {"code": "itemNotFound"}})
            return self.answer(PARENT)
        rest = path.removeprefix(f"/v1.0/drives/{DRIVE}/items/")
        if rest.endswith("/children"):
            parent = rest.removesuffix("/children")
            body = json.loads(request.content)
            assert body["@microsoft.graph.conflictBehavior"] == "rename"
            name = self.unique(body["name"], parent)
            return self.answer(self.make({"name": name, "folder": {}, "parent": parent}), 201)
        if ":/" in rest:
            parent, _, name = rest.partition(":/")
            name, _, action = name.rpartition(":/")
            if action == "createUploadSession":
                self.sessions[f"/session/{len(self.sessions)}"] = {
                    "name": name,
                    "parent": parent,
                    "content": b"",
                }
                upload = f"https://upload.example.test/session/{len(self.sessions) - 1}"
                return httpx.Response(200, json={"uploadUrl": upload})
            assert request.url.params["@microsoft.graph.conflictBehavior"] == "rename"
            item = {"name": self.unique(name, parent), "parent": parent, "content": request.read()}
            return self.answer(self.make(item), 201)
        item = self.items.get(rest)
        if item is None:
            return httpx.Response(404, json={"error": {"code": "itemNotFound"}})
        if request.method == "PATCH":
            item.update(json.loads(request.content))
            return self.answer(rest)
        if request.method == "DELETE":
            item["recycled"] = True
            del self.items[rest]
            return httpx.Response(204)
        return self.answer(rest)


class FakeGraphMixin:
    def setUp(self):
        super().setUp()
        self.graph = FakeGraph()
        self.enterContext(http.use_transport(httpx.MockTransport(self.graph)))
        ConnectorApp.objects.create(
            connector_key="onedrive_sharepoint",
            client_id="fictional-app",
            client_secret="fictional-secret",
            config={"tenant": TENANT},
        )


class OneDriveContractTests(FakeGraphMixin, ConnectorContract, TestCase):
    connector_class = OneDriveSharePointConnector
    valid_credentials = signed_in()
    invalid_credentials = {**signed_in(), "access_token": "expired"}
    connection_config = {"folder": LINK}
    outbound_examples = {
        "save_folder": {"folder_id": "", "name": "MAT-2026-0001 Petition", "crm_id": "1"},
        "save_file": {
            "folder_id": f"{DRIVE}:{PARENT}",
            "name": "passport.pdf",
            "mime_type": "application/pdf",
            "content": base64.b64encode(b"%PDF-1.7").decode(),
            "crm_id": "2",
        },
        "trash_item": {"item_id": f"{DRIVE}:01GONEALREADY"},
    }


class CopyTests(FakeGraphMixin, TestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=Path(directory.name)))
        self.admin = make_user()
        self.contact = make_contact(self.admin)
        self.matter = make_matter(self.contact, self.admin, title="Family petition")
        self.connection = Connection.objects.create(
            connector_key="onedrive_sharepoint",
            label="Firm SharePoint",
            enabled_capabilities=["storage"],
            credentials=signed_in(),
            config={"folder": LINK},
            preview_mode=False,
        )
        acknowledge_data_flows(self.connection, self.admin)
        for name in ("copy_document", "trash_copy", "rename_folder"):
            real = getattr(document_storage, name)
            self.enterContext(
                mock.patch.object(document_storage, name, SimpleNamespace(enqueue=real.call))
            )

    def document(self, content=b"%PDF-1.7 fictional passport", filename="passport.pdf"):
        document = Document(
            matter=self.matter,
            contact=self.contact,
            title="Passport",
            original_filename=filename,
            mime_type="application/pdf",
            size_bytes=len(content),
        )
        document.file.save(filename, ContentFile(content), save=False)
        with self.captureOnCommitCallbacks(execute=True):
            document.save()
        return document

    def ref(self, model):
        drive_id, _, item_id = ExternalRef.objects.get(
            content_type__model=model
        ).external_id.partition(":")
        self.assertEqual(drive_id, DRIVE)
        return self.graph.items[item_id], item_id

    def test_documents_are_copied_renamed_and_recycled(self):
        document = self.document()
        folder, folder_id = self.ref("matter")
        self.assertEqual(
            (folder["name"], folder["parent"]),
            (f"{self.matter.matter_number} Family petition", PARENT),
        )
        copied, _ = self.ref("document")
        self.assertEqual(
            (copied["name"], copied["parent"], copied["content"]),
            ("passport.pdf", folder_id, b"%PDF-1.7 fictional passport"),
        )
        upload = self.graph.calls("PUT", "/content")[-1]
        self.assertEqual(upload.headers["content-type"], "application/pdf")
        self.assertTrue(upload.headers["user-agent"].startswith("NONISV|"))
        create = self.graph.calls("POST", "/children")[-1]
        self.assertEqual(create.url.params["@microsoft.graph.conflictBehavior"], "rename")

        self.matter.title = "Family petition, refiled"
        with self.captureOnCommitCallbacks(execute=True):
            self.matter.save()
        self.assertTrue(folder["name"].endswith("refiled"))

        with self.captureOnCommitCallbacks(execute=True):
            document.delete()
        self.assertEqual(len(self.graph.calls("DELETE")), 1)
        self.assertEqual(sum(1 for i in self.graph.items.values() if "content" in i), 0)

    def test_large_files_go_in_chunks_without_the_sign_in(self):
        content = b"%PDF-1.7 " + b"x" * (SIMPLE_UPLOAD_LIMIT + CHUNK)
        self.document(content, "large.pdf")
        copied, _ = self.ref("document")
        self.assertEqual(copied["content"], content)
        chunks = self.graph.calls("PUT", "/session/")
        self.assertEqual(len(chunks), 3)
        self.assertEqual(CHUNK % (320 * 1024), 0)

    def test_a_taken_name_gets_a_number_and_a_gone_folder_is_made_again(self):
        self.document()
        self.document(filename="passport.pdf")
        names = sorted(i["name"] for i in self.graph.items.values() if "content" in i)
        self.assertEqual(names, ["passport.pdf", "passport.pdf 1"])
        _, folder_id = self.ref("matter")
        del self.graph.items[folder_id]
        self.matter.title = "Renamed"
        with self.captureOnCommitCallbacks(execute=True):
            self.matter.save()
        folder, _ = self.ref("matter")
        self.assertEqual(folder["parent"], PARENT)

    def test_checking_the_chosen_folder_and_answers(self):
        connector = OneDriveSharePointConnector(self.connection)
        connector.test_connection()
        self.graph.items[PARENT].pop("folder")
        with self.assertRaisesMessage(ConnectorError, "not a folder"):
            connector.test_connection()
        self.connection.config = {"folder": LINK + "/elsewhere"}
        with self.assertRaisesMessage(ConnectorError, "could not find the chosen folder"):
            OneDriveSharePointConnector(self.connection).test_connection()
        with mock.patch.object(
            FakeGraph, "__call__", lambda fake, request: httpx.Response(403, json={})
        ):
            with self.assertRaisesMessage(ConnectorError, "consented"):
                connector.test_connection()
        with self.assertRaisesMessage(ConnectorError, "not one it can use"):
            connector.perform("trash_item", {"item_id": "no-drive"})
        with self.assertRaisesMessage(ConnectorError, "cannot send_text"):
            connector.perform("send_text", {})

    def test_names_sharepoint_accepts(self):
        self.assertEqual(graph_name("~$draft.docx"), "$draft.docx")
        self.assertEqual(graph_name("CON"), "_CON")
        self.assertEqual(graph_name("desktop.ini"), "_desktop.ini")
        self.assertEqual(graph_name("a_vti_b.pdf"), "avtib.pdf")
        self.assertEqual(graph_name("MAT-1 Petition.", folder=True), "MAT-1 Petition")
        self.assertEqual(graph_name("Notes.", folder=False), "Notes.")
        self.assertEqual(graph_name("...", folder=True), "_")

    def test_links(self):
        self.assertEqual(
            share_id("https://contoso.example.test/a b"),
            "u!aHR0cHM6Ly9jb250b3NvLmV4YW1wbGUudGVzdC9hIGI",
        )
        validate_link(LINK)
        with self.assertRaises(ValidationError):
            validate_link("http://firm.sharepoint.example.test/x")
