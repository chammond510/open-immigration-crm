import json
import time
from datetime import UTC, datetime, timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

import httpx
from django.test import TestCase
from django.urls import reverse

from crm.appointments import push_appointment
from crm.comms.emailing import send_email
from crm.comms.models import Communication
from crm.connect import http
from crm.connect.models import Connection, ConnectorApp, SyncRun, WebhookEvent
from crm.connect.services import acknowledge_data_flows, disconnect
from crm.connect.tasks import process_webhook_event, run_sync
from crm.connect.testing import ConnectorContract
from crm.models import WorkItem
from crm.tests.factories import make_contact, make_user

from . import PREFER, Microsoft365Connector

TOKEN = "fictional-graph-access-token"
TENANT = "00000000-0000-4000-8000-000000000001"
ACCOUNT = "associate@firm.example.test"
CLIENT = "lena@example.test"
NOW = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
SECRET = "fictional-client-state"


def graph_time(moment):
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000")


def message(message_id, *, sender=CLIENT, to=ACCOUNT, **extra):
    return {
        "id": message_id,
        "subject": f"Fictional subject {message_id}",
        "bodyPreview": "Thank you for the update on the forms.",
        "from": {"emailAddress": {"address": sender}},
        "toRecipients": [{"emailAddress": {"address": to}}],
        "receivedDateTime": "2026-10-30T15:00:00Z",
        "sentDateTime": "2026-10-30T15:00:00Z",
        "conversationId": f"conv-{message_id}",
        "isDraft": False,
        "inferenceClassification": "focused",
        "parentFolderId": "folder-inbox",
        **extra,
    }


def event(event_id, *, attendees=(CLIENT,), start=NOW, **extra):
    return {
        "id": event_id,
        "subject": "Consultation",
        "start": {"dateTime": graph_time(start), "timeZone": "UTC"},
        "end": {"dateTime": graph_time(start + timedelta(hours=1)), "timeZone": "UTC"},
        "location": {"displayName": "Office"},
        "attendees": [{"emailAddress": {"address": a}, "type": "required"} for a in attendees],
        "isCancelled": False,
        "isAllDay": False,
        "changeKey": f"ck-{event_id}",
        **extra,
    }


class FakeGraph:
    """Answers like Microsoft Graph for one fictional staff member."""

    def __init__(self):
        self.requests = []
        self.folders = {"inbox": [], "sentitems": []}
        self.messages = {}
        self.events = {}
        self.expired = set()
        self.subscriptions = {}
        self.lapsed = set()
        self.made = 0

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(
                200,
                json={"access_token": TOKEN, "refresh_token": "r2", "expires_in": 3600},
            )
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        assert request.headers.get("Prefer") == PREFER
        path = request.url.path.removeprefix("/v1.0")
        body = json.loads(request.content) if request.content else {}
        method = request.method
        if path == "/me":
            return httpx.Response(200, json={"mail": ACCOUNT})
        if path.startswith("/me/mailFolders/") and path.endswith("/messages/delta"):
            return self.delta(request, self.folders[path.split("/")[3]], "mail")
        if path.startswith("/me/mailFolders/"):
            return httpx.Response(200, json={"id": f"folder-{path.split('/')[3]}"})
        if path == "/me/calendarView/delta":
            return self.delta(request, list(self.events.values()), "events")
        if path == "/me/messages" and method == "POST":
            self.made += 1
            draft = {**message(f"immutable-{self.made}"), **body}
            self.messages[draft["id"]] = draft
            return httpx.Response(201, json=draft)
        if path.endswith("/send"):
            return httpx.Response(202)
        if path.startswith("/me/messages/"):
            found = self.messages.get(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=found) if found else httpx.Response(404)
        if path.startswith("/me/events"):
            return self.calendar(method, path.removeprefix("/me/events").strip("/"), body)
        if path.startswith("/subscriptions"):
            return self.subscription(method, path.removeprefix("/subscriptions").strip("/"), body)
        return httpx.Response(404)

    def delta(self, request, items, name):
        here = f"https://graph.microsoft.com{request.url.path}"
        done = f"{here}?$deltatoken=d-{name}"
        if request.url.params.get("$deltatoken") in self.expired:
            return httpx.Response(400, json={"error": {"code": "syncStateNotFound"}})
        if request.url.params.get("$skiptoken") == "page-2":
            return httpx.Response(200, json={"value": items[2:], "@odata.deltaLink": done})
        page = {"value": items[:2]}
        if len(items) > 2:
            page["@odata.nextLink"] = f"{here}?$skiptoken=page-2"
        else:
            page["@odata.deltaLink"] = done
        return httpx.Response(200, json=page)

    def calendar(self, method, event_id, body):
        if method == "GET":
            found = self.events.get(event_id)
            return httpx.Response(200, json=found) if found else httpx.Response(404)
        if method == "DELETE":
            return httpx.Response(204 if self.events.pop(event_id, None) else 404)
        if method == "PATCH":
            if event_id not in self.events:
                return httpx.Response(404)
            self.events[event_id].update(body)
            return httpx.Response(200, json=self.events[event_id])
        self.made += 1
        made = {**body, "id": f"crm-event-{self.made}", "changeKey": f"ck{self.made}"}
        self.events[made["id"]] = made
        return httpx.Response(201, json=made)

    def subscription(self, method, sub_id, body):
        if method == "POST":
            self.made += 1
            made = {**body, "id": f"sub-{self.made}"}
            self.subscriptions[made["id"]] = made
            return httpx.Response(201, json=made)
        if method == "DELETE":
            return httpx.Response(204 if self.subscriptions.pop(sub_id, None) else 404)
        if sub_id in self.lapsed or sub_id not in self.subscriptions:
            self.subscriptions.pop(sub_id, None)
            return httpx.Response(404)
        self.subscriptions[sub_id].update(body)
        return httpx.Response(200, json=self.subscriptions[sub_id])

    def calls(self, method, path=""):
        return [r for r in self.requests if r.method == method and path in r.url.path]


class FakeGraphMixin:
    def setUp(self):
        super().setUp()
        self.graph = FakeGraph()
        transport = http.use_transport(httpx.MockTransport(lambda r: self.graph(r)))
        transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)
        patcher = mock.patch.object(http, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)


def signed_in():
    return {"access_token": TOKEN, "refresh_token": "r", "expires_at": time.time() + 3600}


def notification(sub_id, resource_id, change="created", state=SECRET):
    return json.dumps(
        {
            "value": [
                {
                    "subscriptionId": sub_id,
                    "clientState": state,
                    "changeType": change,
                    "resource": f"Users/x/Messages/{resource_id}",
                    "resourceData": {"id": resource_id},
                    "tenantId": TENANT,
                }
            ]
        }
    ).encode()


class GraphContractTests(FakeGraphMixin, ConnectorContract, TestCase):
    connector_class = Microsoft365Connector
    valid_credentials = signed_in()
    invalid_credentials = {**signed_in(), "access_token": "revoked"}
    connection_config = {
        "subscriptions": {"sub-1": {"name": "inbox", "kind": "mail", "expires": NOW.isoformat()}}
    }
    sync_examples = {"mailbox": {}, "calendar": {}}
    outbound_examples = {
        "send_email": {"to": CLIENT, "subject": "Forms", "body": "Attached are the forms."},
        "save_event": {
            "event_id": "",
            "title": "Consultation",
            "starts_at": NOW.isoformat(),
            "ends_at": (NOW + timedelta(hours=1)).isoformat(),
            "time_zone": "UTC",
            "description": "From Open Immigration CRM.",
            "crm_id": "1",
        },
        "delete_event": {"event_id": "crm-event-1"},
    }
    webhook_secret = SECRET
    signature_in_headers = False
    body_is_signed = False

    def setUp(self):
        super().setUp()
        self.graph.folders["inbox"] = [message("m1")]
        self.graph.messages = {"m1": message("m1")}
        self.graph.events = {"e1": event("e1")}

    def make_webhook(self, secret):
        return notification("sub-1", "m1", state=secret), {"Content-Type": "application/json"}


class GraphTestCase(FakeGraphMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user()
        self.owner = make_user("associate", superuser=False)
        self.contact = make_contact(self.owner, email=CLIENT)
        self.connection = Connection.objects.create(
            connector_key="microsoft_365",
            label="Associate's Microsoft 365",
            owner=self.owner,
            enabled_capabilities=["calendar", "mailbox"],
            credentials=signed_in(),
            config={"unmatched": "keep"},
            preview_mode=False,
        )
        acknowledge_data_flows(self.connection, self.admin)

    def sync(self, capability):
        run = SyncRun.objects.create(connection=self.connection, capability=capability)
        run_sync.call(run.pk)
        run.refresh_from_db()
        self.connection.refresh_from_db()
        return run


class SignInTests(FakeGraphMixin, TestCase):
    def test_the_firms_tenant_is_used_for_sign_in(self):
        admin = make_user()
        self.client.force_login(admin)
        app_url = reverse("connector_app", args=["microsoft_365"])
        form = {"client_id": "fictional-app", "client_secret": "s", "setting_tenant": "x/../evil"}
        self.client.post(app_url, form)
        self.assertFalse(ConnectorApp.objects.exists())
        self.client.post(app_url, {**form, "setting_tenant": TENANT})
        self.assertEqual(ConnectorApp.objects.get().config, {"tenant": TENANT})
        detail = self.client.get(reverse("connector_detail", args=["microsoft_365"]))
        self.assertContains(detail, f'value="{TENANT}"')
        self.client.post(
            reverse("connector_detail", args=["microsoft_365"]),
            {
                "label": "Mine",
                "capabilities": ["mailbox", "calendar"],
                "setting_unmatched": "keep",
                "acknowledge": "on",
            },
        )
        connection = Connection.objects.get()
        start = self.client.get(reverse("connection_oauth_start", args=[connection.pk]))
        location = urlparse(start["Location"])
        self.assertEqual(
            f"{location.netloc}{location.path}",
            f"login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize",
        )
        query = {k: v[0] for k, v in parse_qs(location.query).items()}
        self.assertIn("offline_access", query["scope"].split())
        self.assertIn("https://graph.microsoft.com/Mail.Send", query["scope"].split())
        self.client.get(
            reverse("connection_oauth_callback"), {"state": query["state"], "code": "c"}
        )
        token_call = self.graph.calls("POST", "/token")[-1]
        self.assertEqual(
            str(token_call.url), f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
        )
        connection.refresh_from_db()
        self.assertEqual(connection.status, Connection.Status.ACTIVE)


class MailTests(GraphTestCase):
    def test_mail_is_followed_with_delta_queries(self):
        self.graph.folders["inbox"] = [
            message("in"),
            message("draft", isDraft=True),
            message("other", inferenceClassification="other"),
            message("staff", sender="attorney@example.test"),
            message("unknown", sender="new.lead@example.test"),
            {"id": "gone", "@removed": {"reason": "deleted"}},
        ]
        self.graph.folders["sentitems"] = [message("out", sender=ACCOUNT, to=CLIENT)]
        with mock.patch("django.utils.timezone.now", return_value=NOW):
            run = self.sync("mailbox")
        self.assertEqual(run.status, SyncRun.Status.SUCCEEDED, run.error)
        first = self.graph.calls("GET", "/inbox/messages/delta")[0]
        self.assertEqual(first.url.params["$filter"], "receivedDateTime ge 2026-09-30T15:00:00Z")
        self.assertIn("bodyPreview", first.url.params["$select"])
        pages = self.graph.calls("GET", "/inbox/messages/delta")
        self.assertEqual(pages[1].url.params["$skiptoken"], "page-2")
        stored = {c.external_id: c for c in Communication.objects.all()}
        self.assertEqual(set(stored), {"in", "unknown", "out"})
        self.assertEqual((stored["in"].contact, stored["in"].direction), (self.contact, "inbound"))
        self.assertEqual(
            (stored["out"].contact, stored["out"].direction), (self.contact, "outbound")
        )
        self.assertEqual(stored["in"].logged_by, self.owner)
        links = run.cursor_after["links"]
        self.assertTrue(links["inbox"].endswith("$deltatoken=d-mail"))
        # The next sync follows the delta link, and an expired one starts again.
        self.graph.expired = {"d-mail"}
        self.sync("mailbox")
        later = self.graph.calls("GET", "/inbox/messages/delta")[2:]
        self.assertEqual(later[0].url.params["$deltatoken"], "d-mail")
        self.assertIn("$filter", later[1].url.params)
        self.assertEqual(Communication.objects.count(), 3)

    def test_sent_email_keeps_one_id_from_draft_to_sent_items(self):
        request = send_email(
            self.connection,
            contact=self.contact,
            subject="Forms",
            body="Attached.",
            actor=self.owner,
        )
        self.assertEqual(request.provider_ref, "immutable-1")
        draft = json.loads(self.graph.calls("POST", "/me/messages")[0].content)
        self.assertEqual(draft["toRecipients"], [{"emailAddress": {"address": CLIENT}}])
        self.assertEqual(draft["body"], {"contentType": "Text", "content": "Attached."})
        self.assertTrue(self.graph.calls("POST", "/me/messages/immutable-1/send"))
        self.graph.folders["sentitems"] = [
            message("immutable-1", sender=ACCOUNT, to=CLIENT, bodyPreview="Attached.")
        ]
        self.sync("mailbox")
        stored = Communication.objects.get()
        self.assertEqual((stored.external_id, stored.summary), ("immutable-1", "Attached."))


class CalendarTests(GraphTestCase):
    def test_calendar_delta_and_pushed_appointments(self):
        self.graph.events = {
            "client": event("client"),
            "private": event("private", attendees=()),
            "all-day": event("all-day", isAllDay=True),
        }
        with mock.patch("django.utils.timezone.now", return_value=NOW):
            run = self.sync("calendar")
        params = self.graph.calls("GET", "/calendarView/delta")[0].url.params
        self.assertEqual(params["startDateTime"], (NOW - timedelta(days=30)).isoformat())
        self.assertEqual(run.cursor_after["window_start"], NOW.isoformat())
        pulled = WorkItem.objects.get()
        self.assertEqual((pulled.contact, pulled.scheduled_for), (self.contact, NOW))

        # The window is started again after 30 days.
        with mock.patch("django.utils.timezone.now", return_value=NOW + timedelta(days=31)):
            self.sync("calendar")
        restarts = [
            r
            for r in self.graph.calls("GET", "/calendarView/delta")
            if "startDateTime" in r.url.params
        ]
        self.assertEqual(len(restarts), 2)

        with self.captureOnCommitCallbacks(execute=True):
            item = WorkItem.objects.create(
                kind=WorkItem.Kind.APPOINTMENT,
                title="Interview preparation",
                contact=self.contact,
                assigned_to=self.owner,
                scheduled_for=NOW,
            )
        push_appointment.call(item.pk)
        body = json.loads(self.graph.calls("POST", "/me/events")[-1].content)
        self.assertNotIn("attendees", body)
        self.assertEqual(body["start"], {"dateTime": "2026-10-30T15:00:00", "timeZone": "UTC"})
        item.scheduled_for = NOW + timedelta(days=1)
        with self.captureOnCommitCallbacks(execute=True):
            item.save()
        push_appointment.call(item.pk)
        patch = self.graph.calls("PATCH", "/me/events/")[-1]
        self.assertEqual(json.loads(patch.content)["start"]["dateTime"], "2026-10-31T15:00:00")


class NotificationTests(GraphTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("connection_webhooks", args=[self.connection.pk]), {"register": "on"}
        )
        self.response = response
        self.url = response.context["webhook_url"]
        self.secret = response.context["secret"]
        self.connection.refresh_from_db()
        self.subs = self.connection.config["subscriptions"]

    def post(self, body, query=""):
        with self.captureOnCommitCallbacks(execute=True):
            answer = self.client.post(self.url + query, body, content_type="application/json")
        for queued in WebhookEvent.objects.filter(status=WebhookEvent.Status.QUEUED):
            process_webhook_event.call(queued.pk)
        return answer

    def sub_id(self, name):
        return next(i for i, detail in self.subs.items() if detail["name"] == name)

    def test_subscriptions_are_created_for_mail_and_calendar(self):
        self.assertContains(
            self.response, "Graph will notify this CRM of new mail and calendar changes."
        )
        made = self.graph.subscriptions
        self.assertEqual(
            sorted(s["resource"] for s in made.values()),
            [
                "me/events",
                "me/mailFolders('inbox')/messages",
                "me/mailFolders('sentitems')/messages",
            ],
        )
        self.assertTrue(all(s["clientState"] == self.secret for s in made.values()))
        self.assertTrue(all(s["notificationUrl"] == self.url for s in made.values()))
        self.assertEqual(self.connection.credentials["webhook_url"], self.url)

    def test_validation_notifications_and_refusals(self):
        challenge = self.client.post(
            self.url + "?validationToken=Validation%3A+fictional", "", content_type="text/plain"
        )
        self.assertEqual(
            (challenge.status_code, challenge.content), (200, b"Validation: fictional")
        )
        self.assertEqual(challenge["Content-Type"], "text/plain")

        self.graph.messages = {"m1": message("m1")}
        answer = self.post(notification(self.sub_id("inbox"), "m1", state=self.secret))
        self.assertEqual((answer.status_code, answer.content), (202, b""))
        self.assertEqual(Communication.objects.get().external_id, "m1")
        self.assertTrue(self.graph.calls("GET", "/me/messages/m1"))

        forged = self.post(notification(self.sub_id("inbox"), "m2", state="guess"))
        self.assertEqual(forged.status_code, 401)
        unknown = self.post(notification("sub-unknown", "m2", state=self.secret))
        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(self.post(b'{"value": []}').status_code, 401)

    def test_calendar_deletions_cancel_the_appointment(self):
        self.graph.events = {"client": event("client")}
        self.sync("calendar")
        self.post(
            notification(self.sub_id("events"), "client", change="deleted", state=self.secret)
        )
        self.assertTrue(WorkItem.objects.get().title.startswith("Cancelled: "))

    def test_subscriptions_are_renewed_or_made_again_and_removed_on_disconnect(self):
        soon = (NOW + timedelta(hours=2)).isoformat()
        config = self.connection.config
        for detail in config["subscriptions"].values():
            detail["expires"] = soon
        Connection.objects.filter(pk=self.connection.pk).update(config=config)
        self.graph.lapsed = {self.sub_id("inbox")}
        with mock.patch("django.utils.timezone.now", return_value=NOW):
            self.sync("mailbox")
        names = {d["name"]: (i, d) for i, d in self.connection.config["subscriptions"].items()}
        self.assertNotEqual(names["inbox"][0], self.sub_id("inbox"))
        self.assertEqual(names["sentitems"][0], self.sub_id("sentitems"))
        self.assertEqual(names["sentitems"][1]["expires"], (NOW + timedelta(days=2)).isoformat())
        self.assertEqual(names["events"][1]["expires"], soon)
        disconnect(self.connection, self.owner)
        self.assertEqual(self.graph.subscriptions, {})
