"""Text search shared by the pages, the API, and the MCP tools."""

from django.db.models import Q

CONTACT_SEARCH_FIELDS = ("first_name", "last_name", "email", "phone", "a_number")
MATTER_SEARCH_FIELDS = (
    "matter_number",
    "title",
    "receipt_number",
    "primary_contact__first_name",
    "primary_contact__last_name",
)


def words_match(query, fields):
    """Match records where every word of the query appears in one of the fields,
    so a search for a full name such as "Ana Muestra" finds the person."""
    match = Q()
    for word in query.split():
        any_field = Q()
        for field in fields:
            any_field |= Q(**{f"{field}__icontains": word})
        match &= any_field
    return match
