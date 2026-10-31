from django import template

from ..forms import SendForm

register = template.Library()


@register.inclusion_tag("signatures/includes/signature_panel.html", takes_context=True)
def signature_panel(context, matter):
    """Requests sent for signature, and a form to send a matter PDF, for a matter page."""
    form = SendForm(matter=matter, user=context["user"], auto_id="id_signature_%s")
    return {
        "request": context["request"],
        "matter": matter,
        "requests": matter.signature_requests.select_related("signed_document", "sent_by"),
        "form": form,
        "can_send": bool(form.connections),
    }
