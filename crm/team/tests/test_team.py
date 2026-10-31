from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.connect.base import Capability
from crm.connect.models import Connection, OutboundRequest
from crm.connect.registry import registry
from crm.connect.services import acknowledge_data_flows
from crm.models import AuditLog, Contact, Matter, TimeEntry, TwoFactorDevice, WorkItem
from crm.team.services import may_grant, may_manage
from crm.tests.factories import make_contact, make_matter, make_user
from crm.tests.test_two_factor import enroll

User = get_user_model()
PASSWORD = "Fictional-New-Password-77!"
ExampleConnector = registry.get("example")


class EmailExampleConnector(ExampleConnector):
    """The example service, sending the CRM's own email instead of texts."""

    key = "example_email"
    capabilities = frozenset({Capability.SYSTEM_EMAIL})
    outbound_actions = {"send_system_email": Capability.SYSTEM_EMAIL}
    sent = []

    def perform(self, action, payload):
        EmailExampleConnector.sent.append(payload)
        return f"example-email-{len(EmailExampleConnector.sent)}"


def connect_email(owner, *, preview=False):
    connection = Connection.objects.create(
        connector_key="example_email",
        label="Firm mail",
        enabled_capabilities=["system_email"],
        preview_mode=preview,
        created_by=owner,
    )
    acknowledge_data_flows(connection, owner)
    return connection


def add_person(client, role="", **fields):
    return client.post(
        reverse("team_add"),
        {
            "first_name": "Noor",
            "last_name": "Example",
            "email": "Noor@Example.test",
            "role": role,
            **fields,
        },
    )


class TeamPageTests(TestCase):
    def setUp(self):
        self.office = make_user("office", role="Office administrator")
        self.partner = make_user("partner", role="Managing partner")
        self.paralegal = make_user("paralegal", role="Paralegal")
        self.client.force_login(self.office)

    def test_only_people_who_manage_staff_see_the_team(self):
        page = self.client.get(reverse("team_list"))
        self.assertContains(page, "Office administrator")
        self.assertContains(page, "(you)")
        self.assertContains(page, 'href="/settings/team/"')
        self.assertContains(page, "Billing, Voids and rates, Delete records")
        self.client.force_login(self.paralegal)
        self.assertEqual(self.client.get(reverse("team_list")).status_code, 403)
        page = self.client.get(reverse("dashboard"))
        self.assertNotContains(page, 'href="/settings/team/"')
        self.assertContains(page, 'href="/accounts/profile/"')

    def test_adding_someone_shows_a_one_time_link_without_email(self):
        attorney = Group.objects.get(name="Attorney")
        response = add_person(self.client, attorney.pk)
        person = User.objects.get(email="noor@example.test")
        self.assertEqual(person.username, "noor@example.test")
        self.assertTrue(person.is_staff and person.is_active)
        self.assertFalse(person.has_usable_password())
        self.assertEqual(list(person.groups.all()), [attorney])
        self.assertContains(response, "Not emailed: no email delivery service is connected")
        self.assertEqual(response["Cache-Control"], "no-store")
        link = response.context["link"]
        self.assertIn("/accounts/welcome/", link)
        self.assertTrue(AuditLog.objects.filter(action="user.invited", user=self.office).exists())
        listing = self.client.get(reverse("team_list"))
        self.assertContains(listing, "Invited")

        # The invited person chooses a password, then the link is spent.
        self.client.logout()
        form_page = self.client.get(link, follow=True)
        self.assertContains(form_page, "noor@example.test")
        set_url = form_page.redirect_chain[-1][0]
        done = self.client.post(
            set_url, {"new_password1": PASSWORD, "new_password2": PASSWORD}, follow=True
        )
        self.assertContains(done, "Your password is saved")
        signed_in = self.client.post(
            reverse("login"), {"username": "noor@example.test", "password": PASSWORD}
        )
        self.assertRedirects(signed_in, reverse("dashboard"))
        self.client.logout()
        self.assertContains(self.client.get(link, follow=True), "This link cannot be used")
        self.assertTrue(AuditLog.objects.filter(action="user.password_set_from_invitation"))

    def test_invitations_go_by_email_when_the_firm_can_send_it(self):
        EmailExampleConnector.sent = []
        with mock.patch.dict(registry._connectors, {"example_email": EmailExampleConnector}):
            connect_email(self.partner)
            response = add_person(self.client)
        self.assertContains(response, "Invitation sent")
        self.assertNotContains(response, "/accounts/welcome/")
        (email,) = EmailExampleConnector.sent
        self.assertEqual(email["to"], "noor@example.test")
        self.assertIn("/accounts/welcome/", email["body"])
        self.assertIn("Your username is noor@example.test", email["body"])
        request = OutboundRequest.objects.get()
        self.assertEqual(request.summary, "Staff invitation email")
        self.assertNotIn("welcome", request.provider_ref + request.summary)

    def test_held_or_refused_email_falls_back_to_the_link(self):
        with mock.patch.dict(registry._connectors, {"example_email": EmailExampleConnector}):
            connect_email(self.partner, preview=True)
            response = add_person(self.client)
        self.assertContains(response, "the email delivery service is in preview mode")
        self.assertIn("/accounts/welcome/", response.context["link"])

    def test_an_invitation_needs_an_address_to_email(self):
        add_person(self.client)
        person = User.objects.get(email="noor@example.test")
        User.objects.filter(pk=person.pk).update(email="")
        with mock.patch.dict(registry._connectors, {"example_email": EmailExampleConnector}):
            connect_email(self.partner)
            response = self.client.post(reverse("team_invite", args=[person.pk]))
        self.assertContains(response, "Add a valid email address for Noor Example first")
        self.assertIn("/accounts/welcome/", response.context["link"])

    def test_nobody_gives_a_role_with_permissions_they_lack(self):
        partner_role = Group.objects.get(name="Managing partner")
        page = self.client.get(reverse("team_add"))
        offered = {group.name for group in page.context["form"].fields["role"].queryset}
        self.assertEqual(offered, {"Attorney", "Paralegal", "Office administrator"})
        response = add_person(self.client, partner_role.pk)
        self.assertContains(response, "Select a valid choice")
        self.assertFalse(User.objects.filter(email="noor@example.test").exists())
        self.assertTrue(may_grant(self.partner, partner_role))
        self.assertFalse(may_grant(self.office, partner_role))

    def test_emails_are_unique(self):
        response = add_person(self.client, email="PARTNER@example.test")
        self.assertContains(response, "Another account already uses this email address.")
        make_user("lee@example.test", superuser=False)
        response = add_person(self.client, email="lee@example.test")
        self.assertContains(response, "Another account already signs in with this address.")

    def test_changing_a_role(self):
        attorney = Group.objects.get(name="Attorney")
        url = reverse("team_detail", args=[self.paralegal.pk])
        response = self.client.post(
            url,
            {
                "first_name": "Casey",
                "last_name": "Example",
                "email": "paralegal@example.test",
                "role": attorney.pk,
            },
        )
        self.assertRedirects(response, url)
        self.paralegal.refresh_from_db()
        self.assertEqual(list(self.paralegal.groups.all()), [attorney])
        self.assertEqual(self.paralegal.username, "paralegal")
        logged = AuditLog.objects.get(action="user.role_changed")
        self.assertEqual(logged.detail, "Paralegal -> Attorney")

        self.client.post(
            url,
            {"first_name": "Casey", "last_name": "Example", "email": "casey@example.test"},
        )
        self.paralegal.refresh_from_db()
        self.assertEqual(list(self.paralegal.groups.all()), [])

    def test_nobody_changes_themselves_or_someone_stronger(self):
        superuser = make_user("root")
        for person in (self.office, self.partner, superuser):
            url = reverse("team_detail", args=[person.pk])
            page = self.client.get(url)
            self.assertFalse(page.context["manageable"])
            self.assertNotContains(page, "Deactivate…")
            self.assertEqual(self.client.post(url, {"first_name": "X"}).status_code, 403)
            for name in ("team_deactivate", "team_two_factor_reset", "team_invite"):
                response = self.client.post(reverse(name, args=[person.pk]))
                self.assertEqual(response.status_code, 403, name)
        self.assertContains(
            self.client.get(reverse("team_detail", args=[self.office.pk])), "This is you."
        )
        self.assertContains(
            self.client.get(reverse("team_detail", args=[superuser.pk])),
            "Only a superuser can change a superuser",
        )
        self.assertTrue(may_manage(self.partner, self.office))
        self.assertTrue(may_manage(superuser, self.partner))
        self.assertFalse(may_manage(self.partner, superuser))

    def test_resetting_two_factor(self):
        enroll(self.paralegal)
        url = reverse("team_detail", args=[self.paralegal.pk])
        self.assertContains(self.client.get(url), "Reset two-factor sign-in")
        response = self.client.post(
            reverse("team_two_factor_reset", args=[self.paralegal.pk]), follow=True
        )
        self.assertContains(response, "Two-factor sign-in was reset.")
        self.assertFalse(TwoFactorDevice.objects.filter(user=self.paralegal).exists())
        self.assertTrue(AuditLog.objects.filter(action="user.two_factor_reset").exists())
        response = self.client.post(
            reverse("team_two_factor_reset", args=[self.paralegal.pk]), follow=True
        )
        self.assertContains(response, "They had not set up two-factor sign-in.")

    def test_a_new_invitation_ends_the_old_one(self):
        first = add_person(self.client).context["link"]
        person = User.objects.get(email="noor@example.test")
        self.assertContains(
            self.client.get(reverse("team_detail", args=[person.pk])), "Send a new invitation"
        )
        second = self.client.post(reverse("team_invite", args=[person.pk])).context["link"]
        self.assertNotEqual(first, second)
        self.client.logout()
        self.assertContains(self.client.get(first, follow=True), "This link cannot be used")
        self.assertContains(self.client.get(second, follow=True), "Choose a password")

    def test_invitations_are_only_for_people_without_a_password(self):
        response = self.client.post(reverse("team_invite", args=[self.paralegal.pk]), follow=True)
        self.assertContains(response, "nothing to resend")
        self.assertTrue(self.paralegal.has_usable_password())

    def test_a_deactivated_person_s_invitation_stops_working(self):
        link = add_person(self.client).context["link"]
        User.objects.filter(email="noor@example.test").update(is_active=False)
        self.client.logout()
        self.assertContains(self.client.get(link, follow=True), "This link cannot be used")


class DeactivateTests(TestCase):
    def setUp(self):
        self.office = make_user("office", role="Office administrator")
        self.leaving = make_user("leaving", role="Attorney")
        self.staying = make_user("staying", role="Attorney")
        self.contact = make_contact(self.leaving)
        self.closed_contact = make_contact(
            self.leaving, first_name="Former", stage=Contact.Stage.CLOSED
        )
        self.matter = make_matter(self.contact, self.leaving)
        self.closed_matter = make_matter(
            self.contact, self.leaving, title="Closed matter", status=Matter.Status.CLOSED
        )
        self.task = WorkItem.objects.create(
            title="Fictional follow-up", assigned_to=self.leaving, contact=self.contact
        )
        self.done = WorkItem.objects.create(
            title="Finished", assigned_to=self.leaving, is_completed=True
        )
        self.timer = TimeEntry.objects.create(
            user=self.leaving, matter=self.matter, started_at=timezone.now() - timedelta(hours=1)
        )
        self.personal = Connection.objects.create(
            connector_key="example", label="Their texts", owner=self.leaving
        )
        self.client.force_login(self.office)

    def test_deactivating_hands_over_open_work_and_ends_access(self):
        url = reverse("team_deactivate", args=[self.leaving.pk])
        page = self.client.get(url)
        self.assertEqual(page.context["counts"], {"work": 1, "matters": 1, "contacts": 1})
        self.assertContains(page, "Their texts")
        leaving_client = self.client_class()
        leaving_client.force_login(self.leaving)

        response = self.client.post(url, {"successor": self.staying.pk}, follow=True)
        self.assertContains(response, "Handed to staying: 1 work item, 1 matter, 1 contact.")
        self.leaving.refresh_from_db()
        self.assertFalse(self.leaving.is_active)
        for record in (self.task, self.matter, self.contact):
            record.refresh_from_db()
            self.assertEqual(record.assigned_to, self.staying)
        for record in (self.done, self.closed_matter, self.closed_contact):
            record.refresh_from_db()
            self.assertEqual(record.assigned_to, self.leaving)
        self.timer.refresh_from_db()
        self.assertIsNotNone(self.timer.stopped_at)
        self.personal.refresh_from_db()
        self.assertEqual(self.personal.status, Connection.Status.DISCONNECTED)
        self.assertTrue(AuditLog.objects.filter(action="user.deactivated").exists())
        # Their open session ends, and they cannot sign in again.
        self.assertEqual(leaving_client.get(reverse("dashboard")).status_code, 302)
        refused = self.client_class().post(
            reverse("login"), {"username": "leaving", "password": "Testing-Password-Only-42!"}
        )
        self.assertContains(refused, "was not recognized")
        self.assertContains(self.client.get(reverse("team_list")), "Deactivated")
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_work_can_be_left_unassigned_and_people_come_back(self):
        self.client.post(reverse("team_deactivate", args=[self.leaving.pk]), {"successor": ""})
        self.task.refresh_from_db()
        self.assertIsNone(self.task.assigned_to)
        page = self.client.get(reverse("team_detail", args=[self.leaving.pk]))
        self.assertContains(page, "Reactivate")
        response = self.client.post(reverse("team_reactivate", args=[self.leaving.pk]), follow=True)
        self.assertContains(response, "can sign in again")
        self.leaving.refresh_from_db()
        self.assertTrue(self.leaving.is_active)

    def test_successors_are_other_active_staff(self):
        form = self.client.get(reverse("team_deactivate", args=[self.leaving.pk])).context["form"]
        choices = set(form.fields["successor"].queryset)
        self.assertNotIn(self.leaving, choices)
        self.assertIn(self.staying, choices)


class ProfileTests(TestCase):
    def test_staff_edit_their_own_name_and_email(self):
        person = make_user("noor@example.test", superuser=False)
        person.email = "noor@example.test"
        person.save()
        self.client.force_login(person)
        page = self.client.get(reverse("profile"))
        self.assertContains(page, "Changing your email changes it too.")
        response = self.client.post(
            reverse("profile"),
            {"first_name": "Noor", "last_name": "Sample", "email": "noor.sample@example.test"},
            follow=True,
        )
        self.assertContains(response, "Your profile is saved.")
        person.refresh_from_db()
        self.assertEqual(person.get_full_name(), "Noor Sample")
        self.assertEqual(person.username, "noor.sample@example.test")

    def test_usernames_that_are_not_emails_stay_and_emails_stay_unique(self):
        person = make_user("casey", superuser=False)
        make_user("other", superuser=False)
        self.client.force_login(person)
        response = self.client.post(
            reverse("profile"),
            {"first_name": "Casey", "last_name": "Example", "email": "other@example.test"},
        )
        self.assertContains(response, "Another account already uses this email address.")
        self.client.post(
            reverse("profile"),
            {"first_name": "Casey", "last_name": "Example", "email": "casey.new@example.test"},
        )
        person.refresh_from_db()
        self.assertEqual((person.username, person.email), ("casey", "casey.new@example.test"))
