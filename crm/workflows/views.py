from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from crm.models import Matter
from crm.roles import DELETE_RECORDS, MANAGE_FIRM_SETTINGS, permission_required

from .forms import ApplyWorkflowForm, WorkflowTemplateForm
from .models import MatterPhase, WorkflowTemplate
from .services import (
    apply_workflow,
    finish_phase,
    remove_workflow,
    save_template,
    templates_for,
)

staff_required = user_passes_test(lambda user: user.is_active and user.is_staff, login_url="login")


def matter_checklist_url(matter):
    return reverse("matter_detail", args=[matter.pk]) + "#checklist"


@permission_required(MANAGE_FIRM_SETTINGS)
@require_GET
def template_list(request):
    templates = WorkflowTemplate.objects.annotate(
        phase_count=Count("phases", distinct=True),
        item_count=Count("phases__items", distinct=True),
        matter_count=Count("matter_workflows", distinct=True),
    )
    return render(request, "workflows/template_list.html", {"templates": templates})


@permission_required(MANAGE_FIRM_SETTINGS)
@require_http_methods(["GET", "POST"])
def template_edit(request, pk=None):
    template = get_object_or_404(WorkflowTemplate, pk=pk) if pk else WorkflowTemplate()
    form = WorkflowTemplateForm(request.POST or None, instance=template)
    if request.method == "POST" and form.is_valid():
        save_template(form.save(commit=False), form.cleaned_data["outline"], request.user)
        messages.success(
            request,
            "Workflow template saved. Matters already using it keep the version they started with.",
        )
        return redirect("workflow_template_list")
    return render(
        request,
        "workflows/template_form.html",
        {"form": form, "template": template if pk else None},
    )


@staff_required
@require_POST
def workflow_apply(request, matter_pk):
    matter = get_object_or_404(Matter, pk=matter_pk)
    form = ApplyWorkflowForm(request.POST, templates=templates_for(matter))
    if form.is_valid():
        try:
            workflow = apply_workflow(matter, form.cleaned_data["template"], request.user)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{workflow.name} applied.")
    else:
        messages.error(request, "Choose a workflow to apply.")
    return redirect(matter_checklist_url(matter))


@staff_required
@require_POST
def phase_finish(request, pk):
    phase = get_object_or_404(MatterPhase.objects.select_related("workflow__matter"), pk=pk)
    skipped = request.POST.get("skip") == "1"
    try:
        following = finish_phase(phase, request.user, skipped=skipped)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        done = "skipped" if skipped else "completed"
        messages.success(
            request,
            f"{phase.name} {done}. {following.name} has started."
            if following
            else f"{phase.name} {done}. The workflow is finished.",
        )
    return redirect(matter_checklist_url(phase.workflow.matter))


@permission_required(DELETE_RECORDS)
@require_POST
def workflow_remove(request, matter_pk):
    matter = get_object_or_404(Matter, pk=matter_pk)
    remove_workflow(matter, request.user)
    messages.success(request, "Workflow removed. Its checklist items stay on the matter.")
    return redirect(matter_checklist_url(matter))
