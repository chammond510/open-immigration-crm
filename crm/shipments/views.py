from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from crm.models import Matter
from crm.services import audit
from crm.views import staff_required

from .forms import NewShipmentForm, ShipmentForm
from .models import Shipment


def matter_shipments(matter):
    return reverse("matter_detail", args=[matter.pk]) + "#shipments"


@staff_required
@require_POST
def shipment_create(request, pk):
    matter = get_object_or_404(Matter, pk=pk)
    form = NewShipmentForm(request.POST)
    if form.is_valid():
        shipment = form.save(commit=False)
        shipment.matter, shipment.created_by = matter, request.user
        shipment.save()
        audit(request, "shipment.created", shipment)
        messages.success(request, "Shipment added.")
    else:
        messages.error(request, "Fill in what was sent and when.")
    return redirect(matter_shipments(matter))


@staff_required
@require_http_methods(["GET", "POST"])
def shipment_update(request, pk):
    shipment = get_object_or_404(Shipment.objects.select_related("matter"), pk=pk)
    form = ShipmentForm(request.POST or None, instance=shipment)
    if form.is_valid():
        form.save()
        audit(request, "shipment.updated", shipment)
        messages.success(request, "Shipment saved.")
        return redirect(matter_shipments(shipment.matter))
    return render(
        request,
        "crm/form_page.html",
        {"form": form, "title": f"Shipment: {shipment}", "submit_label": "Save shipment"},
    )


@staff_required
@require_POST
def shipment_delete(request, pk):
    shipment = get_object_or_404(Shipment.objects.select_related("matter"), pk=pk)
    audit(request, "shipment.deleted", shipment, detail=str(shipment))
    shipment.delete()
    messages.success(request, "Shipment deleted.")
    return redirect(matter_shipments(shipment.matter))
