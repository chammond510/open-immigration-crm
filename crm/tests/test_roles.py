import uuid
from datetime import timedelta

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm import roles
from crm.connect.models import Connection
from crm.connect.services import can_manage, visible_connections
from crm.models import MatterParty, TimeEntry

from .factories import make_contact, make_matter, make_user

EVERYTHING = {permission for permission, _, _ in roles.PERMISSIONS}
PRESETS = {
    "Managing partner": EVERYTHING,
    "Attorney": {roles.BILLING, roles.DELETE_RECORDS},
    "Paralegal": set(),
    "Office administrator": {
        roles.BILLING,
        roles.MANAGE_BILLING,
        roles.DELETE_RECORDS,
        roles.IMPORT_RECORDS,
        roles.MANAGE_FIRM_SETTINGS,
        roles.MANAGE_STAFF,
    },
}
MISSING = uuid.uuid4()

# (method, URL, the permission it needs). Records that do not exist answer 404 to
# whoever passes the check, so a 403 means the check refused.
GATED = [
    ("post", reverse("document_delete", args=[MISSING]), roles.DELETE_RECORDS),
    ("post", reverse("matter_remove_party", args=[0]), roles.DELETE_RECORDS),
    ("post", reverse("time_entry_delete", args=[0]), roles.DELETE_RECORDS),
    ("post", reverse("communication_delete", args=[MISSING]), roles.DELETE_RECORDS),
    ("post", reverse("notice_delete", args=[MISSING]), roles.DELETE_RECORDS),
    ("post", reverse("workflow_remove", args=[MISSING]), roles.DELETE_RECORDS),
    ("get", reverse("firm_settings"), roles.MANAGE_FIRM_SETTINGS),
    ("get", reverse("workflow_template_list"), roles.MANAGE_FIRM_SETTINGS),
    ("get", reverse("workflow_template_create"), roles.MANAGE_FIRM_SETTINGS),
    ("get", reverse("custom_field_list"), roles.MANAGE_FIRM_SETTINGS),
    ("get", reverse("custom_field_create"), roles.MANAGE_FIRM_SETTINGS),
    ("get", reverse("export_csv"), roles.EXPORT_RECORDS),
    ("get", reverse("import_list"), roles.IMPORT_RECORDS),
    ("get", reverse("import_template"), roles.IMPORT_RECORDS),
    ("get", reverse("import_report", args=[MISSING]), roles.IMPORT_RECORDS),
    ("post", reverse("import_discard", args=[MISSING]), roles.IMPORT_RECORDS),
    ("post", reverse("api_settings_update"), roles.MANAGE_CONNECTIONS),
    ("post", reverse("mcp_settings"), roles.MANAGE_CONNECTIONS),
    ("post", reverse("ai_settings"), roles.MANAGE_CONNECTIONS),
    ("post", reverse("ai_connection_test"), roles.MANAGE_CONNECTIONS),
    ("post", reverse("connector_app", args=["missing"]), roles.MANAGE_CONNECTIONS),
    ("post", reverse("connection_acknowledge", args=[MISSING]), roles.MANAGE_CONNECTIONS),
    ("post", reverse("connection_settings", args=[MISSING]), roles.MANAGE_CONNECTIONS),
    ("post", reverse("connection_ai_use", args=[MISSING]), roles.MANAGE_CONNECTIONS),
    ("post", reverse("connection_events", args=[MISSING]), roles.MANAGE_CONNECTIONS),
    ("post", reverse("invoice_void", args=[MISSING]), roles.MANAGE_BILLING),
    ("post", reverse("payment_void", args=[MISSING, MISSING]), roles.MANAGE_BILLING),
    ("get", reverse("billing_settings"), roles.MANAGE_BILLING),
]


def member(role):
    return make_user("role-" + role.lower().replace(" ", "-"), role=role)


class PresetTests(TestCase):
    def test_the_migration_creates_each_preset_with_its_permissions(self):
        for name, expected in PRESETS.items():
            group = Group.objects.get(name=name)
            granted = {
                f"{p.content_type.app_label}.{p.codename}" for p in group.permissions.all()
            }
            self.assertEqual(granted, expected, name)

    def test_superusers_hold_every_permission_and_inactive_staff_none(self):
        admin = make_user()
        partner = member("Managing partner")
        for permission in EVERYTHING:
            self.assertTrue(roles.can(admin, permission))
            self.assertTrue(roles.can(partner, permission))
        partner.is_active = False
        partner.save()
        self.assertFalse(any(roles.can(partner, permission) for permission in EVERYTHING))

    def test_people_who_are_not_staff_hold_nothing(self):
        partner = member("Managing partner")
        partner.is_staff = False
        partner.save()
        self.assertFalse(roles.can(partner, roles.MANAGE_STAFF))

    def test_staff_without_a_role_do_everyday_work_only(self):
        staff = make_user("newcomer", superuser=False)
        self.assertFalse(any(roles.can(staff, permission) for permission in EVERYTHING))
        self.client.force_login(staff)
        for name in ("dashboard", "contact_list", "matter_list", "work_list", "notice_list"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)


class GatedPageTests(TestCase):
    def test_each_role_reaches_exactly_what_it_grants(self):
        people = {name: member(name) for name in PRESETS}
        people["Superuser"] = make_user()
        for method, url, permission in GATED:
            for name, person in people.items():
                held = name == "Superuser" or permission in PRESETS[name]
                self.client.force_login(person)
                status = getattr(self.client, method)(url).status_code
                if permission == roles.MANAGE_BILLING and roles.BILLING not in PRESETS.get(
                    name, EVERYTHING
                ):
                    # Billing pages send people without billing to sign in, as before.
                    self.assertEqual(status, 302, (name, url))
                elif held:
                    self.assertNotEqual(status, 403, (name, url))
                else:
                    self.assertEqual(status, 403, (name, url))

    def test_a_refusal_explains_itself_and_signed_out_people_sign_in(self):
        self.client.force_login(member("Paralegal"))
        response = self.client.get(reverse("firm_settings"))
        self.assertContains(response, "This isn't open to you", status_code=403)
        self.assertContains(response, "ask whoever manages staff", status_code=403)
        self.client.logout()
        response = self.client.get(reverse("firm_settings"))
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('firm_settings')}")

    def test_a_role_can_delete_what_it_may(self):
        owner = member("Attorney")
        matter = make_matter(make_contact(owner), owner)
        party = MatterParty.objects.create(
            matter=matter, contact=make_contact(first_name="Ola"), role="Co-sponsor"
        )
        entry = TimeEntry.objects.create(
            user=owner,
            matter=matter,
            started_at=timezone.now() - timedelta(hours=1),
            stopped_at=timezone.now(),
        )
        self.client.force_login(member("Paralegal"))
        page = self.client.get(reverse("matter_detail", args=[matter.pk]))
        self.assertNotContains(page, reverse("matter_remove_party", args=[party.pk]))
        self.assertNotContains(page, reverse("time_entry_delete", args=[entry.pk]))

        self.client.force_login(owner)
        page = self.client.get(reverse("matter_detail", args=[matter.pk]))
        self.assertContains(page, reverse("matter_remove_party", args=[party.pk]))
        self.assertContains(page, reverse("time_entry_delete", args=[entry.pk]))
        self.client.post(reverse("matter_remove_party", args=[party.pk]))
        self.client.post(reverse("time_entry_delete", args=[entry.pk]))
        self.assertFalse(MatterParty.objects.exists())
        self.assertFalse(TimeEntry.objects.exists())


class NavigationTests(TestCase):
    def settings_links(self, person):
        self.client.force_login(person)
        page = self.client.get(reverse("dashboard")).content.decode()
        names = {
            "Workflows": reverse("workflow_template_list"),
            "Custom fields": reverse("custom_field_list"),
            "Import": reverse("import_list"),
            "Firm settings": reverse("firm_settings"),
            "Administration": reverse("admin:index"),
            "Billing": reverse("invoice_list"),
        }
        return {name for name, url in names.items() if f'href="{url}"' in page}

    def test_the_sidebar_shows_what_each_role_can_open(self):
        self.assertEqual(self.settings_links(member("Paralegal")), set())
        self.assertEqual(self.settings_links(member("Attorney")), {"Billing"})
        self.assertEqual(
            self.settings_links(member("Office administrator")),
            {"Workflows", "Custom fields", "Import", "Firm settings", "Billing"},
        )
        self.assertEqual(
            self.settings_links(member("Managing partner")),
            {"Workflows", "Custom fields", "Import", "Firm settings", "Billing"},
        )
        self.assertEqual(
            self.settings_links(make_user()),
            {"Workflows", "Custom fields", "Import", "Firm settings", "Billing", "Administration"},
        )

    def test_settings_pages_say_who_can_turn_things_on(self):
        self.client.force_login(member("Paralegal"))
        page = self.client.get(reverse("ai_settings"))
        self.assertContains(page, "Someone who manages connections and AI can turn them on.")
        self.assertNotContains(page, "superuser")
        self.client.force_login(member("Managing partner"))
        page = self.client.get(reverse("ai_settings"))
        self.assertContains(page, "Turn them on below.")


class ConnectionVisibilityTests(TestCase):
    def test_who_manages_connections_sees_and_manages_everyone_s(self):
        owner = make_user("owner", superuser=False)
        personal = Connection.objects.create(
            connector_key="example", label="Personal example", owner=owner, created_by=owner
        )
        partner = member("Managing partner")
        office = member("Office administrator")
        self.assertIn(personal, visible_connections(partner))
        self.assertTrue(can_manage(partner, personal))
        self.assertNotIn(personal, visible_connections(office))
        self.assertFalse(can_manage(office, personal))
        self.assertTrue(can_manage(owner, personal))
