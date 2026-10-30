"""CourtListener: case law from the Free Law Project, searched from a matter.

Each staff member connects their own CourtListener account with its API token, sent
as ``Authorization: Token …``. CourtListener's terms forbid sharing or pooling a
token, so the connection is personal and is never used for anyone else's search;
a firm that wants one account for a team needs an agreement with the Free Law
Project.

Searches use the v4 search API for case law (``type=o``), highlighted, newest
relevance first, with published and unpublished opinions both included, since
unpublished decisions matter in immigration practice. Each scope is a set of
courts: the federal courts of appeals and the Supreme Court by default, the Board
of Immigration Appeals, the Supreme Court alone, or every court. Links go to
CourtListener's opinion pages.

CourtListener's default limits are low (5 requests a minute, 50 an hour, 125 a
day), so a rate-limited search is not retried; staff are told when to try again.
CourtListener keeps a log of API queries unless the account turns that off.

API reference: https://wiki.free.law/c/courtlistener/help/api/rest/v4/search
"""

import re

from django.utils.html import strip_tags

from crm.connect.base import (
    ApiKeyAuth,
    Capability,
    ConnectionScope,
    Connector,
    ConnectorError,
    CredentialField,
    DataFlows,
    Maturity,
)
from crm.connect.http import ProviderClient
from crm.connect.registry import register

SITE_URL = "https://www.courtlistener.com"
API_URL = f"{SITE_URL}/api/rest/v4"
APPEALS = "scotus ca1 ca2 ca3 ca4 ca5 ca6 ca7 ca8 ca9 ca10 ca11 cadc cafc"
# Scope -> courts searched, space-separated, or "" for every court. The first is the default.
COURTS = {"": APPEALS, "bia": "bia", "scotus": "scotus", "all": ""}
SCOPES = (
    ("", "Federal courts of appeals and the Supreme Court"),
    ("bia", "Board of Immigration Appeals"),
    ("scotus", "Supreme Court"),
    ("all", "Every court"),
)


def excerpt(result):
    """The first opinion's snippet as plain text, without highlighting."""
    for opinion in result.get("opinions") or []:
        if isinstance(opinion, dict) and opinion.get("snippet"):
            return re.sub(r"\s+", " ", strip_tags(str(opinion["snippet"]))).strip()
    return ""


def result_from(result):
    """One CourtListener case in core's research result shape."""
    citations = [str(c) for c in result.get("citation") or [] if c]
    path = str(result.get("absolute_url") or "")
    return {
        "id": f"cluster:{result.get('cluster_id')}" if result.get("cluster_id") else "",
        "citation": citations[0] if citations else str(result.get("neutralCite") or ""),
        "title": str(result.get("caseName") or result.get("caseNameFull") or ""),
        "court": str(result.get("court") or ""),
        "decided": str(result.get("dateFiled") or "")[:10],
        "kind": "Case law",
        "standing": str(result.get("status") or "").capitalize(),
        "url": f"{SITE_URL}{path}" if path.startswith("/") else "",
        "excerpt": excerpt(result),
    }


def wait_words(seconds):
    if not str(seconds or "").isdigit():
        return "later"
    minutes = max(1, round(int(seconds) / 60))
    if minutes < 90:
        return f"in about {minutes} minute{'s' if minutes != 1 else ''}"
    return f"in about {round(minutes / 60)} hours"


@register
class CourtListenerConnector(Connector):
    key = "courtlistener"
    name = "CourtListener"
    description = (
        "Search case law from the Free Law Project's CourtListener from a matter, with each "
        "staff member's own CourtListener account."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.LEGAL_RESEARCH})
    connection_scope = ConnectionScope.USER
    auth = ApiKeyAuth(
        fields=(
            CredentialField(
                "api_token",
                "CourtListener API token",
                help_text="Your own token, from your CourtListener profile under API token. "
                "CourtListener's terms do not allow sharing a token with others.",
            ),
        ),
        docs_url="https://www.courtlistener.com/profile/api-token/",
        label="API token",
    )
    data_flows = DataFlows(
        version=1,
        sends=(
            "The words of each search you run, and the courts you choose. CourtListener logs "
            "API searches unless you turn that off in your CourtListener profile.",
        ),
        receives=("Case names, citations, courts, dates, excerpts, and links to opinions",),
    )
    outbound_actions = {"search_authorities": Capability.LEGAL_RESEARCH}
    search_scopes = SCOPES

    def __init__(self, connection):
        super().__init__(connection)
        self.client = ProviderClient(
            API_URL,
            name="CourtListener",
            headers={
                "Accept": "application/json",
                "Authorization": f"Token {self.credentials.get('api_token', '')}",
            },
            timeout=30,
            # The limits are low and the waits long, so a refusal is reported at once.
            retries=0,
        )

    def call(self, path, **kwargs):
        try:
            response = self.client.request(
                "GET", path, expected=(200, 400, 401, 403, 429), **kwargs
            )
        except ConnectorError as exc:
            if "did not answer in time" in str(exc):
                raise ConnectorError(
                    "CourtListener did not answer in time. Try a narrower search."
                ) from None
            raise
        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                raise ConnectorError("CourtListener's answer was not understood.") from None
        if response.status_code in (401, 403):
            raise ConnectorError(
                "CourtListener did not accept the API token. Check it in your CourtListener "
                "profile.",
                status=response.status_code,
            )
        if response.status_code == 429:
            raise ConnectorError(
                "Your CourtListener account has reached its request limit. Try again "
                f"{wait_words(response.headers.get('Retry-After'))}.",
                status=429,
            )
        try:
            detail = str(response.json().get("detail") or "")
        except (ValueError, AttributeError):
            detail = ""
        raise ConnectorError(
            f"CourtListener could not run that search{f': {detail[:200]}' if detail else ''}.",
            status=400,
        )

    def test_connection(self):
        # The usage endpoint checks the token without spending the search quota.
        self.call("/api-usage/")

    def perform(self, action, payload):
        if action != "search_authorities":
            raise ConnectorError(f"CourtListener has no action named {action}.")
        query = str(payload.get("query") or "").strip()
        if not query:
            raise ConnectorError("Write what to search for.")
        params = {
            "q": query,
            "type": "o",
            "order_by": "score desc",
            "highlight": "on",
            "stat_Published": "on",
            "stat_Unpublished": "on",
        }
        courts = COURTS.get(payload.get("scope") or "", APPEALS)
        if courts:
            params["court"] = courts
        answer = self.call("/search/", params=params)
        limit = min(max(int(payload.get("limit") or 20), 1), 20)
        results = [
            result_from(result)
            for result in (answer.get("results") or [])[:limit]
            if isinstance(result, dict)
        ]
        count = answer.get("count")
        return {
            "id": "",
            "results": [result for result in results if result["id"]],
            "total": count if isinstance(count, int) else len(results),
        }
