"""Legal research from a matter, through any connection with the ``search_authorities`` action.

The payload is ``{"query", "scope", "limit"}``, where ``scope`` is one of the
connector's ``search_scopes`` or empty for its default. The connector answers
``{"results": [...], "total"}``, each result holding the keys in ``RESULT_FIELDS``.
Searches go through the outbound policy, since the query leaves the CRM and may
describe a client's situation.

Results are kept with the search, so a staff member can save one to the matter
without searching again. Saving keeps the citation, link, and excerpt in the CRM,
with the staff member's note.
"""

from datetime import date
from urllib.parse import urlsplit

from django.db import transaction

from crm.connect import policy
from crm.connect.base import Capability
from crm.connect.models import OutboundRequest
from crm.connect.services import usable_connections

from .models import Authority, Search

SEARCH = "search_authorities"
RESULT_FIELDS = {
    "id": 255,
    "citation": 255,
    "title": 500,
    "court": 255,
    "decided": 10,
    "kind": 120,
    "standing": 255,
    "url": 1000,
    "excerpt": 2000,
}
MAX_RESULTS = 20
# Searches kept per matter; older ones are removed, but saved authorities stay.
KEEP_SEARCHES = 25


def research_connections(user):
    """Legal research connections ``user`` may search through: the firm's and their own."""
    return usable_connections(user, SEARCH, Capability.LEGAL_RESEARCH)


def scopes(connection):
    """What a connection can search, as (value, label) pairs; the first is its default."""
    cls = connection.connector_class
    return list(cls.search_scopes) if cls else []


def clean_result(item):
    """One result in core's shape, with anything unexpected dropped."""
    if not isinstance(item, dict):
        return None
    result = {key: str(item.get(key) or "").strip()[:limit] for key, limit in RESULT_FIELDS.items()}
    if not result["id"] or not (result["title"] or result["citation"]):
        return None
    if urlsplit(result["url"]).scheme != "https":
        result["url"] = ""
    try:
        date.fromisoformat(result["decided"])
    except ValueError:
        result["decided"] = ""
    return result


def search(matter, *, query, connection, actor, scope=""):
    """Run one search and keep it with the matter; return the Search."""
    allowed = {value for value, _ in scopes(connection)}
    scope = scope if scope in allowed else ""
    outbound = policy.send(
        connection,
        SEARCH,
        {"query": query, "scope": scope, "limit": MAX_RESULTS},
        actor=actor,
        summary="Legal research search",
        target=matter,
    )
    answer = getattr(outbound, "response", None)
    answer = answer if isinstance(answer, dict) else {}
    results = [
        cleaned
        for cleaned in (clean_result(item) for item in answer.get("results") or [])
        if cleaned
    ][:MAX_RESULTS]
    total = answer.get("total")
    if outbound.status == OutboundRequest.Status.SENT:
        status, error = Search.Status.DONE, ""
    elif outbound.status == OutboundRequest.Status.PREVIEWED:
        status, error = Search.Status.PREVIEWED, ""
    else:
        status, error = Search.Status.FAILED, outbound.error or "The search was not sent."
    with transaction.atomic():
        found = Search.objects.create(
            matter=matter,
            connection=connection,
            source=connection.label[:120],
            query=query[:500],
            scope=scope,
            status=status,
            results=results,
            total=total if isinstance(total, int) and total >= 0 else None,
            error=error[:500],
            searched_by=actor,
        )
        old = matter.research_searches.values_list("pk", flat=True)[KEEP_SEARCHES:]
        Search.objects.filter(pk__in=list(old)).delete()
    return found


def save(search, index, *, actor, note=""):
    """Save one result of a search to its matter; return (authority, created)."""
    result = search.results[index]
    connector_key = search.connection.connector_key if search.connection else ""
    decided = date.fromisoformat(result["decided"]) if result["decided"] else None
    return Authority.objects.get_or_create(
        matter=search.matter,
        connector_key=connector_key,
        external_id=result["id"],
        defaults={
            "connection": search.connection,
            "source": search.source,
            "citation": result["citation"],
            "title": result["title"] or result["citation"],
            "court": result["court"],
            "decided": decided,
            "kind": result["kind"],
            "standing": result["standing"],
            "url": result["url"],
            "excerpt": result["excerpt"],
            "note": note,
            "query": search.query,
            "saved_by": actor,
        },
    )
