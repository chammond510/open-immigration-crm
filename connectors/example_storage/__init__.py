"""A fictional document storage service for testing copies without any network access.

It is hidden from the catalog. It keeps its folders and files in memory, in
``ExampleStorageConnector.items``, so tests can look at what was copied; tests
clear it between runs. A file named with "refuse" in it is refused.
"""

import itertools

from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
)
from crm.connect.registry import register


@register
class ExampleStorageConnector(Connector):
    key = "example_storage"
    name = "Example document storage"
    description = "A fictional drive used to test copying matter documents."
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.STORAGE})
    auth = ApiKeyAuth(fields=(CredentialField("api_key", "API key"),))
    data_flows = DataFlows(
        version=1, sends=("Matter folders and document files",), receives=("Fictional IDs",)
    )
    outbound_actions = {
        "save_folder": Capability.STORAGE,
        "save_file": Capability.STORAGE,
        "trash_item": Capability.STORAGE,
    }
    hidden = True
    items = {}
    ids = itertools.count(1)

    def test_connection(self):
        if not str(self.credentials.get("api_key", "")).startswith("example-"):
            raise ConnectorError("The example storage service rejected this key.")

    def perform(self, action, payload):
        items = ExampleStorageConnector.items
        if action == "trash_item":
            if payload["item_id"] in items:
                items[payload["item_id"]]["trashed"] = True
            return payload["item_id"]
        if action == "save_folder" and payload["folder_id"] in items:
            items[payload["folder_id"]]["name"] = payload["name"]
            return {"id": payload["folder_id"]}
        if "refuse" in payload["name"]:
            raise ConnectorError("The example storage service refused the file.")
        item_id = f"item-{next(self.ids)}"
        items[item_id] = {**payload, "kind": action, "trashed": False}
        return {"id": item_id}
