from django import template

from ..forms import NewShipmentForm

register = template.Library()


@register.inclusion_tag("shipments/includes/shipments_panel.html", takes_context=True)
def shipments_panel(context, matter):
    """A matter's shipments, where each stands, and a form to add one."""
    return {
        "request": context["request"],
        "user": context["user"],
        "matter": matter,
        "shipments": matter.shipments.all(),
        "form": NewShipmentForm(auto_id="id_shipment_%s"),
    }
