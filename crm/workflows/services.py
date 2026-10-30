"""Writing workflow templates as outlines, applying them to matters, and moving
a matter from phase to phase."""

import re
from datetime import datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from crm.events.services import emit
from crm.models import Activity, AuditLog, ChecklistItem, WorkItem

from .models import ItemTemplate, MatterPhase, MatterWorkflow, PhaseTemplate, WorkflowTemplate

MAX_PHASES = 30
MAX_ITEMS = 300
# Lines are stripped first, so the pattern needs no surrounding whitespace, which
# also keeps matching linear on long lines of spaces.
DUE_PATTERN = re.compile(r"\(due in (\d{1,3}) days?\)$", re.IGNORECASE)


def parse_outline(text):
    """Read a template outline into [(phase name, due days, [(title, description)])].

    A line is a phase name, optionally ending "(due in 30 days)". A line that
    starts with "-" is a checklist item of the phase above it; text after "|"
    is the item's description.
    """
    phases = []
    for number, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("-"):
            if not phases:
                raise ValidationError(f"Line {number}: name a phase before its first item.")
            title, _, description = line[1:].partition("|")
            title, description = title.strip(), description.strip()
            if not title:
                raise ValidationError(f"Line {number}: an item needs a title after the dash.")
            if len(title) > 255:
                raise ValidationError(f"Line {number}: keep item titles under 255 characters.")
            phases[-1][2].append((title, description))
            continue
        due = DUE_PATTERN.search(line)
        name = line[: due.start()].strip() if due else line
        if not name or len(name) > 200:
            raise ValidationError(f"Line {number}: a phase name needs 1 to 200 characters.")
        phases.append((name, int(due.group(1)) if due else None, []))
    if not phases:
        raise ValidationError("Add at least one phase.")
    if len(phases) > MAX_PHASES:
        raise ValidationError(f"A workflow can have at most {MAX_PHASES} phases.")
    if sum(len(items) for _, _, items in phases) > MAX_ITEMS:
        raise ValidationError(f"A workflow can have at most {MAX_ITEMS} items.")
    return phases


def outline_text(template):
    lines = []
    for phase in template.phases.prefetch_related("items"):
        due = f" (due in {phase.due_days} day{'s' if phase.due_days != 1 else ''})"
        lines.append(phase.name + (due if phase.due_days else ""))
        for item in phase.items.all():
            lines.append(f"- {item.title}" + (f" | {item.description}" if item.description else ""))
        lines.append("")
    return "\n".join(lines).strip()


def audit(actor, action, target, detail=""):
    AuditLog.objects.create(
        user=actor,
        action=action,
        target_type=target.__class__.__name__,
        target_id=str(target.pk),
        detail=detail[:500],
    )


@transaction.atomic
def save_template(template, outline, actor):
    """Save a template and replace its phases and items with the outline's."""
    is_new = template._state.adding
    if is_new:
        template.created_by = actor
    template.save()
    template.phases.all().delete()
    for phase_position, (name, due_days, items) in enumerate(outline):
        phase = PhaseTemplate.objects.create(
            template=template, name=name, position=phase_position, due_days=due_days
        )
        ItemTemplate.objects.bulk_create(
            ItemTemplate(phase=phase, title=title, description=description, position=position)
            for position, (title, description) in enumerate(items)
        )
    audit(actor, "workflow_template.created" if is_new else "workflow_template.updated", template)
    return template


def due_time(day):
    return timezone.make_aware(datetime.combine(day, time(17)))


@transaction.atomic
def apply_workflow(matter, template, actor=None):
    """Copy a template's phases and items onto a matter and start the first phase."""
    if MatterWorkflow.objects.filter(matter=matter).exists():
        raise ValidationError("This matter already has a workflow.")
    phase_templates = list(template.phases.prefetch_related("items"))
    if not phase_templates:
        raise ValidationError("This workflow template has no phases.")
    workflow = MatterWorkflow.objects.create(
        matter=matter, template=template, name=template.name, applied_by=actor
    )
    position = (
        matter.checklist.order_by("-position").values_list("position", flat=True).first()
    ) or 0
    for phase_template in phase_templates:
        phase = MatterPhase.objects.create(
            workflow=workflow,
            name=phase_template.name,
            position=phase_template.position,
            due_days=phase_template.due_days,
        )
        for item in phase_template.items.all():
            position += 1
            ChecklistItem.objects.create(
                matter=matter,
                phase=phase,
                title=item.title,
                description=item.description,
                position=position,
            )
    Activity.objects.create(
        matter=matter,
        contact=matter.primary_contact,
        kind=Activity.Kind.SYSTEM,
        subject=f"Workflow {template.name} applied",
        created_by=actor,
    )
    audit(actor, "workflow.applied", workflow, template.name)
    start_phase(workflow.phases.first(), actor, previous=None)
    return workflow


def start_phase(phase, actor=None, *, previous):
    phase.status = MatterPhase.Status.IN_PROGRESS
    phase.started_at = timezone.now()
    matter = phase.workflow.matter
    if phase.due_days:
        day = timezone.localdate() + timedelta(days=phase.due_days)
        phase.due_item = WorkItem.objects.create(
            kind=WorkItem.Kind.DEADLINE,
            title=f"Finish {phase.name} · {matter.matter_number}",
            description="Created by the matter's workflow. Completing the phase completes it.",
            matter=matter,
            contact=matter.primary_contact,
            assigned_to=matter.assigned_to,
            scheduled_for=due_time(day),
        )
    phase.save(update_fields=["status", "started_at", "due_item"])
    emit(
        "matter.phase_started",
        matter,
        changes={"phase": {"from": previous.name if previous else None, "to": phase.name}},
    )


def current_phase(workflow):
    return workflow.phases.filter(status=MatterPhase.Status.IN_PROGRESS).first()


@transaction.atomic
def finish_phase(phase, actor, *, skipped=False):
    """Complete or skip the current phase and start the next one."""
    phase = (
        MatterPhase.objects.select_for_update().select_related("workflow__matter").get(pk=phase.pk)
    )
    if phase.status != MatterPhase.Status.IN_PROGRESS:
        raise ValidationError("Only the phase in progress can be completed or skipped.")
    now = timezone.now()
    phase.status = MatterPhase.Status.SKIPPED if skipped else MatterPhase.Status.COMPLETED
    phase.completed_at = now
    phase.save(update_fields=["status", "completed_at"])
    if phase.due_item_id and not phase.due_item.is_completed:
        phase.due_item.is_completed = True
        phase.due_item.save(update_fields=["is_completed", "completed_at", "updated_at"])
    workflow = phase.workflow
    matter = workflow.matter
    following = workflow.phases.filter(
        position__gt=phase.position, status=MatterPhase.Status.NOT_STARTED
    ).first()
    verb = "skipped" if skipped else "completed"
    subject = f"Phase {phase.name} {verb}"
    if following:
        start_phase(following, actor, previous=phase)
        subject += f"; {following.name} started"
    else:
        workflow.completed_at = now
        workflow.save(update_fields=["completed_at"])
        subject += f"; workflow {workflow.name} finished"
    Activity.objects.create(
        matter=matter,
        contact=matter.primary_contact,
        kind=Activity.Kind.STATUS,
        subject=subject,
        created_by=actor,
    )
    audit(actor, f"workflow.phase_{verb}", phase, phase.name)
    return following


@transaction.atomic
def remove_workflow(matter, actor):
    """Remove the workflow; checklist items and completed work stay on the matter."""
    workflow = MatterWorkflow.objects.filter(matter=matter).first()
    if workflow is None:
        return
    WorkItem.objects.filter(matter_phase__workflow=workflow, is_completed=False).delete()
    name = workflow.name
    audit(actor, "workflow.removed", workflow, name)
    workflow.delete()
    Activity.objects.create(
        matter=matter,
        contact=matter.primary_contact,
        kind=Activity.Kind.SYSTEM,
        subject=f"Workflow {name} removed",
        created_by=actor,
    )


def templates_for(matter):
    """Active templates for the matter's case type first, then those for any matter."""
    templates = WorkflowTemplate.objects.filter(is_active=True).exclude(phases=None).distinct()
    matching = [t for t in templates if t.case_type == matter.case_type]
    general = [t for t in templates if not t.case_type]
    return matching + general


def apply_automatic_workflow(matter):
    template = WorkflowTemplate.objects.filter(
        is_active=True, apply_to_new_matters=True, case_type=matter.case_type
    ).first()
    if template and template.phases.exists():
        apply_workflow(matter, template)
