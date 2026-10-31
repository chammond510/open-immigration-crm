from django import template

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
        "form": form_for(context),
        "next": request.get_full_path() if request else "",
    }


@register.filter
def language_label(code):
    return language_name(code)
