from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from crm.roles import MANAGE_FIRM_SETTINGS, permission_required
from crm.services import audit

from .forms import CustomFieldForm
from .models import CustomField


@permission_required(MANAGE_FIRM_SETTINGS)
@require_GET
def field_list(request):
    fields = CustomField.objects.all()
    groups = [
        {"key": key, "label": label, "fields": [f for f in fields if f.applies_to == key]}
        for key, label in CustomField.AppliesTo.choices
    ]
    return render(request, "customfields/field_list.html", {"groups": groups})


@permission_required(MANAGE_FIRM_SETTINGS)
@require_http_methods(["GET", "POST"])
def field_edit(request, pk=None):
    field = get_object_or_404(CustomField, pk=pk) if pk else CustomField()
    initial = {}
    if not pk and request.GET.get("for") in CustomField.AppliesTo.values:
        initial["applies_to"] = request.GET["for"]
    form = CustomFieldForm(request.POST or None, instance=field, initial=initial)
    if request.method == "POST" and form.is_valid():
        field = form.save()
        audit(request, "custom_field.updated" if pk else "custom_field.created", field, field.key)
        messages.success(request, f"{field.label} saved.")
        return redirect("custom_field_list")
    return render(
        request, "customfields/field_form.html", {"form": form, "field": field if pk else None}
    )
