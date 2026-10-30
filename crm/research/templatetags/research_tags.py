from datetime import date

from django import template

from ..forms import AuthorityForm, SearchForm
from ..services import research_connections

register = template.Library()


@register.inclusion_tag("research/includes/research_panel.html", takes_context=True)
def research_panel(context, matter):
    """Saved authorities, a search box, and recent searches for a matter page."""
    user = context["user"]
    return {
        "request": context["request"],
        "user": user,
        "matter": matter,
        "authorities": matter.authorities.select_related("saved_by"),
        "searches": matter.research_searches.select_related("searched_by")[:5],
        "connections": research_connections(user),
        "form": SearchForm(user=user, auto_id="id_research_%s"),
        "manual_form": AuthorityForm(auto_id="id_authority_%s"),
    }


@register.filter
def iso_date(value):
    """A result's ISO date as a date, so templates can format it; anything else as is."""
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return value
