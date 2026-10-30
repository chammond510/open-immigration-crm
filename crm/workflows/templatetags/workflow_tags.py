from django import template

from crm.forms import ChecklistItemForm

from ..forms import ApplyWorkflowForm
from ..models import MatterWorkflow
from ..services import templates_for

register = template.Library()


@register.inclusion_tag("workflows/includes/workflow_panel.html", takes_context=True)
def workflow_panel(context, matter):
    """The matter's checklist, grouped by workflow phase when a workflow is applied."""
    items = list(matter.checklist.select_related("completed_by"))
    workflow = (
        MatterWorkflow.objects.filter(matter=matter).prefetch_related("phases__due_item").first()
    )
    phases = []
    if workflow:
        for phase in workflow.phases.all():
            phase.checklist = [item for item in items if item.phase_id == phase.pk]
            phases.append(phase)
    templates = [] if workflow else templates_for(matter)
    current = next((p for p in phases if p.status == "in_progress"), None)
    return {
        "matter": matter,
        "user": context["request"].user,
        "csrf_token": context.get("csrf_token"),
        "workflow": workflow,
        "phases": phases,
        "current": current,
        "current_number": phases.index(current) + 1 if current else None,
        "loose_items": [item for item in items if not item.phase_id] if workflow else items,
        "done_count": sum(item.is_completed for item in items),
        "item_count": len(items),
        "apply_form": ApplyWorkflowForm(templates=templates) if templates else None,
        "checklist_form": ChecklistItemForm(),
    }
