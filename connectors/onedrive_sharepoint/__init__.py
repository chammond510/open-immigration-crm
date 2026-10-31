"""OneDrive and SharePoint: matter documents copied to a folder per matter, through Microsoft Graph.

A superuser connects once for the whole firm, through the firm's own Entra app
registration, and pastes the link of the folder the matter folders go in, ideally in
a SharePoint document library the firm owns. The link is resolved through Graph's
``/shares`` endpoint to the folder's drive and item. Folders and files are named in
the CRM by ``"{drive ID}:{item ID}"``, so later calls need no lookup.

Files up to 4 MiB are uploaded in one request; larger ones through an upload session,
in chunks of a multiple of 320 KiB, sent without the Authorization header as Graph
requires. A name already taken gets a number added rather than replacing anything.
Deleting moves the copy to the site's recycle bin, from which it can be restored.

Drive items: https://learn.microsoft.com/graph/api/resources/driveitem
"""

import base64
import re
from urllib.parse import quote

from django.core.exceptions import ValidationError

from connectors.microsoft_365 import GRAPH_SCOPE, GRAPH_URL, LOGIN_URL, validate_tenant
from crm.connect.base import (
    Capability,
    Connector,
    ConnectorError,
    DataFlows,
    Maturity,
    OAuthAuth,
    SettingField,
)
from crm.connect.http import ProviderClient, check_url
from crm.connect.registry import register

SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024
# SharePoint asks API clients to name themselves this way.
USER_AGENT = "NONISV|OpenImmigrationCRM|OpenImmigrationCRM/1"
RESERVED = re.compile(r"^(\.lock|CON|PRN|AUX|NUL|COM\d|LPT\d|desktop\.ini|forms)$", re.IGNORECASE)
CHUNK = 10 * 320 * 1024
RENAME = {"@microsoft.graph.conflictBehavior": "rename"}


def share_id(link):
    """The ``/shares`` ID for a link: "u!" and the link in unpadded base64url."""
    encoded = base64.urlsafe_b64encode(link.strip().encode()).decode().rstrip("=")
    return f"u!{encoded}"


def validate_link(value):
    try:
        check_url(str(value or "").strip())
    except ConnectorError:
        raise ValidationError(
            "Paste the folder's link from SharePoint or OneDrive; it starts with https://."
        ) from None


def graph_name(name, *, folder=False):
    """A name SharePoint and OneDrive accept, from the one core chose."""
    name = name.replace("_vti_", "vti").lstrip("~").strip()
    if folder:
        name = name.rstrip(". ")
    if not name or RESERVED.match(name):
        name = f"_{name}"
    return name


def split(item):
    """The drive ID and item ID in the CRM's name for an item, as Graph gave them."""
    drive_id, _, item_id = str(item or "").partition(":")
    if not drive_id or not item_id:
        raise ConnectorError("The CRM's record of that item is not one it can use.")
    return drive_id, item_id


def item_path(drive_id, item_id):
    return f"/drives/{quote(drive_id, safe='')}/items/{quote(item_id, safe='')}"


@register
class OneDriveSharePointConnector(Connector):
    key = "onedrive_sharepoint"
    name = "OneDrive and SharePoint"
    description = (
        "Copy each matter's documents into a folder for the matter in the firm's SharePoint "
        "or OneDrive, one way, from the CRM."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.STORAGE})
    auth = OAuthAuth(
        authorize_url=f"{LOGIN_URL}/authorize",
        token_url=f"{LOGIN_URL}/token",
        scopes={Capability.STORAGE: ("offline_access", f"{GRAPH_SCOPE}Files.ReadWrite.All")},
        docs_url="https://learn.microsoft.com/entra/identity-platform/quickstart-register-app",
        app_fields=(
            SettingField(
                "tenant",
                "Directory (tenant) ID",
                help_text="On the app registration's overview page, or the firm's Microsoft "
                "365 domain.",
                validate=validate_tenant,
            ),
        ),
    )
    settings_fields = (
        SettingField(
            "folder",
            "Folder for matter folders",
            help_text="Paste the link of the SharePoint or OneDrive folder the CRM should put "
            "a folder for each matter in, ideally in a document library the firm owns.",
            validate=validate_link,
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
        self.http = ProviderClient(
            GRAPH_URL,
            name="Microsoft Graph",
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )

    def call(self, method, path, *, missing_ok=False, **kwargs):
        """Graph's answer; None for something no longer there, when ``missing_ok``."""
        self.http.headers["Authorization"] = f"Bearer {self.access_token()}"
        try:
            return self.http.json(method, path, **kwargs)
        except ConnectorError as exc:
            if missing_ok and exc.status == 404:
                return None
            if exc.status == 401:
                raise ConnectorError(
                    "Microsoft did not accept the sign-in. Sign in again.", status=401
                ) from None
            if exc.status == 403:
                raise ConnectorError(
                    "Microsoft refused the request. Check that the connected account can add "
                    "files to the chosen folder, and that an administrator consented to the "
                    "app's permissions.",
                    status=403,
                ) from None
            if exc.status == 404:
                raise ConnectorError(
                    "Microsoft could not find the chosen folder. Paste its link again in the "
                    "connection's settings.",
                    status=404,
                ) from None
            raise

    def chosen_folder(self):
        """The chosen folder as "{drive ID}:{item ID}", from its link."""
        found = self.call(
            "GET",
            f"/shares/{share_id(self.setting('folder'))}/driveItem",
            params={"$select": "id,name,folder,parentReference"},
        )
        drive_id = str((found.get("parentReference") or {}).get("driveId") or "")
        if "folder" not in found or not drive_id or not found.get("id"):
            raise ConnectorError("The chosen link is not a folder.")
        return f"{drive_id}:{found['id']}"

    def test_connection(self):
        self.chosen_folder()

    # Copies

    def save_folder(self, payload):
        if payload.get("folder_id"):
            renamed = self.call(
                "PATCH",
                item_path(*split(payload["folder_id"])),
                json={"name": graph_name(payload["name"], folder=True)},
                missing_ok=True,
            )
            if renamed:
                return {"id": payload["folder_id"]}
        drive_id, parent_id = split(self.chosen_folder())
        made = self.call(
            "POST",
            f"{item_path(drive_id, parent_id)}/children",
            # Microsoft's pages differ on where this goes, so it goes in both places.
            params=RENAME,
            json={"name": graph_name(payload["name"], folder=True), "folder": {}, **RENAME},
        )
        made_in = (made.get("parentReference") or {}).get("driveId") or drive_id
        return {"id": f"{made_in}:{made['id']}"}

    def save_file(self, payload):
        drive_id, folder_id = split(payload["folder_id"])
        content = base64.b64decode(payload["content"])
        name = quote(graph_name(payload["name"]), safe="")
        target = f"{item_path(drive_id, folder_id)}:/{name}:"
        if len(content) <= SIMPLE_UPLOAD_LIMIT:
            made = self.call(
                "PUT",
                f"{target}/content",
                params=RENAME,
                content=content,
                headers={"Content-Type": payload["mime_type"] or "application/octet-stream"},
            )
        else:
            made = self.upload_in_chunks(target, content)
        return {"id": f"{drive_id}:{made['id']}"}

    def upload_in_chunks(self, target, content):
        session = self.call("POST", f"{target}/createUploadSession", json={"item": RENAME})
        upload_url = str(session.get("uploadUrl") or "")
        check_url(upload_url)
        # The upload address carries its own authorization; Graph refuses another.
        chunks = ProviderClient(
            name="Microsoft Graph",
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        total, made = len(content), {}
        for start in range(0, total, CHUNK):
            end = min(start + CHUNK, total) - 1
            made = chunks.json(
                "PUT",
                upload_url,
                content=content[start : end + 1],
                headers={"Content-Range": f"bytes {start}-{end}/{total}"},
            )
        if not made.get("id"):
            raise ConnectorError("Microsoft did not finish the upload.")
        return made

    def perform(self, action, payload):
        if action == "save_folder":
            return self.save_folder(payload)
        if action == "save_file":
            return self.save_file(payload)
        if action == "trash_item":
            # Deleting moves the item to the recycle bin, where it can be restored.
            self.call("DELETE", item_path(*split(payload["item_id"])), missing_ok=True)
            return payload["item_id"]
        raise ConnectorError(f"OneDrive and SharePoint cannot {action}.")
