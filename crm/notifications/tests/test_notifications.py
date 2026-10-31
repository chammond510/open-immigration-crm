from contextlib import contextmanager
from datetime import timedelta
from unittest import mock

from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from crm.connect.registry import registry
from crm.models import Contact, Matter, WorkItem
from crm.notifications import services
from crm.notifications.models import Notification, Preference
from crm.team.tests.test_team import EmailExampleConnector, connect_email
from crm.tests.factories import make_contact, make_matter, make_user


@contextmanager
def acting_as(user):
    """Save records as if ``user`` made the change in a request."""
    request = RequestFactory().get("/")
    request.user = user
    token = services.current_request.set(request)
    try:
        yield
    finally:
        services.current_request.reset(token)


class AssignmentAlertTests(TestCase):
    def setUp(self):
        self.giver = make_user("giver", role="Attorney")
        self.giver.first_name, self.giver.last_name = "Riley", "Example"
        self.giver.save()
        self.taker = make_user("taker", role="Paralegal")
        self.other = make_user("other", role="Paralegal")
        self.contact = make_contact(first_name="Lena")
        self.matter = make_matter(self.contact, title="Fictional petition")

    def test_assigning_work_to_someone_else_tells_them(self):
        self.client.force_login(self.giver)
        self.client.post(
            reverse("work_create"),
            {
                "kind": WorkItem.Kind.TASK,
                "title": "Gather fictional records",
                "priority": WorkItem.Priority.NORMAL,
                "assigned_to": self.taker.pk,
                "contact": self.contact.pk,
            },
        )
        item = WorkItem.objects.get()
        notification = Notification.objects.get()
        self.assertEqual(notification.recipient, self.taker)
        self.assertEqual(notification.actor, self.giver)
        self.assertEqual(
            notification.text, "Riley Example assigned you a task: Gather fictional records"
        )
        self.assertEqual(notification.url, reverse("work_update", args=[item.pk]))
        self.assertEqual(
            (notification.target_type, notification.target_id), ("work_item", str(item.pk))
        )

        # Saving again, or taking it yourself, tells nobody new.
        self.client.post(
            reverse("work_update", args=[item.pk]),
            {
                "kind": WorkItem.Kind.TASK,
                "title": "Gather fictional records",
                "priority": WorkItem.Priority.HIGH,
                "assigned_to": self.taker.pk,
                "contact": self.contact.pk,
            },
        )
        self.assertEqual(Notification.objects.count(), 1)
        self.client.force_login(self.taker)
        self.client.post(
            reverse("work_update", args=[item.pk]),
            {
                "kind": WorkItem.Kind.TASK,
                "title": "Gather fictional records",
                "priority": WorkItem.Priority.HIGH,
                "assigned_to": self.other.pk,
                "contact": self.contact.pk,
            },
        )
        self.assertEqual(Notification.objects.filter(recipient=self.other).count(), 1)

    def test_matters_and_contacts_tell_their_new_owner(self):
        with acting_as(self.giver):
            self.matter.assigned_to = self.taker
            self.matter.save()
            self.contact.assigned_to = self.taker
            self.contact.save()
        texts = set(self.taker.notifications.values_list("text", flat=True))
        self.assertEqual(
            texts,
            {
                f"Riley Example assigned you matter {self.matter.matter_number}: "
                "Fictional petition",
                "Riley Example assigned you a contact: Lena Example",
            },
        )

    def test_changes_nobody_makes_by_hand_tell_nobody(self):
        WorkItem.objects.create(title="From a calendar", assigned_to=self.taker)
        with acting_as(self.giver), services.quietly():
            WorkItem.objects.create(title="Imported", assigned_to=self.taker)
        with acting_as(self.giver):
            WorkItem.objects.create(title="Mine", assigned_to=self.giver)
            WorkItem.objects.create(title="Nobody's")
            # A record whose assignee was not loaded cannot be compared, so it is skipped.
            item = WorkItem.objects.only("title").get(title="Nobody's")
            item.assigned_to = self.taker
            item.save()
            self.other.is_active = False
            self.other.save()
            WorkItem.objects.create(title="For someone who left", assigned_to=self.other)
        self.assertFalse(Notification.objects.exists())

    def test_the_bell_lists_and_opens_notifications(self):
        with acting_as(self.giver):
            self.matter.assigned_to = self.taker
            self.matter.save()
        notification = Notification.objects.get()
        self.client.force_login(self.taker)
        page = self.client.get(reverse("dashboard"))
        self.assertContains(
            page,
            '<span class="notification-count">1<span class="sr-only"> unread</span>',
            html=False,
        )
        listing = self.client.get(reverse("notification_list"))
        self.assertContains(listing, "Fictional petition")
        self.assertContains(listing, "Mark all as read")
        opened = self.client.get(reverse("notification_open", args=[notification.pk]))
        self.assertRedirects(opened, reverse("matter_detail", args=[self.matter.pk]))
        notification.refresh_from_db()
        self.assertIsNotNone(notification.read_at)
        self.assertNotContains(self.client.get(reverse("dashboard")), "notification-count")

        self.client.force_login(self.other)
        response = self.client.get(reverse("notification_open", args=[notification.pk]))
        self.assertEqual(response.status_code, 404)

    def test_records_read_as_text(self):
        notification = Notification(text="Riley Example assigned you a task: Call")
        self.assertEqual(str(notification), "Riley Example assigned you a task: Call")
        self.assertEqual(str(Preference(user=self.taker)), "Notification preferences for taker")

    def test_marking_everything_read(self):
        with acting_as(self.giver):
            WorkItem.objects.create(title="One", assigned_to=self.taker)
            WorkItem.objects.create(title="Two", assigned_to=self.taker)
        self.client.force_login(self.taker)
        self.client.post(reverse("notifications_read"))
        self.assertFalse(self.taker.notifications.filter(read_at__isnull=True).exists())
        self.assertContains(self.client.get(reverse("notification_list")), "Two")

    def test_only_links_inside_the_crm_are_followed(self):
        notification = Notification.objects.create(
            recipient=self.taker, kind=Notification.Kind.ASSIGNED, text="Odd", url="//example.test/"
        )
        self.client.force_login(self.taker)
        response = self.client.get(reverse("notification_open", args=[notification.pk]))
        self.assertRedirects(response, reverse("notification_list"))


class EmailAlertTests(TestCase):
    def setUp(self):
        EmailExampleConnector.sent = []
        patcher = mock.patch.dict(registry._connectors, {"example_email": EmailExampleConnector})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.giver = make_user("giver", role="Attorney")
        self.taker = make_user("taker", role="Paralegal")
        connect_email(make_user())

    def save_matter(self):
        contact = make_contact(first_name="Lena")
        matter = make_matter(contact, title="Fictional petition")
        with mock.patch.object(services, "email_notification") as task:
            with self.captureOnCommitCallbacks(execute=True), acting_as(self.giver):
                matter.assigned_to = self.taker
                matter.save()
        return matter, task.enqueue

    def test_email_goes_only_to_people_who_ask_and_names_no_client(self):
        _, enqueue = self.save_matter()
        enqueue.assert_not_called()

        Preference.objects.create(user=self.taker, email_assignments=True)
        matter, enqueue = self.save_matter()
        notification = Notification.objects.latest("created_at")
        brief = f"giver assigned you matter {matter.matter_number}"
        enqueue.assert_called_once_with(notification.pk, brief, "http://testserver")
        services.email_notification.call(notification.pk, brief, "http://testserver")
        (email,) = EmailExampleConnector.sent
        self.assertEqual(email["to"], "taker@example.test")
        self.assertIn(f"/notifications/{notification.pk}/", email["body"])
        self.assertNotIn("Lena", email["body"] + email["subject"])
        self.assertNotIn("petition", email["body"] + email["subject"])

    def test_no_email_for_people_who_left_or_notifications_gone(self):
        services.email_notification.call(999, "Gone", "http://testserver")
        notification = Notification.objects.create(
            recipient=self.taker, kind=Notification.Kind.ASSIGNED, text="X", url="/"
        )
        self.taker.is_active = False
        self.taker.save()
        services.email_notification.call(notification.pk, "Left", "http://testserver")
        self.taker.is_active = True
        self.taker.email = ""
        self.taker.save()
        services.email_notification.call(notification.pk, "No address", "http://testserver")
        self.assertEqual(EmailExampleConnector.sent, [])

    def test_work_emails_say_when_but_not_what(self):
        moment = timezone.make_aware(timezone.datetime(2026, 11, 2, 10, 0))
        item = WorkItem(
            title="Call Lena", kind=WorkItem.Kind.APPOINTMENT, scheduled_for=moment, pk=7
        )
        _, text, brief, _ = services.describe(item)
        self.assertEqual(text, "an appointment: Call Lena")
        self.assertEqual(brief, "an appointment for November 2")

    def test_the_profile_turns_email_on(self):
        self.client.force_login(self.taker)
        page = self.client.get(reverse("profile"))
        self.assertContains(page, "Email me when someone gives me work")
        self.assertNotContains(page, "no emails are sent yet")
        self.client.post(
            reverse("profile"),
            {
                "first_name": "Casey",
                "last_name": "Example",
                "email": "taker@example.test",
                "alerts-email_assignments": "on",
            },
        )
        self.assertTrue(Preference.objects.get(user=self.taker).email_assignments)
        with mock.patch("crm.comms.system_email.sender", return_value=None):
            page = self.client.get(reverse("profile"))
        self.assertContains(page, "no emails are sent yet")


class HandOverTests(TestCase):
    def test_deactivation_sends_one_summary(self):
        office = make_user("office", role="Office administrator")
        leaving = make_user("leaving", role="Attorney")
        staying = make_user("staying", role="Attorney")
        contact = make_contact(leaving)
        make_matter(contact, leaving)
        WorkItem.objects.create(title="Open", assigned_to=leaving)
        self.client.force_login(office)
        self.client.post(reverse("team_deactivate", args=[leaving.pk]), {"successor": staying.pk})
        (notification,) = Notification.objects.all()
        self.assertEqual(notification.recipient, staying)
        self.assertEqual(notification.kind, Notification.Kind.HANDED_OVER)
        self.assertEqual(
            notification.text,
            "office handed you leaving's open work: 1 work item, 1 matter, 1 contact",
        )
        self.assertEqual(notification.url, reverse("work_list") + "?owner=mine")


class OverviewTests(TestCase):
    def setUp(self):
        self.me = make_user("me", role="Attorney")
        self.colleague = make_user("colleague", role="Attorney")
        mine = make_contact(self.me, first_name="Mine")
        theirs = make_contact(self.colleague, first_name="Theirs")
        make_matter(mine, self.me, title="My fictional matter")
        make_matter(theirs, self.colleague, title="Their fictional matter")
        WorkItem.objects.create(title="My task", assigned_to=self.me)
        WorkItem.objects.create(
            title="Their task",
            assigned_to=self.colleague,
            scheduled_for=timezone.now() - timedelta(days=1),
        )
        self.client.force_login(self.me)

    def test_the_overview_starts_with_my_own_work(self):
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, "My task")
        self.assertNotContains(page, "Their task")
        self.assertContains(page, "My fictional matter")
        self.assertNotContains(page, "Their fictional matter")
        self.assertEqual(page.context["overdue_count"], 0)
        self.assertEqual((page.context["open_matters"], page.context["active_contacts"]), (1, 1))
        self.assertContains(page, 'href="/matters/?owner=mine"')
        self.assertContains(page, "?stage=inquiry&amp;owner=mine")
        self.assertContains(page, 'aria-current="page">My work</a>')

        firm = self.client.get(reverse("dashboard"), {"view": "firm"})
        self.assertContains(firm, "Their task")
        self.assertContains(firm, "Their fictional matter")
        self.assertEqual(firm.context["overdue_count"], 1)
        self.assertContains(firm, 'aria-current="page">Whole firm</a>')

    def test_an_empty_overview_points_to_the_firm(self):
        self.client.force_login(make_user("new", role="Paralegal"))
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, "No open work assigned to you")
        self.assertContains(page, "No matters assigned to you")

    def test_lists_narrow_to_my_records(self):
        matters = self.client.get(reverse("matter_list"), {"owner": "mine"})
        self.assertEqual([m.title for m in matters.context["matters"]], ["My fictional matter"])
        contacts = self.client.get(reverse("contact_list"), {"owner": "mine"})
        self.assertEqual([c.first_name for c in contacts.context["contacts"]], ["Mine"])
        self.assertEqual(len(self.client.get(reverse("contact_list")).context["contacts"]), 2)
        self.assertEqual(Contact.objects.count(), 2)
        self.assertEqual(Matter.objects.count(), 2)
