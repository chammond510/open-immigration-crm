from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.models import Activity, AuditLog, ChecklistItem, WorkItem
from crm.tests.factories import make_contact, make_matter, make_user
from crm.workflows.models import MatterPhase, MatterWorkflow, WorkflowTemplate
from crm.workflows.services import (
    apply_workflow,
    finish_phase,
    outline_text,
    parse_outline,
    remove_workflow,
    save_template,
    templates_for,
)

OUTLINE = """
Gather documents (due in 14 days)
- First fictional item | A note for staff
- Second fictional item

Prepare
- Third fictional item
Finish (due in 1 day)
"""


def make_template(user, outline=OUTLINE, **fields):
    template = WorkflowTemplate(name=fields.pop("name", "Fictional workflow"), **fields)
    return save_template(template, parse_outline(outline), user)


class OutlineTests(TestCase):
    def test_parse_and_write_back(self):
        phases = parse_outline(OUTLINE)
        self.assertEqual(
            phases,
            [
                (
                    "Gather documents",
                    14,
                    [("First fictional item", "A note for staff"), ("Second fictional item", "")],
                ),
                ("Prepare", None, [("Third fictional item", "")]),
                ("Finish", 1, []),
            ],
        )
        template = make_template(make_user())
        self.assertEqual(parse_outline(outline_text(template)), phases)
        self.assertIn("Finish (due in 1 day)", outline_text(template))

    def test_outline_errors(self):
        for text, message in (
            ("", "Add at least one phase."),
            ("- An item first", "Line 1: name a phase before its first item."),
            ("Phase\n-   | only a note", "Line 2: an item needs a title"),
            ("x" * 201, "a phase name needs 1 to 200 characters"),
            ("\n".join(f"Phase {n}" for n in range(31)), "at most 30 phases"),
        ):
            with self.assertRaisesMessage(ValidationError, message):
                parse_outline(text)


class WorkflowServiceTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.contact = make_contact(self.user)
        self.matter = make_matter(self.contact, self.user)
        self.template = make_template(self.user)

    def test_saving_replaces_phases(self):
        save_template(self.template, parse_outline("Only phase\n- Only item"), self.user)
        self.assertEqual([p.name for p in self.template.phases.all()], ["Only phase"])
        self.assertEqual(
            list(AuditLog.objects.values_list("action", flat=True).order_by("pk")),
            ["workflow_template.created", "workflow_template.updated"],
        )

    def test_apply_copies_phases_and_items_and_starts_the_first(self):
        ChecklistItem.objects.create(matter=self.matter, title="Earlier item", position=4)
        with mock.patch("crm.workflows.services.emit") as emit:
            workflow = apply_workflow(self.matter, self.template, self.user)
        phases = list(workflow.phases.all())
        self.assertEqual([p.status for p in phases], ["in_progress", "not_started", "not_started"])
        first = phases[0]
        self.assertEqual(
            list(first.items.values_list("title", "position")),
            [("First fictional item", 5), ("Second fictional item", 6)],
        )
        self.assertEqual(first.items.first().description, "A note for staff")
        due = first.due_item
        self.assertEqual(
            (due.kind, due.matter, due.contact), ("deadline", self.matter, self.contact)
        )
        self.assertEqual(
            timezone.localtime(due.scheduled_for).date(), timezone.localdate() + timedelta(days=14)
        )
        self.assertTrue(Activity.objects.filter(subject="Workflow Fictional workflow applied"))
        emit.assert_called_once_with(
            "matter.phase_started",
            mock.ANY,
            changes={"phase": {"from": None, "to": "Gather documents"}},
        )
        with self.assertRaisesMessage(ValidationError, "already has a workflow"):
            apply_workflow(self.matter, self.template, self.user)
        empty = WorkflowTemplate.objects.create(name="Empty")
        other = make_matter(self.contact, title="Second fictional matter")
        with self.assertRaisesMessage(ValidationError, "has no phases"):
            apply_workflow(other, empty, self.user)

    def test_finishing_and_skipping_phases(self):
        workflow = apply_workflow(self.matter, self.template, self.user)
        first, second, third = workflow.phases.all()
        with self.assertRaisesMessage(ValidationError, "Only the phase in progress"):
            finish_phase(second, self.user)
        following = finish_phase(first, self.user)
        self.assertEqual(following, second)
        first.refresh_from_db()
        self.assertEqual(first.status, "completed")
        self.assertTrue(first.due_item.is_completed)
        self.assertIsNone(MatterPhase.objects.get(pk=second.pk).due_item)
        self.assertTrue(
            Activity.objects.filter(
                subject="Phase Gather documents completed; Prepare started", kind="status"
            ).exists()
        )
        finish_phase(second, self.user, skipped=True)
        third.refresh_from_db()
        self.assertEqual(third.status, "in_progress")
        self.assertIsNotNone(third.due_item)
        self.assertIsNone(finish_phase(third, self.user))
        workflow.refresh_from_db()
        self.assertIsNotNone(workflow.completed_at)
        self.assertEqual(MatterPhase.objects.get(pk=second.pk).status, "skipped")
        self.assertTrue(
            Activity.objects.filter(subject__endswith="workflow Fictional workflow finished")
        )

    def test_removing_keeps_items_and_finished_work(self):
        workflow = apply_workflow(self.matter, self.template, self.user)
        first = workflow.phases.first()
        finish_phase(first, self.user)
        remove_workflow(self.matter, self.user)
        self.assertFalse(MatterWorkflow.objects.exists())
        self.assertEqual(self.matter.checklist.filter(phase__isnull=True).count(), 3)
        self.assertEqual(list(WorkItem.objects.values_list("is_completed", flat=True)), [True])
        self.assertTrue(Activity.objects.filter(subject="Workflow Fictional workflow removed"))
        remove_workflow(self.matter, self.user)

    def test_new_matters_start_their_case_type_workflow(self):
        automatic = make_template(
            self.user, name="Automatic", case_type="family", apply_to_new_matters=True
        )
        family = make_matter(self.contact, case_type="family")
        self.assertEqual(family.workflow.template, automatic)
        self.assertIsNone(family.workflow.applied_by)
        other = make_matter(self.contact, case_type="humanitarian")
        self.assertFalse(MatterWorkflow.objects.filter(matter=other).exists())
        self.assertEqual(templates_for(family), [automatic, self.template])

    def test_automatic_workflow_rules(self):
        template = WorkflowTemplate(name="No type", apply_to_new_matters=True)
        with self.assertRaisesMessage(ValidationError, "Choose a case type"):
            template.full_clean()
        make_template(self.user, name="First", case_type="family", apply_to_new_matters=True)
        second = WorkflowTemplate(name="Second", case_type="family", apply_to_new_matters=True)
        with self.assertRaisesMessage(ValidationError, "First already applies to new family"):
            second.full_clean()


class WorkflowPageTests(TestCase):
    def setUp(self):
        self.admin = make_user()
        self.staff = make_user("paralegal", superuser=False)
        self.contact = make_contact(self.admin)
        self.matter = make_matter(self.contact, self.admin)

    def test_superusers_write_templates(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("workflow_template_list")), "No workflows yet")
        response = self.client.post(
            reverse("workflow_template_create"),
            {"name": "Fictional", "case_type": "", "outline": OUTLINE, "is_active": "on"},
        )
        self.assertRedirects(response, reverse("workflow_template_list"))
        template = WorkflowTemplate.objects.get()
        self.assertEqual(template.created_by, self.admin)
        listing = self.client.get(reverse("workflow_template_list"))
        self.assertContains(listing, "Fictional")
        edit = self.client.get(reverse("workflow_template_update", args=[template.pk]))
        self.assertContains(edit, "Gather documents (due in 14 days)")
        bad = self.client.post(
            reverse("workflow_template_update", args=[template.pk]),
            {"name": "Fictional", "outline": "- item first"},
        )
        self.assertContains(bad, "name a phase before its first item")

        self.client.force_login(self.staff)
        response = self.client.get(reverse("workflow_template_list"))
        self.assertEqual(response.status_code, 302)

    def test_staff_apply_and_advance_from_the_matter_page(self):
        template = make_template(self.admin)
        self.client.force_login(self.staff)
        page = self.client.get(reverse("matter_detail", args=[self.matter.pk]))
        self.assertContains(page, "Apply a workflow")
        self.assertNotContains(page, "Write your firm&#x27;s first workflow")
        response = self.client.post(
            reverse("workflow_apply", args=[self.matter.pk]), {"template": template.pk}
        )
        self.assertRedirects(
            response,
            reverse("matter_detail", args=[self.matter.pk]) + "#checklist",
            fetch_redirect_response=False,
        )
        page = self.client.get(reverse("matter_detail", args=[self.matter.pk]))
        self.assertContains(page, "Workflow: Fictional workflow")
        self.assertContains(page, "Phase 1 of 3: Gather documents")
        self.assertContains(page, "0 of 3 items done")
        self.assertNotContains(page, "Remove workflow")

        self.client.post(
            reverse("checklist_add", args=[self.matter.pk]), {"title": "Ad hoc", "position": "9"}
        )
        first = MatterPhase.objects.get(status="in_progress")
        self.assertEqual(ChecklistItem.objects.get(title="Ad hoc").phase, first)

        response = self.client.post(reverse("workflow_phase_finish", args=[first.pk]), follow=True)
        self.assertContains(response, "Gather documents completed. Prepare has started.")
        response = self.client.post(reverse("workflow_phase_finish", args=[first.pk]), follow=True)
        self.assertContains(response, "Only the phase in progress")
        response = self.client.post(
            reverse("workflow_apply", args=[self.matter.pk]), {"template": template.pk}, follow=True
        )
        self.assertContains(response, "This matter already has a workflow.")
        self.assertEqual(
            self.client.post(reverse("workflow_remove", args=[self.matter.pk])).status_code, 302
        )
        self.assertTrue(MatterWorkflow.objects.exists())

        self.client.force_login(self.admin)
        response = self.client.post(reverse("workflow_remove", args=[self.matter.pk]), follow=True)
        self.assertContains(response, "Workflow removed.")
        self.assertContains(response, "Apply a workflow")

    def test_the_api_reports_the_current_phase(self):
        from crm.api.models import ApiSettings, ApiToken

        ApiSettings.objects.update_or_create(pk=1, defaults={"enabled": True})
        _, raw_token = ApiToken.issue(
            user=self.admin, name="Fictional", scopes=["matters:read"], lifetime_days=30
        )
        apply_workflow(self.matter, make_template(self.admin), self.admin)
        body = self.client.get(
            f"/api/v1/matters/{self.matter.pk}/", headers={"Authorization": f"Bearer {raw_token}"}
        ).json()
        self.assertEqual(
            body["workflow"],
            {
                "name": "Fictional workflow",
                "current_phase": "Gather documents",
                "completed_at": None,
            },
        )
