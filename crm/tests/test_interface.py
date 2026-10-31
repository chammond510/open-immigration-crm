from datetime import datetime, timedelta
from unittest import mock

from django import forms
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.forms import ContactForm, SectionedFormMixin, TwoFactorCodeForm
from crm.models import AuditLog, Matter, WorkItem
from crm.views import work_groups

from .factories import make_contact, make_matter, make_user


class NavigationTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_only_the_current_section_is_marked(self):
        response = self.client.get(reverse("intake_workspace"))
        self.assertEqual(response.context["nav_section"], "intake")
        self.assertContains(response, '<a href="/intake/" aria-current="page">Intake</a>')
        self.assertNotContains(response, '<a href="/work/" aria-current="page">')

    def test_overview_and_pages_outside_the_navigation(self):
        self.assertEqual(self.client.get(reverse("dashboard")).context["nav_section"], "overview")
        self.assertEqual(self.client.get(reverse("search")).context["nav_section"], "")

    def test_topbar_search_shows_only_its_own_query(self):
        on_search = self.client.get(reverse("search"), {"q": "Amina"})
        self.assertContains(on_search, 'id="global-q" name="q" type="search" value="Amina"')
        on_contacts = self.client.get(reverse("contact_list"), {"q": "Amina"})
        self.assertContains(on_contacts, 'id="global-q" name="q" type="search" value=""')


class SearchTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.ana = make_contact(first_name="Ana", last_name="Muestra")
        self.matter = make_matter(self.ana, title="Adjustment of status")
        make_contact(first_name="Ana", last_name="Ejemplo")

    def test_full_name_finds_the_contact(self):
        response = self.client.get(reverse("contact_list"), {"q": "Ana Muestra"})
        self.assertEqual(list(response.context["contacts"]), [self.ana])
        response = self.client.get(reverse("contact_list"), {"q": "Ana Nobody"})
        self.assertEqual(list(response.context["contacts"]), [])

    def test_matter_words_can_mix_client_and_title(self):
        response = self.client.get(reverse("matter_list"), {"q": "Muestra adjustment"})
        self.assertEqual(list(response.context["matters"]), [self.matter])

    def test_global_search_matches_full_names(self):
        response = self.client.get(reverse("search"), {"q": "ana muestra"})
        self.assertEqual(list(response.context["contacts"]), [self.ana])
        self.assertEqual(list(response.context["matters"]), [self.matter])

    def test_matter_list_filters_by_case_type(self):
        make_matter(self.ana, title="Naturalization", case_type=Matter.CaseType.NATURALIZATION)
        response = self.client.get(reverse("matter_list"), {"type": "naturalization"})
        self.assertEqual([m.title for m in response.context["matters"]], ["Naturalization"])
        self.assertContains(response, "1 matter")


class WorkTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.contact = make_contact()

    def test_groups_follow_the_due_date(self):
        noon = timezone.make_aware(datetime(2026, 10, 14, 12, 0))

        def item(title, **fields):
            return WorkItem(title=title, contact=self.contact, **fields)

        items = [
            item("Late", scheduled_for=noon - timedelta(hours=1)),
            item("This afternoon", scheduled_for=noon + timedelta(hours=1)),
            item("This week", scheduled_for=noon + timedelta(days=3)),
            item("Next month", scheduled_for=noon + timedelta(days=30)),
            item("Someday"),
            item("Finished", is_completed=True),
        ]
        with mock.patch("django.utils.timezone.now", return_value=noon):
            groups = work_groups(items)
        self.assertEqual(
            [(label, [i.title for i in group]) for label, group in groups],
            [
                ("Overdue", ["Late"]),
                ("Today", ["This afternoon"]),
                ("Next 7 days", ["This week"]),
                ("Later", ["Next month"]),
                ("Not scheduled", ["Someday"]),
                ("Done", ["Finished"]),
            ],
        )

    def test_most_pressing_priority_comes_first(self):
        for title, priority in (("Low", "low"), ("Urgent", "urgent"), ("High", "high")):
            WorkItem.objects.create(title=title, priority=priority, contact=self.contact)
        response = self.client.get(reverse("work_list"))
        self.assertEqual(
            [i.title for i in response.context["work_items"]], ["Urgent", "High", "Low"]
        )

    def test_edit_a_work_item(self):
        item = WorkItem.objects.create(title="Draft letter", contact=self.contact)
        page = self.client.get(reverse("work_update", args=[item.pk]))
        self.assertContains(page, "Edit work item")
        self.assertContains(page, f'href="{reverse("work_list")}">Cancel</a>')
        response = self.client.post(
            reverse("work_update", args=[item.pk]),
            {
                "title": "Draft cover letter",
                "kind": "deadline",
                "priority": "high",
                "contact": self.contact.pk,
                "assigned_to": self.user.pk,
            },
        )
        self.assertRedirects(response, reverse("work_list"))
        item.refresh_from_db()
        self.assertEqual((item.title, item.kind), ("Draft cover letter", "deadline"))
        self.assertTrue(AuditLog.objects.filter(action="work_item.updated").exists())

    def test_open_work_panels_leave_out_finished_items(self):
        matter = make_matter(self.contact)
        WorkItem.objects.create(
            title="Done already", contact=self.contact, matter=matter, is_completed=True
        )
        WorkItem.objects.create(title="Still open", contact=self.contact, matter=matter)
        for url in (
            reverse("matter_detail", args=[matter.pk]),
            reverse("contact_detail", args=[self.contact.pk]),
        ):
            titles = [i.title for i in self.client.get(url).context["work_items"]]
            self.assertIn("Still open", titles)
            self.assertNotIn("Done already", titles)


class FormPageTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.user.first_name, self.user.last_name = "Rosa", "Sample"
        self.user.save()
        self.client.force_login(self.user)

    def test_cancel_returns_to_the_record(self):
        contact = make_contact()
        response = self.client.get(reverse("contact_update", args=[contact.pk]))
        self.assertContains(
            response, f'href="{reverse("contact_detail", args=[contact.pk])}">Cancel</a>'
        )
        self.assertContains(response, "<legend>Address</legend>")

    def test_staff_are_offered_by_name(self):
        choices = dict(ContactForm().fields["assigned_to"].choices)
        self.assertIn("Rosa Sample", choices.values())
        self.assertEqual(choices[""], "Unassigned")

    def test_fields_outside_the_sections_come_last(self):
        class Example(SectionedFormMixin, forms.Form):
            sections = (("Main", ("name",)),)
            name = forms.CharField()
            extra = forms.CharField()

        sections = Example().field_sections()
        self.assertEqual(
            [(title, [field.name for field in fields]) for title, fields in sections],
            [("Main", ["name"]), ("More details", ["extra"])],
        )

    def test_second_code_form_does_not_take_focus(self):
        self.assertIn("autofocus", TwoFactorCodeForm().fields["code"].widget.attrs)
        regenerate = TwoFactorCodeForm(prefix="regenerate")
        self.assertNotIn("autofocus", regenerate.fields["code"].widget.attrs)
