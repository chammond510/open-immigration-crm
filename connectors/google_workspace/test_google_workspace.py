import base64
import email
import json
import time
from datetime import UTC, datetime, timedelta
from email import policy as email_policy
from unittest import mock

import httpx
from django.test import TestCase
from django.urls import reverse

from crm.appointments import push_appointment, remove_appointment_event
from crm.comms.emailing import send_email
from crm.comms.models import Communication
from crm.connect import http, policy
from crm.connect.models import Connection, ExternalRef, OutboundRequest, SyncRun
from crm.connect.services import acknowledge_data_flows, disconnect
from crm.connect.tasks import run_sync
from crm.connect.testing import ConnectorContract
from crm.models import WorkItem
from crm.tests.factories import make_contact, make_matter, make_user

from . import BACKFILL_DAYS, CRM_MARK, RESYNC_DAYS, GoogleWorkspaceConnector

TOKEN = "fictional-google-access-token"
ACCOUNT = "attorney@firm.example.test"
CLIENT = "lena@example.test"
NOW = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)


def ms(moment):
    return str(int(moment.timestamp() * 1000))


def gmail_message(message_id, *, labels=("INBOX",), sender=CLIENT, to=ACCOUNT, **headers):
    names = {"From": sender, "To": to, "Subject": f"Fictional subject {message_id}", **headers}
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "labelIds": list(labels),
        "snippet": "Thanks for the update &amp; the forms",
        "internalDate": ms(NOW),
        "payload": {"headers": [{"name": k, "value": v} for k, v in names.items()]},
    }


def calendar_event(event_id, *, attendees=(CLIENT,), start=NOW, **extra):
    return {
        "id": event_id,
        "status": "confirmed",
        "etag": f'"etag-{event_id}"',
        "summary": "Consultation",
        "location": "Office",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
        "attendees": [{"email": ACCOUNT, "self": True}, *({"email": a} for a in attendees)],
        **extra,
    }


class FakeGoogle:
    """Answers like Gmail and Google Calendar for one fictional staff member."""

    def __init__(self):
        self.requests = []
        self.messages = {}
        self.history = []
        self.expired_history = set()
        self.events = {}
        self.event_pages = None
        self.created = 0

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={})
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": {"code": 401}})
        path, params = request.url.path, request.url.params
        if path.startswith("/gmail/v1/users/me"):
            return self.gmail(request, path.removeprefix("/gmail/v1/users/me"), params)
        return self.calendar(request, path.removeprefix("/calendar/v3/calendars/primary"), params)

    def gmail(self, request, path, params):
        if path == "/profile":
            return httpx.Response(200, json={"emailAddress": ACCOUNT, "historyId": "2000"})
        if path == "/history":
            if params["startHistoryId"] in self.expired_history:
                return httpx.Response(404, json={"error": {"code": 404}})
            page = int(params.get("pageToken", "0"))
            chunk = self.history[page * 2 : page * 2 + 2]
            body = {
                "history": [{"messagesAdded": [{"message": {"id": i}}]} for i in chunk],
                "historyId": "2100",
            }
            if len(self.history) > (page + 1) * 2:
                body["nextPageToken"] = str(page + 1)
            return httpx.Response(200, json=body)
        if path == "/messages":
            return httpx.Response(200, json={"messages": [{"id": i} for i in self.messages]})
        if path == "/messages/send":
            raw = json.loads(request.content)["raw"]
            self.sent = email.message_from_bytes(
                base64.urlsafe_b64decode(raw), policy=email_policy.default
            )
            return httpx.Response(200, json={"id": "sent-1", "labelIds": ["SENT"]})
        message = self.messages.get(path.removeprefix("/messages/"))
        return httpx.Response(200, json=message) if message else httpx.Response(404)

    def calendar(self, request, path, params):
        if request.method == "GET":
            if params.get("syncToken") == "expired":
                return httpx.Response(410, json={"error": {"code": 410}})
            if self.event_pages is not None:
                return httpx.Response(200, json=self.event_pages.pop(0))
            return httpx.Response(
                200, json={"items": list(self.events.values()), "nextSyncToken": "sync-1"}
            )
        event_id = path.removeprefix("/events").strip("/")
        if request.method == "DELETE":
            return httpx.Response(204 if self.events.pop(event_id, None) else 410)
        body = json.loads(request.content)
        if request.method == "PATCH":
            if event_id not in self.events:
                return httpx.Response(404)
            self.events[event_id].update(body)
            return httpx.Response(200, json=self.events[event_id])
        self.created += 1
        event = {**body, "id": f"crm-event-{self.created}", "etag": f'"e{self.created}"'}
        self.events[event["id"]] = event
        return httpx.Response(200, json=event)

    def calls(self, method):
        return [r for r in self.requests if r.method == method]


class FakeGoogleMixin:
    def setUp(self):
        super().setUp()
        self.google = FakeGoogle()
        transport = http.use_transport(httpx.MockTransport(lambda r: self.google(r)))
        transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)
        patcher = mock.patch.object(http, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)


def signed_in(**extra):
    return {
        "access_token": TOKEN,
        "refresh_token": "fictional-refresh-token",
        "token_type": "Bearer",
        "expires_at": time.time() + 3600,
        **extra,
    }


class GoogleContractTests(FakeGoogleMixin, ConnectorContract, TestCase):
    connector_class = GoogleWorkspaceConnector
    valid_credentials = signed_in()
    invalid_credentials = signed_in(access_token="expired-elsewhere")
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

    def setUp(self):
        super().setUp()
        self.google.messages = {"m1": gmail_message("m1")}
        self.google.events = {"e1": calendar_event("e1")}


class GoogleTestCase(FakeGoogleMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user()
        self.owner = make_user("associate", superuser=False)
        self.contact = make_contact(self.owner, email=CLIENT)
        self.connection = Connection.objects.create(
            connector_key="google_workspace",
            label="Attorney's Google",
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
        return run


class MailTests(GoogleTestCase):
    def test_first_sync_brings_in_client_mail_only(self):
        self.google.messages = {
            "in": gmail_message("in"),
            "out": gmail_message("out", labels=("SENT",), sender=ACCOUNT, to=f"Lena <{CLIENT}>"),
            "spam": gmail_message("spam", labels=("SPAM",)),
            "promo": gmail_message("promo", labels=("INBOX", "CATEGORY_PROMOTIONS")),
            "list": gmail_message("list", **{"List-Unsubscribe": "<mailto:x@example.test>"}),
            "auto": gmail_message("auto", **{"Auto-Submitted": "auto-replied"}),
            "staff": gmail_message("staff", sender="attorney@example.test"),
            "self": gmail_message("self", sender=ACCOUNT),
            "unknown": gmail_message("unknown", sender="new.lead@example.test"),
        }
        with mock.patch("django.utils.timezone.now", return_value=NOW):
            run = self.sync("mailbox")
        self.assertEqual(run.status, SyncRun.Status.SUCCEEDED, run.error)
        query = next(r for r in self.google.requests if r.url.path.endswith("/messages"))
        self.assertEqual(
            query.url.params["q"], f"newer_than:{BACKFILL_DAYS}d (in:inbox OR in:sent)"
        )
        self.assertEqual(run.cursor_after, {"history_id": "2000"})
        stored = {c.external_id: c for c in Communication.objects.all()}
        self.assertEqual(set(stored), {"in", "out", "unknown"})
        self.assertEqual((stored["in"].contact, stored["in"].direction), (self.contact, "inbound"))
        self.assertEqual(
            (stored["out"].contact, stored["out"].direction), (self.contact, "outbound")
        )
        self.assertEqual(stored["in"].summary, "Thanks for the update & the forms")
        self.assertEqual(stored["in"].subject, "Fictional subject in")
        self.assertEqual(stored["in"].logged_by, self.owner)
        self.assertIsNone(stored["unknown"].contact)
        # Only headers are asked for, never full messages.
        reads = [r for r in self.google.requests if "/messages/" in r.url.path]
        self.assertTrue(all(r.url.params["format"] == "metadata" for r in reads))

    def test_later_syncs_follow_history_and_recover_when_it_expires(self):
        self.google.messages = {i: gmail_message(i) for i in ("a", "b", "c")}
        self.google.history = ["a", "b", "c", "a"]
        batch = GoogleWorkspaceConnector(self.connection).sync("mailbox", {"history_id": "1500"})
        self.assertEqual([r.external_id for r in batch.records], ["a", "b", "c"])
        self.assertEqual(batch.cursor, {"history_id": "2100"})
        self.google.expired_history = {"1500"}
        again = GoogleWorkspaceConnector(self.connection).sync("mailbox", {"history_id": "1500"})
        query = [r for r in self.google.requests if r.url.path.endswith("/messages")][-1]
        self.assertEqual(query.url.params["q"], f"newer_than:{RESYNC_DAYS}d (in:inbox OR in:sent)")
        self.assertEqual(again.cursor, {"history_id": "2000"})

    def test_unmatched_mail_can_be_left_out(self):
        self.connection.config = {"unmatched": "skip"}
        self.connection.save()
        self.google.messages = {
            "known": gmail_message("known"),
            "unknown": gmail_message("unknown", sender="new.lead@example.test"),
        }
        self.sync("mailbox")
        self.assertEqual(
            list(Communication.objects.values_list("external_id", flat=True)), ["known"]
        )

    def test_sending_email_from_the_contact_page(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("contact_detail", args=[self.contact.pk]))
        self.assertContains(page, "Send an email")
        self.assertContains(page, f"Message to {CLIENT}")
        response = self.client.post(
            reverse("communication_send_email"),
            {
                "contact": self.contact.pk,
                "connection": self.connection.pk,
                "subject": "Your interview\nnotice",
                "body": "The interview notice arrived. We will call you to prepare.",
            },
            follow=True,
        )
        self.assertContains(response, "Email sent through Attorney&#x27;s Google.")
        sent = self.google.sent
        self.assertEqual(
            (sent["From"], sent["To"], sent["Subject"]), (ACCOUNT, CLIENT, "Your interview notice")
        )
        self.assertIn("We will call you to prepare.", sent.get_content())
        logged = Communication.objects.get()
        self.assertEqual(
            (logged.external_id, logged.channel, logged.direction), ("sent-1", "email", "outbound")
        )
        # Gmail's own copy, once synced, lands on the same record without repeating the text.
        self.google.messages = {
            "sent-1": {
                **gmail_message("sent-1", labels=("SENT",), sender=ACCOUNT, to=CLIENT),
                "snippet": "The interview notice arrived. We will call you",
            }
        }
        self.google.history = ["sent-1"]
        GoogleWorkspaceConnector(self.connection).sync("mailbox", {"history_id": "1500"})
        self.sync("mailbox")
        logged.refresh_from_db()
        self.assertEqual(Communication.objects.count(), 1)
        self.assertEqual(
            logged.summary, "The interview notice arrived. We will call you to prepare."
        )

    def test_others_cannot_send_from_a_personal_mailbox(self):
        other = make_user("paralegal", superuser=False)
        self.client.force_login(other)
        page = self.client.get(reverse("contact_detail", args=[self.contact.pk]))
        self.assertNotContains(page, "Send an email")
        response = self.client.post(
            reverse("communication_send_email"),
            {
                "contact": self.contact.pk,
                "connection": self.connection.pk,
                "subject": "x",
                "body": "y",
            },
        )
        self.assertEqual(response.status_code, 403)
        request = send_email(
            self.connection, contact=self.contact, subject="Hi", body="Hello", actor=other
        )
        self.assertEqual(request.decision_code, "personal_connection")

    def test_disconnecting_revokes_the_sign_in(self):
        disconnect(self.connection, self.owner)
        revoke = self.google.calls("POST")[-1]
        self.assertEqual(str(revoke.url), "https://oauth2.googleapis.com/revoke")
        self.assertEqual(revoke.content, b"token=fictional-refresh-token")


class CalendarPullTests(GoogleTestCase):
    def test_events_with_a_contact_become_appointments(self):
        self.google.events = {
            "client": calendar_event("client"),
            "private": calendar_event("private", attendees=()),
            "all-day": {**calendar_event("all-day"), "start": {"date": "2026-10-31"}},
        }
        with mock.patch("django.utils.timezone.now", return_value=NOW):
            run = self.sync("calendar")
        first = self.google.calls("GET")[-1]
        self.assertEqual(
            first.url.params["timeMin"], (NOW - timedelta(days=BACKFILL_DAYS)).isoformat()
        )
        self.assertEqual(first.url.params["singleEvents"], "true")
        self.assertEqual(run.cursor_after, {"sync_token": "sync-1"})
        item = WorkItem.objects.get()
        self.assertEqual(
            (item.contact, item.scheduled_for, item.assigned_to), (self.contact, NOW, self.owner)
        )
        self.google.event_pages = [
            {"items": [{"id": "client", "status": "cancelled"}], "nextPageToken": "p2"},
            {"items": [], "nextSyncToken": "sync-2"},
        ]
        run = self.sync("calendar")
        self.assertEqual(self.google.calls("GET")[-2].url.params["syncToken"], "sync-1")
        self.assertEqual(run.cursor_after, {"sync_token": "sync-2"})
        item.refresh_from_db()
        self.assertTrue(item.title.startswith("Cancelled: "))

    def test_an_expired_sync_token_starts_over(self):
        self.google.events = {"client": calendar_event("client")}
        batch = GoogleWorkspaceConnector(self.connection).sync(
            "calendar", {"sync_token": "expired"}
        )
        self.assertEqual(batch.cursor, {"sync_token": "sync-1"})
        self.assertIn("timeMin", self.google.calls("GET")[-1].url.params)


class CalendarPushTests(GoogleTestCase):
    def appointment(self, **fields):
        with self.captureOnCommitCallbacks(execute=True):
            item = WorkItem.objects.create(
                **{
                    "kind": WorkItem.Kind.APPOINTMENT,
                    "title": "Interview preparation",
                    "contact": self.contact,
                    "matter": make_matter(self.contact, self.owner),
                    "assigned_to": self.owner,
                    "scheduled_for": NOW,
                    **fields,
                }
            )
        push_appointment.call(item.pk)
        return item

    def update(self, item, **fields):
        for name, value in fields.items():
            setattr(item, name, value)
        with self.captureOnCommitCallbacks(execute=True):
            item.save()
        push_appointment.call(item.pk)

    def test_appointments_are_put_on_the_owners_calendar_quietly(self):
        item = self.appointment()
        create = self.google.calls("POST")[-1]
        self.assertEqual(create.url.params["sendUpdates"], "none")
        body = json.loads(create.content)
        self.assertNotIn("attendees", body)
        self.assertEqual(body["summary"], "Interview preparation")
        self.assertEqual(body["start"]["dateTime"], NOW.isoformat())
        self.assertIn(f"Contact: {self.contact}", body["description"])
        self.assertEqual(
            body["extendedProperties"]["private"], {"crm": CRM_MARK, "crmId": str(item.pk)}
        )
        ref = ExternalRef.objects.get()
        self.assertEqual((ref.external_id, ref.created_by_crm), ("crm-event-1", True))
        request = OutboundRequest.objects.get()
        self.assertTrue(request.is_automation)
        self.assertEqual(request.status, OutboundRequest.Status.SENT)

        # Moving it in the CRM moves the event; moving the event moves it back here.
        self.update(item, scheduled_for=NOW + timedelta(days=1))
        patch = self.google.calls("PATCH")[-1]
        self.assertTrue(patch.url.path.endswith("/events/crm-event-1"))
        moved = {
            **self.google.events["crm-event-1"],
            "start": {"dateTime": (NOW + timedelta(days=2)).isoformat()},
            "etag": '"moved"',
        }
        self.google.events = {"crm-event-1": moved}
        requests_before = OutboundRequest.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            self.sync("calendar")
        item.refresh_from_db()
        self.assertEqual(item.scheduled_for, NOW + timedelta(days=2))
        self.assertEqual(item.description, "")
        self.assertEqual(OutboundRequest.objects.count(), requests_before)

        # Giving it to someone else takes it off this calendar; deleting it does too.
        self.update(item, assigned_to=self.admin)
        self.assertTrue(self.google.calls("DELETE")[-1].url.path.endswith("/events/crm-event-1"))
        self.assertFalse(ExternalRef.objects.exists())
        self.update(item, assigned_to=self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            item.delete()
        self.assertEqual(len(self.google.calls("DELETE")), 1)
        ref_event = "crm-event-2"
        remove_appointment_event.call(str(self.connection.pk), ref_event, 99)
        self.assertNotIn(ref_event, self.google.events)

    def test_preview_mode_and_appointments_from_a_calendar(self):
        Connection.objects.filter(pk=self.connection.pk).update(preview_mode=True)
        self.appointment()
        self.assertEqual(OutboundRequest.objects.get().status, OutboundRequest.Status.PREVIEWED)
        self.assertFalse(self.google.calls("POST"))
        Connection.objects.filter(pk=self.connection.pk).update(preview_mode=False)
        self.google.events = {"client": calendar_event("client")}
        self.sync("calendar")
        pulled = WorkItem.objects.get(title="Consultation")
        self.update(pulled, scheduled_for=NOW + timedelta(hours=3))
        self.assertFalse(self.google.calls("PATCH"))
        self.assertFalse(self.google.calls("POST"))

    def test_only_the_owners_appointments_go_on_their_calendar(self):
        self.appointment(assigned_to=self.admin)
        self.assertFalse(OutboundRequest.objects.exists())
        # An automation may keep only the owner's own records in step on their account.
        theirs = self.appointment(assigned_to=self.admin)
        refused = policy.send(
            self.connection, "save_event", {"event_id": ""}, is_automation=True, target=theirs
        )
        self.assertEqual(refused.decision_code, "personal_connection")
        mine = self.appointment()
        email = policy.send(self.connection, "send_email", {}, is_automation=True, target=mine)
        self.assertEqual(email.decision_code, "personal_connection")
