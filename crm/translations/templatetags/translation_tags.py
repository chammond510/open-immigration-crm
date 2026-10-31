from django import template

from crm.comms.sharing import withheld_connections, withheld_reason

from ..forms import TranslateForm
from ..services import language_name

register = template.Library()


def form_for(context):
    """One form per page: the translators a staff member may use are looked up once."""
    request = context.get("request")
    if request is None or not request.user.is_authenticated:
        return None
    if not hasattr(request, "_translate_form"):
        # Many records share the page, so the form's fields carry no ids.
        form = TranslateForm(user=request.user, auto_id=False)
        request._translate_form = form if form.options else None
    return request._translate_form


@register.inclusion_tag("translations/includes/translations.html", takes_context=True)
def translations(context, record, kind):
    """A record's machine translations, and the form to ask for one."""
    request = context.get("request")
    labels = {
        key: answer.get("label", key)
        for key, answer in (getattr(record, "answers", None) or {}).items()
        if isinstance(answer, dict)
    }
    return {
        "record": record,
        "kind": kind,
        "found": [
            (found, [(labels.get(key, key), text) for key, text in found.content.items()])
            for found in record.translations.all()
        ],
        "form": (form := form_for(context)),
        # A form needs a signed-in request, so the reason is only looked up with one.
        "withheld": withheld_reason(record, withheld_for(request))
        if form and kind == "communication"
        else "",
        "next": request.get_full_path() if request else "",
    }


def withheld_for(request):
    """The connections whose messages are kept out of translation, looked up once a page."""
    if not hasattr(request, "_withheld_connections"):
        request._withheld_connections = withheld_connections()
    return request._withheld_connections


@register.filter
def language_label(code):
    return language_name(code)
