"""Microsoft 365: each staff member's Outlook mail and calendar, through Microsoft Graph.

Each staff member signs in with the firm's own Entra ID app registration (OAuth
with PKCE), and one stored token serves both mail and calendar.

Mail: the inbox and Sent Items are followed with Graph's delta queries, starting
30 days back. Only the subject, preview, people, and times are read. Drafts,
Outlook's "Other" (unfocused) mail, and email between staff are left out. Email is
sent as plain text through the outbound policy, as a draft that is then sent, so
its ID (an immutable ID) is the same one the Sent Items copy has.

Calendar: events are followed with a calendar-view delta query, and appointments
assigned to the staff member are put on the calendar with no attendees.

Change notifications: Graph subscriptions tell the CRM about new mail and calendar
changes. Graph proves each notification with the subscription's clientState, not a
signature, so the CRM checks the clientState and the subscription ID and then
fetches what changed itself; nothing in the notification is trusted. The worker
renews subscriptions before they expire, and the delta sync every few minutes is
the fallback.

API reference: https://learn.microsoft.com/graph/api/overview
"""

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from django.core.exceptions import ValidationError
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
    WebhookEnvelope,
    WebhookSpec,
)
from crm.connect.http import ProviderClient
from crm.connect.records import INBOUND, OUTBOUND, CalendarEventRecord, EmailRecord
from crm.connect.registry import register

GRAPH_URL = "https://graph.microsoft.com/v1.0"
LOGIN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
GRAPH_SCOPE = "https://graph.microsoft.com/"
TENANT = re.compile(r"^(organizations|[0-9a-fA-F-]{36}|[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+)$")
# Immutable IDs stay the same when a message moves, such as from Drafts to Sent Items.
PREFER = 'IdType="ImmutableId", outlook.timezone="UTC"'
MAIL_FOLDERS = ("inbox", "sentitems")
MESSAGE_FIELDS = (
    "id,subject,bodyPreview,from,toRecipients,receivedDateTime,sentDateTime,"
    "conversationId,isDraft,inferenceClassification"
)
BACKFILL_DAYS = 30
CALENDAR_AHEAD_DAYS = 365
# A calendar delta covers a fixed window, so it is started again this often.
CALENDAR_WINDOW_RESTART = timedelta(days=30)
DELTA_LIMIT = 5000
SUBSCRIPTION_LENGTH = timedelta(days=2)
RENEW_WITHIN = timedelta(days=1)
SUBSCRIPTIONS = {
    "inbox": ("mail", "me/mailFolders('inbox')/messages", "created"),
    "sentitems": ("mail", "me/mailFolders('sentitems')/messages", "created"),
    "events": ("event", "me/events", "created,updated,deleted"),
}
MAX_NOTIFICATIONS = 100


def validate_tenant(value):
    if not TENANT.match(value):
        raise ValidationError(
            "Enter the directory (tenant) ID from the app registration's overview, the "
            "firm's Microsoft 365 domain, or organizations."
        )


def parse_time(value):
    """A Graph date and time, which is in UTC here, as an aware datetime."""
    text = str(value or "").rstrip("Z")
    try:
        moment = datetime.fromisoformat(text[:26])
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def address(recipient):
    return str(((recipient or {}).get("emailAddress") or {}).get("address") or "").lower()


@register
class Microsoft365Connector(Connector):
    key = "microsoft_365"
    name = "Microsoft 365"
    description = (
        "File each staff member's Outlook email with their clients on the right contact, "
        "send email from contact pages, and keep appointments and Outlook calendar in step."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.MAILBOX, Capability.CALENDAR})
    auth = OAuthAuth(
        authorize_url=f"{LOGIN_URL}/authorize",
        token_url=f"{LOGIN_URL}/token",
        scopes={
            Capability.MAILBOX: (
                "offline_access",
                f"{GRAPH_SCOPE}User.Read",
                f"{GRAPH_SCOPE}Mail.Read",
                f"{GRAPH_SCOPE}Mail.Send",
            ),
            Capability.CALENDAR: (
                "offline_access",
                f"{GRAPH_SCOPE}User.Read",
                f"{GRAPH_SCOPE}Calendars.ReadWrite",
            ),
        },
        docs_url="https://learn.microsoft.com/entra/identity-platform/quickstart-register-app",
        app_fields=(
            SettingField(
                "tenant",
                "Directory (tenant) ID",
                help_text="On the app registration's overview page, or the firm's Microsoft "
                "365 domain. Enter organizations only for an app that accepts accounts from "
                "any organization.",
                validate=validate_tenant,
            ),
        ),
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
            "When staff ask: this CRM's private webhook address, for change notifications",
        ),
        receives=(
            "The staff member's email from the last 30 days on: the other party's address, "
            "the subject, Outlook's short preview, and the time. Never full messages or "
            "attachments.",
            "Events on the staff member's Outlook calendar: title, time, location, and "
            "attendees' addresses",
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
    webhooks = WebhookSpec(
        capabilities=(Capability.MAILBOX, Capability.CALENDAR),
        register_label="Ask Microsoft Graph to notify this CRM of new mail and calendar "
        "changes. The CRM renews the subscriptions before they expire.",
        instructions="Graph subscriptions are created through its API; the CRM does this "
        "when the box above is ticked. Without them, mail and calendar still sync every few "
        "minutes.",
        ack_status=202,
        ack_body="",
    )

    def __init__(self, connection):
        super().__init__(connection)
        self.http = ProviderClient(
            GRAPH_URL, name="Microsoft 365", headers={"Accept": "application/json"}
        )
        self.account = ""
        self.folder_ids = None

    # Calls

    def client(self):
        self.http.headers["Authorization"] = f"Bearer {self.access_token()}"
        self.http.headers["Prefer"] = PREFER
        return self.http

    def call(self, method, path, *, missing_ok=False, **kwargs):
        """A Graph answer; a 404 or 410 is None when ``missing_ok``."""
        try:
            return self.client().json(method, path, **kwargs)
        except ConnectorError as exc:
            if missing_ok and exc.status in (404, 410):
                return None
            if exc.status == 401:
                raise ConnectorError(
                    "Microsoft did not accept the sign-in. Sign in again.", status=401
                ) from None
            if exc.status == 403:
                raise ConnectorError(
                    "Microsoft refused the request. An administrator may need to consent "
                    "to the app's permissions.",
                    status=403,
                ) from None
            raise

    def me(self):
        if not self.account:
            profile = self.call("GET", "/me", params={"$select": "mail,userPrincipalName"})
            self.account = str(profile.get("mail") or profile.get("userPrincipalName") or "")
            self.account = self.account.lower()
        return self.account

    def delta(self, url, params=None):
        """Every item a delta query returns, following pages, and its next delta link.

        Returns (None, "") when Graph refuses a saved delta link, which it does with a
        400-series error such as syncStateNotFound once the link has expired, so the
        caller starts again.
        """
        items = []
        while True:
            try:
                page = self.call("GET", url, params=params, missing_ok=True)
            except ConnectorError as exc:
                if params is None and exc.status in (400, 404, 410):
                    return None, ""
                raise
            if page is None:
                return None, ""
            items += page.get("value") or []
            if page.get("@odata.deltaLink") or len(items) >= DELTA_LIMIT:
                return items, page.get("@odata.deltaLink", "")
            url, params = page.get("@odata.nextLink"), None
            if not url:
                return items, ""

    def test_connection(self):
        self.me()

    # Mail

    def sync_mail(self, cursor):
        self.renew_subscriptions("mail")
        links = dict(cursor.get("links") or {})
        since = (timezone.now() - timedelta(days=BACKFILL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        records = []
        for folder in MAIL_FOLDERS:
            messages = None
            if links.get(folder):
                messages, link = self.delta(links[folder])
            if messages is None:
                # No delta yet, or Graph no longer has it: start again from 30 days back.
                messages, link = self.delta(
                    f"/me/mailFolders/{folder}/messages/delta",
                    {"$select": MESSAGE_FIELDS, "$filter": f"receivedDateTime ge {since}"},
                )
            links[folder] = link or links.get(folder, "")
            records += [r for r in (self.email_record(m, folder) for m in messages or []) if r]
        return SyncBatch(records=records, cursor={"links": links})

    def email_record(self, message, folder):
        if "@removed" in message or message.get("isDraft") or not message.get("id"):
            return None
        if folder not in MAIL_FOLDERS:
            return None
        if folder == "inbox" and message.get("inferenceClassification") == "other":
            return None
        outbound = folder == "sentitems"
        recipients = message.get("toRecipients") or []
        remote = (
            address(recipients[0] if recipients else {})
            if outbound
            else address(message.get("from"))
        )
        when = parse_time(message.get("sentDateTime" if outbound else "receivedDateTime"))
        if not remote or when is None or remote == self.me():
            return None
        return EmailRecord(
            external_id=message["id"],
            occurred_at=when,
            direction=OUTBOUND if outbound else INBOUND,
            remote=remote,
            subject=str(message.get("subject") or "")[:255],
            snippet=str(message.get("bodyPreview") or "")[:500],
            thread_id=str(message.get("conversationId") or ""),
            staff_email=self.me(),
        )

    def send_email(self, payload):
        draft = self.call(
            "POST",
            "/me/messages",
            json={
                "subject": payload["subject"],
                "body": {"contentType": "Text", "content": payload["body"]},
                "toRecipients": [{"emailAddress": {"address": payload["to"]}}],
            },
        )
        if not draft.get("id"):
            raise ConnectorError("Outlook did not return a message ID.")
        self.call("POST", f"/me/messages/{draft['id']}/send")
        return draft["id"]

    # Calendar

    def sync_calendar(self, cursor):
        self.renew_subscriptions("event")
        now = timezone.now()
        started = parse_time(cursor.get("window_start"))
        link = cursor.get("link") if started and now - started < CALENDAR_WINDOW_RESTART else ""
        events = None
        if link:
            events, next_link = self.delta(link)
        if events is None:
            started = now
            events, next_link = self.delta(
                "/me/calendarView/delta",
                {
                    "startDateTime": (now - timedelta(days=BACKFILL_DAYS)).isoformat(),
                    "endDateTime": (now + timedelta(days=CALENDAR_AHEAD_DAYS)).isoformat(),
                },
            )
        records = [r for r in (self.event_record(e) for e in events or []) if r]
        return SyncBatch(
            records=records,
            cursor={"link": next_link or link, "window_start": started.isoformat()},
        )

    def event_record(self, event):
        if not event.get("id"):
            return None
        if "@removed" in event or event.get("isCancelled"):
            return CalendarEventRecord(
                external_id=event["id"],
                occurred_at=parse_time((event.get("start") or {}).get("dateTime"))
                or timezone.now(),
                title=event.get("subject") or "Cancelled event",
                cancelled=True,
            )
        starts_at = parse_time((event.get("start") or {}).get("dateTime"))
        if event.get("isAllDay") or starts_at is None:
            return None
        attendees = [address(a) for a in event.get("attendees") or []]
        return CalendarEventRecord(
            external_id=event["id"],
            occurred_at=starts_at,
            title=(event.get("subject") or "(No title)")[:255],
            ends_at=parse_time((event.get("end") or {}).get("dateTime")),
            location=str((event.get("location") or {}).get("displayName") or "")[:255],
            attendee_emails=tuple(a for a in attendees if a and a != self.me()),
            etag=str(event.get("changeKey") or ""),
            staff_email=self.me(),
        )

    def save_event(self, payload):
        def utc(value):
            return datetime.fromisoformat(value).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")

        body = {
            "subject": payload["title"],
            "body": {"contentType": "text", "content": payload["description"]},
            "start": {"dateTime": utc(payload["starts_at"]), "timeZone": "UTC"},
            "end": {"dateTime": utc(payload["ends_at"]), "timeZone": "UTC"},
        }
        if payload.get("event_id"):
            event = self.call(
                "PATCH", f"/me/events/{payload['event_id']}", json=body, missing_ok=True
            )
            if event:
                return event["id"]
        return self.call("POST", "/me/events", json=body)["id"]

    def perform(self, action, payload):
        if action == "send_email":
            return self.send_email(payload)
        if action == "save_event":
            return self.save_event(payload)
        if action == "delete_event":
            self.call("DELETE", f"/me/events/{payload['event_id']}", missing_ok=True)
            return payload["event_id"]
        raise ConnectorError(f"Microsoft 365 cannot {action}.")

    def sync(self, capability, cursor):
        if capability == Capability.MAILBOX:
            return self.sync_mail(cursor or {})
        return self.sync_calendar(cursor or {})

    # Change notifications

    def subscriptions(self):
        return dict((self.connection.config or {}).get("subscriptions") or {})

    def save_subscriptions(self, subscriptions):
        self.connection.config = {**(self.connection.config or {}), "subscriptions": subscriptions}
        self.connection.save(update_fields=["config", "updated_at"])

    def subscribe(self, name, url, secret):
        kind, resource, change_type = SUBSCRIPTIONS[name]
        expires = timezone.now() + SUBSCRIPTION_LENGTH
        made = self.call(
            "POST",
            "/subscriptions",
            json={
                "changeType": change_type,
                "notificationUrl": url,
                "resource": resource,
                "expirationDateTime": expires.isoformat(),
                "clientState": secret,
            },
        )
        return made["id"], {"name": name, "kind": kind, "expires": expires.isoformat()}

    def wanted_subscriptions(self):
        enabled = self.connection.enabled_capabilities
        names = list(MAIL_FOLDERS) if Capability.MAILBOX in enabled else []
        return names + (["events"] if Capability.CALENDAR in enabled else [])

    def register_webhook(self, url, secret):
        self.remove_subscriptions()
        subscriptions = dict(
            self.subscribe(name, url, secret) for name in self.wanted_subscriptions()
        )
        self.save_subscriptions(subscriptions)
        # Kept encrypted with the other credentials, to subscribe again if one lapses.
        self.connection.credentials = {**self.credentials, "webhook_url": url}
        self.connection.save(update_fields=["credentials", "updated_at"])
        kinds = {detail["kind"] for detail in subscriptions.values()}
        nouns = [
            noun
            for kind, noun in (("mail", "new mail"), ("event", "calendar changes"))
            if kind in kinds
        ]
        return f"Graph will notify this CRM of {' and '.join(nouns)}."

    def renew_subscriptions(self, kind):
        """Extend this kind's subscriptions before they lapse, or subscribe again if gone."""
        subscriptions = self.subscriptions()
        url, secret = self.credentials.get("webhook_url"), self.connection.webhook_secret
        if not subscriptions or not url or not secret:
            return
        changed = False
        for sub_id, detail in list(subscriptions.items()):
            expires = parse_time(detail.get("expires"))
            if detail.get("kind") != kind or (expires and expires - timezone.now() > RENEW_WITHIN):
                continue
            new_expiry = timezone.now() + SUBSCRIPTION_LENGTH
            renewed = self.call(
                "PATCH",
                f"/subscriptions/{sub_id}",
                json={"expirationDateTime": new_expiry.isoformat()},
                missing_ok=True,
            )
            del subscriptions[sub_id]
            if renewed:
                subscriptions[sub_id] = {**detail, "expires": new_expiry.isoformat()}
            else:
                new_id, new_detail = self.subscribe(detail["name"], url, secret)
                subscriptions[new_id] = new_detail
            changed = True
        if changed:
            self.save_subscriptions(subscriptions)

    def remove_subscriptions(self):
        for sub_id in self.subscriptions():
            self.call("DELETE", f"/subscriptions/{sub_id}", missing_ok=True)
        self.save_subscriptions({})

    def revoke(self):
        # Graph has no way to revoke one sign-in; the stored tokens are deleted on disconnect.
        self.remove_subscriptions()

    def webhook_challenge(self, url, body, headers):
        token = parse_qs(urlsplit(url).query).get("validationToken")
        if token and not body:
            return 200, token[0][:1024]
        return None

    def notifications(self, body):
        try:
            data = json.loads(body)
        except ValueError:
            return None
        items = data.get("value") if isinstance(data, dict) else None
        if not isinstance(items, list) or not 0 < len(items) <= MAX_NOTIFICATIONS:
            return None
        return items if all(isinstance(item, dict) for item in items) else None

    def verify_webhook(self, body, headers, secret, *, url=""):
        items = self.notifications(body)
        known = self.subscriptions()
        return bool(items) and all(
            hmac.compare_digest(str(item.get("clientState", "")), secret)
            and item.get("subscriptionId") in known
            for item in items
        )

    def parse_webhook(self, body, headers):
        known = self.subscriptions()
        changes = []
        for item in self.notifications(body) or []:
            resource_id = str((item.get("resourceData") or {}).get("id") or "")
            if resource_id:
                changes.append(
                    {
                        "kind": known[item["subscriptionId"]]["kind"],
                        "id": resource_id[:512],
                        "change": str(item.get("changeType") or "")[:20],
                    }
                )
        if not changes:
            raise ConnectorError("The Graph notification named nothing that changed.")
        kinds = {change["kind"] for change in changes}
        return WebhookEnvelope(
            event_id=f"graph:{hashlib.sha256(body).hexdigest()[:40]}",
            capability=Capability.MAILBOX if "mail" in kinds else Capability.CALENDAR,
            payload={"changes": changes},
        )

    def handle_webhook(self, capability, payload):
        """Fetch what each notification points to; the notification itself is not trusted."""
        records = []
        for change in payload.get("changes", []):
            if change["kind"] == "event" and change["change"] == "deleted":
                record = self.event_record({"id": change["id"], "@removed": {}})
            elif change["kind"] == "event":
                event = self.call("GET", f"/me/events/{change['id']}", missing_ok=True)
                record = self.event_record(event) if event else None
            else:
                message = self.call(
                    "GET",
                    f"/me/messages/{change['id']}",
                    params={"$select": MESSAGE_FIELDS + ",parentFolderId"},
                    missing_ok=True,
                )
                record = self.email_record(message, self.folder_of(message)) if message else None
            if record:
                records.append(record)
        return records

    def folder_of(self, message):
        """Which followed folder a message is in: the inbox, Sent Items, or neither."""
        if self.folder_ids is None:
            self.folder_ids = {
                self.call("GET", f"/me/mailFolders/{name}", params={"$select": "id"})["id"]: name
                for name in MAIL_FOLDERS
            }
        return self.folder_ids.get(message.get("parentFolderId"), "")
