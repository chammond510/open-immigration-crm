"""Google Workspace: each staff member's Gmail and Google Calendar.

Each staff member signs in with the firm's own Google Cloud app (OAuth with PKCE).

Mail: the first sync brings in the last 30 days of the inbox and sent mail, then
Gmail's history is followed from where the last sync stopped, page by page. Only
headers and Gmail's short snippet are read; full bodies and attachments stay in
Gmail. Spam, drafts, chats, Gmail's Promotions, Social, Updates, and Forums tabs,
and bulk or automated mail are skipped. Email is sent as plain text through the
outbound policy.

Calendar: events on the primary calendar are pulled with Google's sync tokens,
and appointments assigned to the staff member are put on it with no attendees and
``sendUpdates=none``, so Google emails nobody.

API references: https://developers.google.com/workspace/gmail/api/reference/rest
and https://developers.google.com/workspace/calendar/api/v3/reference
"""

import base64
import html
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses

from django.utils import timezone

from crm.connect.base import (
    Capability,
    ConnectionScope,
    Connector,
    ConnectorError,
    DataFlows,
    Maturity,
    OAuthAuth,
    SettingField,
    SyncBatch,
)
from crm.connect.http import ProviderClient
from crm.connect.records import INBOUND, OUTBOUND, CalendarEventRecord, EmailRecord
from crm.connect.registry import register

GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/primary"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPES = {
    Capability.MAILBOX: (
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ),
    Capability.CALENDAR: ("https://www.googleapis.com/auth/calendar.events",),
}
BACKFILL_DAYS = 30
# When Gmail no longer has the history a sync stopped at, look back this far again.
RESYNC_DAYS = 7
MAIL_LIMIT = 2000
EVENT_LIMIT = 5000
SKIPPED_LABELS = {
    "SPAM",
    "TRASH",
    "DRAFT",
    "CHAT",
    "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
}
HEADERS = ("From", "To", "Subject", "List-Unsubscribe", "Precedence", "Auto-Submitted")
CRM_MARK = "open-immigration-crm"


def from_ms(value):
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def first_address(value):
    return next((address.lower() for _, address in getaddresses([value or ""]) if address), "")


def is_bulk(headers):
    """Mailing lists and automated mail, which are not client correspondence."""
    return (
        "list-unsubscribe" in headers
        or headers.get("precedence", "").lower() in ("bulk", "list", "junk")
        or headers.get("auto-submitted", "no").lower() != "no"
    )


def event_time(value):
    """A timed event's start or end; None for an all-day event, which has only a date."""
    try:
        return datetime.fromisoformat((value or {}).get("dateTime", ""))
    except ValueError:
        return None


@register
class GoogleWorkspaceConnector(Connector):
    key = "google_workspace"
    name = "Google Workspace"
    description = (
        "File each staff member's Gmail with their clients on the right contact, send "
        "email from contact pages, and keep appointments and Google Calendar in step."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.MAILBOX, Capability.CALENDAR})
    auth = OAuthAuth(
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
        docs_url="https://developers.google.com/identity/protocols/oauth2/web-server",
        # A refresh token is issued only for offline access, and again only on consent.
        extra_authorize_params={"access_type": "offline", "prompt": "consent"},
    )
    connection_scope = ConnectionScope.USER
    settings_fields = (
        SettingField(
            "unmatched",
            "Email from people who are not contacts",
            choices=(
                ("keep", "Keep it in Needs matching, to link or add as a contact"),
                ("skip", "Leave it out of the CRM"),
            ),
            default="keep",
            help_text="Email between staff members is always left out.",
        ),
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "Email staff send from contact and matter pages: the contact's address, the "
            "subject, and the message",
            "Appointments assigned to the staff member: title, time, description, and the "
            "contact's name and matter number",
        ),
        receives=(
            "The staff member's email from the last 30 days on: the other party's address, "
            "the subject, Gmail's short snippet, and the time. Never full messages or "
            "attachments.",
            "Events on the staff member's primary Google Calendar: title, time, location, "
            "and attendees' addresses",
        ),
    )
    outbound_actions = {
        "send_email": Capability.MAILBOX,
        "save_event": Capability.CALENDAR,
        "delete_event": Capability.CALENDAR,
    }
    owner_sync_actions = frozenset({"save_event", "delete_event"})
    sync_capabilities = frozenset({Capability.MAILBOX, Capability.CALENDAR})
    sync_interval = timedelta(minutes=10)

    def __init__(self, connection):
        super().__init__(connection)
        # Gmail allows 6,000 quota units a minute per user, and reading a message costs
        # 20, so requests are spaced to stay under it.
        self.http = ProviderClient(
            name="Google", headers={"Accept": "application/json"}, min_interval=0.25
        )

    def client(self):
        """The shared client, with a current access token."""
        self.http.headers["Authorization"] = f"Bearer {self.access_token()}"
        return self.http

    def call(self, method, url, *, missing_ok=False, **kwargs):
        """A Google API answer; a 404 or 410 is None when ``missing_ok``."""
        try:
            return self.client().json(method, url, **kwargs)
        except ConnectorError as exc:
            if missing_ok and exc.status in (404, 410):
                return None
            if exc.status == 401:
                raise ConnectorError(
                    "Google did not accept the sign-in. Sign in again.", status=401
                ) from None
            if exc.status == 403:
                raise ConnectorError(
                    "Google refused the request. Sign in again and allow every permission "
                    "asked for, or check the firm's Google Cloud app.",
                    status=403,
                ) from None
            raise

    def test_connection(self):
        enabled = self.connection.enabled_capabilities
        if Capability.MAILBOX in enabled:
            self.call("GET", f"{GMAIL_URL}/profile")
        if Capability.CALENDAR in enabled:
            self.call("GET", f"{CALENDAR_URL}/events", params={"maxResults": 1})

    def revoke(self):
        token = self.credentials.get("refresh_token") or self.credentials.get("access_token")
        if token:
            ProviderClient(name="Google", retries=1).request(
                "POST", REVOKE_URL, data={"token": token}, expected=(200, 400)
            )

    def sync(self, capability, cursor):
        if capability == Capability.MAILBOX:
            return self.sync_mail(cursor or {})
        return self.sync_calendar(cursor or {})

    # Mail

    def sync_mail(self, cursor):
        profile = self.call("GET", f"{GMAIL_URL}/profile")
        account = str(profile.get("emailAddress", "")).lower()
        ids, latest = None, str(profile.get("historyId", ""))
        if cursor.get("history_id"):
            ids, history_latest = self.mail_history(cursor["history_id"])
            if ids is not None:
                latest = history_latest or latest
        if ids is None:
            days = RESYNC_DAYS if cursor.get("history_id") else BACKFILL_DAYS
            ids = self.recent_mail(days)
        records = []
        for message_id in list(dict.fromkeys(ids))[:MAIL_LIMIT]:
            record = self.email_record(message_id, account)
            if record:
                records.append(record)
        return SyncBatch(records=records, cursor={"history_id": latest})

    def mail_history(self, start):
        """Messages added since ``start``, and the newest history ID; (None, "") if too old."""
        ids, page_token, latest = [], None, ""
        while True:
            params = {"startHistoryId": start, "historyTypes": "messageAdded", "maxResults": 500}
            if page_token:
                params["pageToken"] = page_token
            page = self.call("GET", f"{GMAIL_URL}/history", params=params, missing_ok=True)
            if page is None:
                return None, ""
            for entry in page.get("history") or []:
                ids += [m["message"]["id"] for m in entry.get("messagesAdded") or []]
            latest = str(page.get("historyId") or latest)
            page_token = page.get("nextPageToken")
            if not page_token or len(ids) >= MAIL_LIMIT:
                return ids, latest

    def recent_mail(self, days):
        """IDs of inbox and sent messages from the last ``days`` days."""
        url = f"{GMAIL_URL}/messages"
        query = {"q": f"newer_than:{days}d (in:inbox OR in:sent)", "maxResults": 500}

        def next_request(page):
            token = page.get("nextPageToken")
            return (url, {**query, "pageToken": token}) if token else None

        messages = self.client().paginate(
            url,
            items=lambda page: page.get("messages") or [],
            next_request=next_request,
            params=query,
            limit=MAIL_LIMIT,
        )
        return [message["id"] for message in messages]

    def email_record(self, message_id, account):
        message = self.call(
            "GET",
            f"{GMAIL_URL}/messages/{message_id}",
            params={"format": "metadata", "metadataHeaders": list(HEADERS)},
            missing_ok=True,
        )
        if not message:
            return None
        labels = set(message.get("labelIds") or [])
        if labels & SKIPPED_LABELS or not labels & {"INBOX", "SENT"}:
            return None
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in (message.get("payload") or {}).get("headers") or []
        }
        if is_bulk(headers):
            return None
        outbound = "SENT" in labels
        remote = first_address(headers.get("to" if outbound else "from"))
        occurred_at = from_ms(message.get("internalDate"))
        if not remote or remote == account or occurred_at is None:
            return None
        return EmailRecord(
            external_id=message["id"],
            occurred_at=occurred_at,
            direction=OUTBOUND if outbound else INBOUND,
            remote=remote,
            subject=headers.get("subject", "")[:255],
            snippet=html.unescape(message.get("snippet", ""))[:500],
            thread_id=str(message.get("threadId", "")),
            staff_email=account,
        )

    def send_email(self, payload):
        account = self.call("GET", f"{GMAIL_URL}/profile").get("emailAddress", "")
        message = EmailMessage()
        try:
            message["From"] = account
            message["To"] = payload["to"]
            message["Subject"] = payload["subject"]
        except ValueError:
            raise ConnectorError("The email's address or subject was not valid.") from None
        message.set_content(payload["body"])
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        answer = self.call("POST", f"{GMAIL_URL}/messages/send", json={"raw": raw})
        if not answer.get("id"):
            raise ConnectorError("Gmail did not return a message ID.")
        return answer["id"]

    # Calendar

    def sync_calendar(self, cursor):
        token = cursor.get("sync_token")
        params = {"singleEvents": "true", "maxResults": 250}
        if token:
            params["syncToken"] = token
        else:
            params["timeMin"] = (timezone.now() - timedelta(days=BACKFILL_DAYS)).isoformat()
        events, next_token = [], None
        while True:
            page = self.call("GET", f"{CALENDAR_URL}/events", params=params, missing_ok=bool(token))
            if page is None:
                # Google no longer accepts the sync token; start again.
                return self.sync_calendar({})
            events += page.get("items") or []
            next_token = page.get("nextSyncToken") or next_token
            if not page.get("nextPageToken") or len(events) >= EVENT_LIMIT:
                break
            params = {**params, "pageToken": page["nextPageToken"]}
        records = [record for record in map(self.event_record, events) if record]
        return SyncBatch(records=records, cursor={"sync_token": next_token or token or ""})

    def event_record(self, event):
        if not event.get("id"):
            return None
        if event.get("status") == "cancelled":
            return CalendarEventRecord(
                external_id=event["id"],
                occurred_at=event_time(event.get("start")) or timezone.now(),
                title=event.get("summary") or "Cancelled event",
                cancelled=True,
            )
        starts_at = event_time(event.get("start"))
        if starts_at is None:
            return None
        attendees = [
            a["email"]
            for a in event.get("attendees") or []
            if a.get("email") and not a.get("self") and not a.get("resource")
        ]
        return CalendarEventRecord(
            external_id=event["id"],
            occurred_at=starts_at,
            title=(event.get("summary") or "(No title)")[:255],
            ends_at=event_time(event.get("end")),
            location=(event.get("location") or "")[:255],
            attendee_emails=tuple(attendees),
            etag=event.get("etag", ""),
        )

    def save_event(self, payload):
        body = {
            "summary": payload["title"],
            "description": payload["description"],
            "start": {"dateTime": payload["starts_at"], "timeZone": payload["time_zone"]},
            "end": {"dateTime": payload["ends_at"], "timeZone": payload["time_zone"]},
            "extendedProperties": {"private": {"crm": CRM_MARK, "crmId": payload["crm_id"]}},
        }
        quiet = {"sendUpdates": "none"}
        if payload.get("event_id"):
            event = self.call(
                "PATCH",
                f"{CALENDAR_URL}/events/{payload['event_id']}",
                params=quiet,
                json=body,
                missing_ok=True,
            )
            if event:
                return event["id"]
        return self.call("POST", f"{CALENDAR_URL}/events", params=quiet, json=body)["id"]

    def perform(self, action, payload):
        if action == "send_email":
            return self.send_email(payload)
        if action == "save_event":
            return self.save_event(payload)
        if action == "delete_event":
            self.call(
                "DELETE",
                f"{CALENDAR_URL}/events/{payload['event_id']}",
                params={"sendUpdates": "none"},
                missing_ok=True,
            )
            return payload["event_id"]
        raise ConnectorError(f"Google Workspace cannot {action}.")
