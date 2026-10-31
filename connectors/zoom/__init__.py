"""Zoom: a video meeting for each of a staff member's appointments, on their own Zoom account.

Each staff member connects their own Zoom account through the firm's Zoom app (a
General app, user-managed, kept unpublished), with OAuth and PKCE. The app's ID and
secret go to Zoom's token endpoint in an HTTP Basic header, as Zoom requires.
Access tokens last an hour, and each refresh returns a new refresh token, which the
framework saves once under a row lock. An unpublished app works only for members of
the firm's own Zoom account, and Zoom sends it no deauthorization notices, so a
revoked app shows up as a refused refresh.

Core asks for a meeting with ``save_meeting`` and removes it with ``delete_meeting``,
only for the owner's own appointments. A meeting is a scheduled meeting (type 2)
with the appointment's title as its topic and its start time; its settings, such as
the waiting room and passcode, are left to the staff member's Zoom defaults. Only
the join link is kept. The host's start link is never stored, because it signs in as
the host. Zoom allows 100 meeting creations or updates a day per person, so core
sends an update only when the title or time changes. Deleting does not email
registrants, and a meeting already deleted at Zoom counts as deleted.

OAuth: https://developers.zoom.us/docs/integrations/oauth/
Meetings: https://developers.zoom.us/docs/api/meetings/
"""

from datetime import UTC, datetime

from crm.connect.base import (
    Capability,
    ConnectionScope,
    Connector,
    ConnectorError,
    DataFlows,
    Maturity,
    OAuthAuth,
)
from crm.connect.http import ProviderClient
from crm.connect.models import ConnectorApp
from crm.connect.oauth import basic_header
from crm.connect.registry import register

API_URL = "https://api.zoom.us/v2"
OAUTH_URL = "https://zoom.us/oauth"
SCOPES = (
    "meeting:read:meeting",
    "meeting:write:meeting",
    "meeting:update:meeting",
    "meeting:delete:meeting",
)
# Zoom's error code for a meeting that no longer exists.
MEETING_GONE = 3001


@register
class ZoomConnector(Connector):
    key = "zoom"
    name = "Zoom"
    description = (
        "Make a Zoom meeting for each of your appointments, on your own Zoom account, and "
        "keep it in step when the appointment moves or is removed."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.VIDEO})
    connection_scope = ConnectionScope.USER
    auth = OAuthAuth(
        authorize_url=f"{OAUTH_URL}/authorize",
        token_url=f"{OAUTH_URL}/token",
        scopes={Capability.VIDEO: SCOPES},
        docs_url="https://developers.zoom.us/docs/integrations/create/",
        token_auth="basic",
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "For each of your appointments: its title and time, to make, move, or delete "
            "its Zoom meeting",
        ),
        receives=("Each meeting's ID and join link",),
    )
    outbound_actions = {"save_meeting": Capability.VIDEO, "delete_meeting": Capability.VIDEO}
    owner_sync_actions = frozenset({"save_meeting", "delete_meeting"})

    def __init__(self, connection):
        super().__init__(connection)
        self.http = ProviderClient(name="Zoom", headers={"Accept": "application/json"})

    def call(self, method, path, *, missing_ok=False, **kwargs):
        """Zoom's answer; None for a meeting that no longer exists, when ``missing_ok``."""
        self.http.headers["Authorization"] = f"Bearer {self.access_token()}"
        response = self.http.request(
            method, API_URL + path, expected=(200, 201, 204, 400, 401, 403, 404, 429), **kwargs
        )
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        if response.status_code < 300:
            return body
        code = body.get("code") if isinstance(body, dict) else None
        if response.status_code == 404 or code == MEETING_GONE:
            if missing_ok:
                return None
            raise ConnectorError("Zoom could not find that meeting.", status=404)
        if response.status_code == 429:
            raise ConnectorError(
                "Zoom's limit on meeting changes was reached. Zoom allows 100 a day for each "
                "person, counted from midnight UTC.",
                status=429,
            )
        if response.status_code == 401:
            raise ConnectorError("Zoom did not accept the sign-in. Sign in again.", status=401)
        if response.status_code == 403:
            raise ConnectorError(
                "Zoom refused the request. Sign in again and allow every permission asked for.",
                status=403,
            )
        raise ConnectorError("Zoom refused the meeting.", status=response.status_code)

    def test_connection(self):
        self.call("GET", "/users/me/meetings", params={"page_size": 1})

    def revoke(self):
        """Zoom revokes a sign-in by its access token."""
        token = self.credentials.get("access_token")
        app = ConnectorApp.objects.filter(connector_key=self.key).first()
        if token and app is not None:
            ProviderClient(name="Zoom", retries=1).request(
                "POST",
                f"{OAUTH_URL}/revoke",
                data={"token": token},
                headers={"Authorization": basic_header(app)},
                expected=(200, 400, 401),
            )

    def save_meeting(self, payload):
        starts = datetime.fromisoformat(payload["starts_at"]).astimezone(UTC)
        body = {
            "topic": payload["title"][:200],
            "type": 2,
            "start_time": starts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration": int(payload["duration_minutes"]),
            "timezone": payload["time_zone"],
        }
        if payload.get("meeting_id"):
            path = f"/meetings/{payload['meeting_id']}"
            if self.call("PATCH", path, json=body, missing_ok=True) is not None:
                meeting = self.call("GET", path, missing_ok=True)
                if meeting is not None:
                    return self.answer(meeting)
        return self.answer(self.call("POST", "/users/me/meetings", json=body))

    @staticmethod
    def answer(meeting):
        if not meeting.get("id") or not str(meeting.get("join_url", "")).startswith("https://"):
            raise ConnectorError("Zoom's answer did not include the meeting's link.")
        return {"id": str(meeting["id"]), "join_url": meeting["join_url"]}

    def perform(self, action, payload):
        if action == "save_meeting":
            return self.save_meeting(payload)
        if action == "delete_meeting":
            self.call(
                "DELETE",
                f"/meetings/{payload['meeting_id']}",
                params={"schedule_for_reminder": "false", "cancel_meeting_reminder": "false"},
                missing_ok=True,
            )
            return payload["meeting_id"]
        raise ConnectorError(f"Zoom cannot {action}.")
