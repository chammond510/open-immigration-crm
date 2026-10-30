from django import template

from ..models import active_fields

register = template.Library()


@register.inclusion_tag("customfields/includes/facts.html")
def custom_field_facts(record, applies_to):
    """Definition-list rows for a record's active custom fields."""
    stored = record.custom_fields or {}
    return {
        "rows": [
            (definition.label, definition.display(stored.get(definition.key)))
            for definition in active_fields(applies_to)
        ]
    }
