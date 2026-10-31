"""Google Drive: matter documents copied to a folder per matter on the firm's Drive.

A superuser connects once for the whole firm, through the firm's own Google Cloud
app, and chooses the folder the matter folders go in, ideally on a shared drive so
the copies belong to the firm rather than to one person.

It asks for the ``drive`` scope. The narrower ``drive.file`` scope reaches only what
the app made or what someone picked with Google's Picker, so it could not use the
folder the firm chose without loading Google's scripts into the CRM's pages. ``drive``
is a restricted scope; an Internal app on the firm's Google Workspace needs no
verification for it. The connector only ever touches the chosen folder and what it
made there, and the setup guide suggests connecting an account that can see nothing
but that shared drive. Every request names shared
drives (``supportsAllDrives``). Uploads are resumable uploads: one request starts
the upload with the file's name, folder, and the CRM's ID for it in private
``appProperties``, and a second sends the bytes. Deleting moves the copy to Drive's
trash, from which it can be restored.

Drive API: https://developers.google.com/workspace/drive/api/guides/about-sdk
"""

import base64
import re

from django.core.exceptions import ValidationError

from crm.connect.base import (
    Capability,
    Connector,
    ConnectorError,
    DataFlows,
    Maturity,
    OAuthAuth,
    SettingField,
)
from crm.connect.http import ProviderClient
from crm.connect.registry import register

FILES_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
FOLDER = "application/vnd.google-apps.folder"
SCOPE = "https://www.googleapis.com/auth/drive"
ALL_DRIVES = {"supportsAllDrives": "true"}
FOLDER_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
FOLDER_LINK = re.compile(r"/folders/([A-Za-z0-9_-]{10,200})")


def folder_id(value):
    """The folder ID in a Drive folder link, or the ID itself."""
    value = str(value or "").strip()
    found = FOLDER_LINK.search(value)
    return found.group(1) if found else value


def validate_folder(value):
    if not FOLDER_ID.match(folder_id(value)):
        raise ValidationError(
            "Paste the folder's link from Drive, such as "
            "https://drive.google.com/drive/folders/..., or its ID."
        )


@register
class GoogleDriveConnector(Connector):
    key = "google_drive"
    name = "Google Drive"
    description = (
        "Copy each matter's documents into a folder for the matter on the firm's Google "
        "Drive, one way, from the CRM."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.STORAGE})
    auth = OAuthAuth(
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes={Capability.STORAGE: (SCOPE,)},
        docs_url="https://developers.google.com/workspace/drive/api/guides/about-auth",
        # A refresh token is issued only for offline access, and again only on consent.
        extra_authorize_params={"access_type": "offline", "prompt": "consent"},
    )
    settings_fields = (
        SettingField(
            "folder",
            "Folder for matter folders",
            help_text="Paste the link of the Drive folder the CRM should put a folder for each "
            "matter in, ideally on a shared drive the firm owns.",
            validate=validate_folder,
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "A folder for each matter, named with its number and title, and a copy of each of "
            "its documents",
        ),
        receives=("The IDs of the folders and files it makes",),
    )
    outbound_actions = {
        "save_folder": Capability.STORAGE,
        "save_file": Capability.STORAGE,
        "trash_item": Capability.STORAGE,
    }

    def __init__(self, connection):
        super().__init__(connection)
        self.http = ProviderClient(name="Google Drive", headers={"Accept": "application/json"})

    @property
    def parent(self):
        return folder_id(self.setting("folder"))

    def request(self, method, url, *, missing_ok=False, **kwargs):
        """Drive's answer; None for something no longer there, when ``missing_ok``."""
        self.http.headers["Authorization"] = f"Bearer {self.access_token()}"
        params = {**ALL_DRIVES, **kwargs.pop("params", {})}
        try:
            return self.http.request(method, url, params=params, **kwargs)
        except ConnectorError as exc:
            if missing_ok and exc.status == 404:
                return None
            if exc.status == 401:
                raise ConnectorError(
                    "Google did not accept the sign-in. Sign in again.", status=401
                ) from None
            if exc.status == 403:
                raise ConnectorError(
                    "Google Drive refused the request. Check that the connected account can "
                    "add files to the chosen folder, and that the drive has space.",
                    status=403,
                ) from None
            if exc.status == 404:
                raise ConnectorError(
                    "Google Drive could not find the chosen folder. Choose it again in the "
                    "connection's settings.",
                    status=404,
                ) from None
            raise

    def call(self, method, url, **kwargs):
        response = self.request(method, url, **kwargs)
        if response is None:
            return None
        try:
            return response.json() if response.content else {}
        except ValueError:
            raise ConnectorError("Google Drive's answer was not understood.") from None

    def test_connection(self):
        found = self.call(
            "GET",
            f"{FILES_URL}/{self.parent}",
            params={
                "fields": "id,name,mimeType,trashed,capabilities(canAddChildren,canTrashChildren)"
            },
        )
        if found.get("mimeType") != FOLDER or found.get("trashed"):
            raise ConnectorError("The chosen Drive item is not a folder in use.")
        able = found.get("capabilities") or {}
        if not able.get("canAddChildren"):
            raise ConnectorError("The connected account cannot add files to the chosen folder.")
        if not able.get("canTrashChildren"):
            raise ConnectorError(
                "The connected account can add files to the chosen folder but not move them to "
                "the trash. On a shared drive, give it the Content manager role."
            )

    def revoke(self):
        token = self.credentials.get("refresh_token") or self.credentials.get("access_token")
        if token:
            ProviderClient(name="Google", retries=1).request(
                "POST", REVOKE_URL, data={"token": token}, expected=(200, 400)
            )

    # Copies

    def save_folder(self, payload):
        if payload.get("folder_id"):
            renamed = self.call(
                "PATCH",
                f"{FILES_URL}/{payload['folder_id']}",
                params={"fields": "id"},
                json={"name": payload["name"]},
                missing_ok=True,
            )
            if renamed:
                return {"id": renamed["id"]}
        made = self.call(
            "POST",
            FILES_URL,
            params={"fields": "id"},
            json={
                "name": payload["name"],
                "mimeType": FOLDER,
                "parents": [self.parent],
                "appProperties": {"crmId": payload["crm_id"]},
            },
        )
        return {"id": made["id"]}

    def save_file(self, payload):
        content = base64.b64decode(payload["content"])
        started = self.request(
            "POST",
            UPLOAD_URL,
            params={"uploadType": "resumable", "fields": "id"},
            json={
                "name": payload["name"],
                "parents": [payload["folder_id"]],
                "mimeType": payload["mime_type"],
                "appProperties": {"crmId": payload["crm_id"]},
            },
            headers={
                "X-Upload-Content-Type": payload["mime_type"],
                "X-Upload-Content-Length": str(len(content)),
            },
        )
        session = started.headers.get("location", "")
        if not session.startswith("https://www.googleapis.com/"):
            raise ConnectorError("Google Drive did not start the upload.")
        sent = self.http.request(
            "PUT",
            session,
            content=content,
            headers={"Content-Type": payload["mime_type"]},
            expected=(200, 201),
        )
        try:
            return {"id": sent.json()["id"]}
        except (ValueError, KeyError):
            raise ConnectorError("Google Drive's answer was not understood.") from None

    def perform(self, action, payload):
        if action == "save_folder":
            return self.save_folder(payload)
        if action == "save_file":
            return self.save_file(payload)
        if action == "trash_item":
            self.call(
                "PATCH",
                f"{FILES_URL}/{payload['item_id']}",
                params={"fields": "id"},
                json={"trashed": True},
                missing_ok=True,
            )
            return payload["item_id"]
        raise ConnectorError(f"Google Drive cannot {action}.")
