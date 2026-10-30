"""A fictional OAuth connector for testing sign-in, hidden from the catalog.

Its provider addresses use the reserved .test domain, so they never resolve.
"""

from crm.connect.base import (
    Capability,
    ConnectionScope,
    Connector,
    ConnectorError,
    DataFlows,
    Maturity,
    OAuthAuth,
)
from crm.connect.registry import register


@register
class ExampleOAuthConnector(Connector):
    key = "example_oauth"
    name = "Example calendar"
    description = "A fictional calendar service used to test OAuth sign-in."
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.CALENDAR})
    auth = OAuthAuth(
        authorize_url="https://auth.example.test/authorize",
        token_url="https://auth.example.test/token",
        scopes={Capability.CALENDAR: ("calendar.read",)},
        extra_authorize_params={"access_type": "offline"},
    )
    connection_scope = ConnectionScope.USER
    data_flows = DataFlows(version=1, receives=("Fictional calendar events",))
    hidden = True

    def test_connection(self):
        if not str(self.credentials.get("access_token", "")).startswith("example-"):
            raise ConnectorError("The example calendar rejected this sign-in.")
