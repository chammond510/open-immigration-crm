import base64
import json
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from crm.appointments import event_payload
from crm.connect import http
from crm.connect.base import ConnectorError
from crm.connect.models import Connection, ConnectorApp, ExternalRef, OutboundRequest
from crm.connect.services import acknowledge_data_flows
from crm.connect.testing import ConnectorContract
from crm.meetings import push_meeting, remove_appointment_meeting
from crm.models import WorkItem
from crm.tests.factories import make_contact, make_user

from . import ZoomConnector

NOW = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
APP_ID, APP_SECRET = "fictional-zoom-app", "fictional-zoom-secret"


def signed_in():
    return {
        "access_token": "fictional-access",
        "refresh_token": "fictional-refresh",
        "token_type": "Bearer",
        "expires_at": time.time() + 3600,
    }


class FakeZoom:
    def __init__(self):
        self.meetings = {}
        self.requests = []
        self.next_id = 85000000001
        self.refuse = None

    def calls(self, method, path=""):
        return [r for r in self.requests if r.method == method and r.url.path.endswith(path)]

    def meeting(self, meeting_id):
        found = self.meetings[meeting_id]
        return {
            "id": int(meeting_id),
            "topic": found["topic"],
            "start_time": found["start_time"],
            "join_url": f"https://zoom.example.test/j/{meeting_id}?pwd=fictional",
            "start_url": f"https://zoom.example.test/s/{meeting_id}?zak=secret",
            "password": "fictional",
        }

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "zoom.us":
            given = base64.b64decode(request.headers.get("authorization", "")[6:]).decode()
            if given != f"{APP_ID}:{APP_SECRET}":
                return httpx.Response(401, json={"reason": "Invalid client_id or client_secret"})
            if path == "/oauth/revoke":
                return httpx.Response(200, json={"status": "success"})
            return httpx.Response(200, json={**signed_in(), "expires_in": 3599, "scope": "x"})
        if self.refuse:
            return httpx.Response(self.refuse, json={"code": 124, "message": "Invalid token."})
        if request.headers.get("authorization") != "Bearer fictional-access":
            return httpx.Response(401, json={"code": 124, "message": "Invalid access token."})
        if path == "/v2/users/me/meetings" and request.method == "GET":
            return httpx.Response(200, json={"meetings": []})
        if path == "/v2/users/me/meetings":
            meeting_id = str(self.next_id)
            self.next_id += 1
            self.meetings[meeting_id] = json.loads(request.content)
            return httpx.Response(201, json=self.meeting(meeting_id))
        meeting_id = path.rsplit("/", 1)[-1]
        if meeting_id not in self.meetings:
            return httpx.Response(404, json={"code": 3001, "message": "Meeting does not exist."})
        if request.method == "PATCH":
            self.meetings[meeting_id].update(json.loads(request.content))
            return httpx.Response(204)
        if request.method == "DELETE":
            del self.meetings[meeting_id]
            return httpx.Response(204)
        return httpx.Response(200, json=self.meeting(meeting_id))


class FakeZoomMixin:
    def setUp(self):
        super().setUp()
        self.zoom = FakeZoom()
        self.enterContext(http.use_transport(httpx.MockTransport(self.zoom)))
        ConnectorApp.objects.create(
            connector_key="zoom", client_id=APP_ID, client_secret=APP_SECRET
        )


class ZoomContractTests(FakeZoomMixin, ConnectorContract, TestCase):
    connector_class = ZoomConnector
    valid_credentials = signed_in()
    invalid_credentials = {**signed_in(), "access_token": "expired"}
    outbound_examples = {
        "save_meeting": {
            "meeting_id": "",
            "title": "Consultation",
            "starts_at": NOW.isoformat(),
            "duration_minutes": 60,
            "time_zone": "America/New_York",
            "crm_id": "1",
        },
        "delete_meeting": {"meeting_id": "85000000099"},
    }


class SignInTests(FakeZoomMixin, TestCase):
    def test_sign_in_sends_the_apps_secret_in_a_basic_header(self):
        owner = make_user("associate", superuser=False)
        self.client.force_login(owner)
        self.client.post(
            reverse("connector_detail", args=["zoom"]),
            {"label": "My Zoom", "capabilities": ["video"], "acknowledge": "on"},
        )
        connection = Connection.objects.get()
        start = self.client.get(reverse("connection_oauth_start", args=[connection.pk]))
        location = urlparse(start["Location"])
        self.assertEqual(f"{location.netloc}{location.path}", "zoom.us/oauth/authorize")
        query = {k: v[0] for k, v in parse_qs(location.query).items()}
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertIn("meeting:write:meeting", query["scope"].split())
        self.client.get(
            reverse("connection_oauth_callback"), {"state": query["state"], "code": "c"}
        )
        token_call = self.zoom.calls("POST", "/oauth/token")[-1]
        form = parse_qs(token_call.content.decode())
        self.assertNotIn("client_secret", form)
        self.assertTrue(form["code_verifier"][0])
        connection.refresh_from_db()
        self.assertEqual(connection.status, Connection.Status.ACTIVE)
        # Refreshing uses the same header, and disconnecting revokes the sign-in.
        connection.credentials = {**connection.credentials, "expires_at": time.time()}
        connection.save()
        ZoomConnector(connection).test_connection()
        refresh = parse_qs(self.zoom.calls("POST", "/oauth/token")[-1].content.decode())
        self.assertEqual(refresh["grant_type"], ["refresh_token"])
        ZoomConnector(connection).revoke()
        revoked = parse_qs(self.zoom.calls("POST", "/oauth/revoke")[-1].content.decode())
        self.assertEqual(revoked, {"token": ["fictional-access"]})


class MeetingTests(FakeZoomMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user()
        self.owner = make_user("associate", superuser=False)
        self.contact = make_contact(self.owner)
        self.connection = Connection.objects.create(
            connector_key="zoom",
            label="My Zoom",
            owner=self.owner,
            enabled_capabilities=["video"],
            credentials=signed_in(),
            preview_mode=False,
        )
        acknowledge_data_flows(self.connection, self.admin)

    def appointment(self, **fields):
        with self.captureOnCommitCallbacks(execute=True):
            item = WorkItem.objects.create(
                **{
                    "kind": WorkItem.Kind.APPOINTMENT,
                    "title": "Consultation",
                    "contact": self.contact,
                    "assigned_to": self.owner,
                    "scheduled_for": NOW,
                    **fields,
                }
            )
        push_meeting.call(item.pk)
        item.refresh_from_db()
        return item

    def update(self, item, **fields):
        for name, value in fields.items():
            setattr(item, name, value)
        with self.captureOnCommitCallbacks(execute=True):
            item.save()
        push_meeting.call(item.pk)
        item.refresh_from_db()

    def test_a_meeting_is_made_moved_and_removed_with_the_appointment(self):
        item = self.appointment()
        create = json.loads(self.zoom.calls("POST", "/users/me/meetings")[-1].content)
        self.assertEqual(
            create,
            {
                "topic": "Consultation",
                "type": 2,
                "start_time": "2026-11-03T15:00:00Z",
                "duration": 60,
                "timezone": settings.TIME_ZONE,
            },
        )
        self.assertEqual(item.meeting_url, "https://zoom.example.test/j/85000000001?pwd=fictional")
        ref = ExternalRef.objects.get()
        self.assertEqual((ref.external_id, ref.created_by_crm), ("85000000001", True))
        calendar = event_payload(item, "")["description"]
        self.assertIn(f"Video meeting: {item.meeting_url}", calendar)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("work_list")), item.meeting_url)

        # Saving without a change to the title or time sends nothing.
        self.update(item, description="Bring the passport.")
        self.assertEqual(len(self.zoom.calls("PATCH")), 0)
        # Moving it moves the meeting; the link stays.
        self.update(item, scheduled_for=NOW + timedelta(days=1))
        self.assertEqual(
            json.loads(self.zoom.calls("PATCH")[-1].content)["start_time"], "2026-11-04T15:00:00Z"
        )
        self.update(item, title="Consultation, moved")
        self.assertEqual(self.zoom.meetings["85000000001"]["topic"], "Consultation, moved")
        self.assertEqual(len(self.zoom.calls("GET", "/meetings/85000000001")), 2)
        self.assertEqual(len(self.zoom.calls("POST", "/users/me/meetings")), 1)

        # Giving it to someone else removes the meeting and its link.
        self.update(item, assigned_to=self.admin)
        self.assertEqual(self.zoom.meetings, {})
        delete = self.zoom.calls("DELETE")[-1]
        self.assertEqual(delete.url.params["cancel_meeting_reminder"], "false")
        self.assertEqual((item.meeting_url, ExternalRef.objects.count()), ("", 0))

        # Back with its owner it gets a new meeting; deleting it deletes that one.
        self.update(item, assigned_to=self.owner)
        self.assertIn("85000000002", item.meeting_url)
        item_pk = item.pk
        with self.captureOnCommitCallbacks(execute=True) as queued:
            item.delete()
        self.assertEqual((len(queued), ExternalRef.objects.count()), (1, 0))
        # The worker runs what was queued.
        remove_appointment_meeting.call(str(self.connection.pk), "85000000002", item_pk)
        self.assertEqual(self.zoom.meetings, {})
        remove_appointment_meeting.call("00000000-0000-0000-0000-000000000000", "1", 1)

    def test_a_meeting_deleted_at_zoom_is_made_again(self):
        item = self.appointment()
        self.zoom.meetings.clear()
        self.update(item, scheduled_for=NOW + timedelta(hours=2))
        self.assertIn("85000000002", item.meeting_url)
        self.assertEqual(ExternalRef.objects.get().external_id, "85000000002")

    def test_appointments_that_get_no_meeting(self):
        pasted = self.appointment(meeting_url="https://meet.example.test/firm-room")
        unscheduled = self.appointment(scheduled_for=None)
        done = self.appointment(is_completed=True)
        task = self.appointment(kind=WorkItem.Kind.TASK)
        other = self.appointment(assigned_to=self.admin)
        self.assertEqual(self.zoom.calls("POST", "/users/me/meetings"), [])
        self.assertEqual(pasted.meeting_url, "https://meet.example.test/firm-room")
        for item in (unscheduled, done, task, other):
            self.assertEqual(item.meeting_url, "")
        # Unscheduling an appointment removes its meeting.
        item = self.appointment()
        self.update(item, scheduled_for=None)
        self.assertEqual((self.zoom.meetings, item.meeting_url), ({}, ""))

    def test_held_in_preview_mode_or_refused(self):
        self.connection.preview_mode = True
        self.connection.save()
        item = self.appointment()
        self.assertEqual(item.meeting_url, "")
        self.assertEqual(OutboundRequest.objects.get().status, OutboundRequest.Status.PREVIEWED)
        self.connection.preview_mode = False
        self.connection.save()
        self.zoom.refuse = 401
        self.update(item, title="Retry")
        self.assertEqual(OutboundRequest.objects.first().status, OutboundRequest.Status.FAILED)
        self.assertEqual(item.meeting_url, "")

    def test_zoom_answers(self):
        connector = ZoomConnector(self.connection)
        payload = ZoomContractTests.outbound_examples["save_meeting"]
        # Only the ID and join link come back; the host's start link is never kept.
        self.assertEqual(set(connector.perform("save_meeting", payload)), {"id", "join_url"})
        for status, message in (
            (403, "allow every permission"),
            (400, "refused the meeting"),
            (429, "100 a day"),
        ):
            self.zoom.refuse = status
            with self.subTest(status=status), self.assertRaisesMessage(ConnectorError, message):
                connector.test_connection()
        self.zoom.refuse = None
        with self.assertRaisesMessage(ConnectorError, "could not find"):
            connector.call("GET", "/meetings/1")
        with self.assertRaisesMessage(ConnectorError, "cannot send_text"):
            connector.perform("send_text", {})
        with self.assertRaisesMessage(ConnectorError, "did not include the meeting's link"):
            connector.answer({"id": 1, "join_url": "http://zoom.example.test/j/1"})

    def test_the_link_can_be_pasted_by_hand(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("work_create"))
        self.assertContains(page, "Video meeting link")
        self.assertContains(page, "Connected through")
        response = self.client.post(
            reverse("work_create"),
            {
                "kind": "appointment",
                "title": "Interview prep",
                "priority": "normal",
                "assigned_to": self.admin.pk,
                "scheduled_for": "2026-11-05T10:00",
                "meeting_url": "http://meet.example.test/room",
            },
        )
        self.assertContains(response, "starts with https://")
