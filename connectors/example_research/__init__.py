"""A fictional legal research service for testing research without any network access.

It is hidden from the catalog. Each search answers with three fictional decisions
that repeat the query, so tests and demonstrations can tell searches apart. A
query containing "nothing" finds nothing.
"""

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
class ExampleResearchConnector(Connector):
    key = "example_research"
    name = "Example legal research"
    description = "A fictional legal research service used to test research from a matter."
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.LEGAL_RESEARCH})
    auth = ApiKeyAuth(fields=(CredentialField("api_key", "API key"),))
    data_flows = DataFlows(version=1, sends=("Search words",), receives=("Fictional decisions",))
    outbound_actions = {"search_authorities": Capability.LEGAL_RESEARCH}
    search_scopes = (("", "All sources"), ("board", "Board decisions"))
    hidden = True

    def test_connection(self):
        if not str(self.credentials.get("api_key", "")).startswith("example-"):
            raise ConnectorError("The example research service rejected this key.")

    def perform(self, action, payload):
        query = str(payload.get("query") or "")
        if action != "search_authorities" or not query:
            raise ConnectorError("The example research service needs search words.")
        if "nothing" in query:
            return {"results": [], "total": 0}
        court = "Fictional Board" if payload.get("scope") == "board" else "Fictional Court"
        results = [
            {
                "id": f"example-{number}",
                "citation": f"{number} Fict. Dec. {100 + number}",
                "title": f"Matter of Example {number}",
                "court": court,
                "decided": f"202{number}-03-1{number}",
                "kind": "Fictional decision",
                "standing": "Fictional precedent",
                "url": f"https://research.example.test/decisions/{number}",
                "excerpt": f"A fictional decision about {query}.",
            }
            for number in range(1, 4)
        ]
        return {"results": results, "total": 3}
