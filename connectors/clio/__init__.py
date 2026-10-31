"""Clio Manage: a one-way, read-only import of the firm's contacts and matters.

The firm registers a private app in its Clio region's developer portal, with read
permission for contacts and matters, and a superuser enters its ID, secret, and
region here. Fetching from the connection's page then reads every contact, every
matter, and the other contacts linked to each matter (Clio's relationships), through
API v4, paged by ID and spaced to stay inside Clio's rate limit. Nothing is written
to Clio. Core turns the records into an import a superuser reviews and confirms;
what was imported before is left out, so a later fetch brings only what is new.

API reference: https://docs.developers.clio.com/clio-manage/api-reference/
"""

from datetime import date, datetime
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.utils import timezone

from crm.connect.base import (
    Capability,
    Connector,
    ConnectorError,
    DataFlows,
    Maturity,
    OAuthAuth,
    SettingField,
    SyncBatch,
)
from crm.connect.http import ProviderClient
from crm.connect.records import MATTER_STATUSES, ContactRecord, MatterRecord
from crm.connect.registry import register

REGIONS = (
    ("app.clio.com", "United States (app.clio.com)"),
    ("ca.app.clio.com", "Canada (ca.app.clio.com)"),
    ("eu.app.clio.com", "Europe (eu.app.clio.com)"),
    ("au.app.clio.com", "Australia (au.app.clio.com)"),
)
CONTACT_FIELDS = (
    "id,type,name,first_name,last_name,date_of_birth,primary_email_address,"
    "primary_phone_number,addresses{street,city,province,postal_code,country,primary},"
    "updated_at"
)
MATTER_FIELDS = (
    "id,display_number,description,status,open_date,client{id},practice_area{name},"
    "responsible_attorney{email},updated_at"
)
RELATIONSHIP_FIELDS = "id,description,matter{id},contact{id}"
PAGE_SIZE = 200
# Clio allows each sign-in 50 requests a minute at busy times.
MIN_INTERVAL = 1.25
# A guard against a list that never ends.
MOST_ITEMS = 200_000
# Words in a practice area's name that point to one of the CRM's case types.
CASE_TYPES = (
    ("humanitarian", ("asylum", "refugee", "humanitarian", "vawa", "u visa", "t visa", "tps")),
    ("naturalization", ("naturaliz", "citizenship")),
    ("removal", ("removal", "deportation", "immigration court", "eoir", "detention")),
    ("waiver", ("waiver",)),
    ("appeal", ("appeal", "motion")),
    ("employment", ("employment", "business", "h-1b", "h1b", "perm", "investor")),
    ("family", ("family", "marriage", "spous", "fianc")),
)


def validate_region(value):
    if value not in dict(REGIONS):
        raise ValidationError("Choose the region the firm's Clio account is in.")


def case_type(practice_area):
    name = (practice_area or "").lower()
    for value, words in CASE_TYPES:
        if any(word in name for word in words):
            return value
    return ""


def parse_date(value):
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def parse_time(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return timezone.now()
    return moment if moment.tzinfo else timezone.make_aware(moment)


def primary(items):
    items = [item for item in items or [] if isinstance(item, dict)]
    return next((item for item in items if item.get("primary")), items[0] if items else {})


def contact_record(item):
    first, last = str(item.get("first_name") or ""), str(item.get("last_name") or "")
    name = str(item.get("name") or "").strip()
    if not (first or last):
        first, _, last = name.rpartition(" ")
        if not first:
            first, last = last, ""
    address = primary(item.get("addresses"))
    street = str(address.get("street") or "").strip().splitlines() or [""]
    return ContactRecord(
        external_id=str(item["id"]),
        occurred_at=parse_time(item.get("updated_at")),
        first_name=first[:100],
        last_name=last[:100],
        company=name if item.get("type") == "Company" else "",
        email=str(item.get("primary_email_address") or ""),
        phone=str(item.get("primary_phone_number") or ""),
        date_of_birth=parse_date(item.get("date_of_birth")),
        address_line1=street[0],
        address_line2=" ".join(line.strip() for line in street[1:]),
        city=str(address.get("city") or ""),
        region=str(address.get("province") or ""),
        postal_code=str(address.get("postal_code") or ""),
        country=str(address.get("country") or ""),
    )


def matter_record(item, parties):
    client = (item.get("client") or {}).get("id")
    if not client:
        # Clio requires a client; without one the matter cannot be filed.
        return None
    number = str(item.get("display_number") or "")
    title, _, rest = str(item.get("description") or "").strip().partition("\n")
    status = str(item.get("status") or "").lower()
    area = str((item.get("practice_area") or {}).get("name") or "")
    return MatterRecord(
        external_id=str(item["id"]),
        occurred_at=parse_time(item.get("updated_at")),
        client=str(client),
        title=(title.strip() or number or f"Clio matter {item['id']}")[:255],
        number=number,
        case_type=case_type(area),
        practice_area=area,
        status=status if status in MATTER_STATUSES else "open",
        opened_on=parse_date(item.get("open_date")),
        description=rest.strip(),
        responsible_email=str((item.get("responsible_attorney") or {}).get("email") or ""),
        parties=tuple(parties.get(str(item["id"]), ())),
    )


@register
class ClioConnector(Connector):
    key = "clio"
    name = "Clio Manage"
    description = (
        "Move contacts and matters over from Clio Manage, read-only: you review what each "
        "fetch would import before anything is saved, and a later fetch brings only what is new."
    )
    maturity = Maturity.BETA
    capabilities = frozenset({Capability.MIGRATION})
    auth = OAuthAuth(
        authorize_url="https://{region}/oauth/authorize",
        token_url="https://{region}/oauth/token",
        scopes={Capability.MIGRATION: ()},
        docs_url="https://docs.developers.clio.com/handbook/getting-started/building-private-apps/",
        # Without this Clio shows its own error page when someone declines.
        extra_authorize_params={"redirect_on_decline": "true"},
        app_fields=(
            SettingField(
                "region",
                "Clio region",
                choices=REGIONS,
                default="app.clio.com",
                help_text="Where the firm's Clio account is. Register the app in that region's "
                "developer portal, with read permission for Contacts and Matters.",
                validate=validate_region,
            ),
        ),
    )
    sync_capabilities = frozenset({Capability.MIGRATION})
    sync_interval = None
    data_flows = DataFlows(
        version=1,
        sends=("Nothing: the CRM only reads from Clio, when a superuser fetches",),
        receives=(
            "Every contact the signed-in user can see: name, email, phone, date of birth, "
            "and address",
            "Every matter: its number, description, status, open date, practice area, "
            "client, and responsible attorney's email",
            "The other contacts linked to each matter, and their roles",
        ),
    )

    def __init__(self, connection):
        super().__init__(connection)
        self.host = self.app_setting("region") or "app.clio.com"
        self.http = ProviderClient(
            f"https://{self.host}/api/v4",
            name="Clio",
            headers={"Accept": "application/json"},
            min_interval=MIN_INTERVAL,
        )

    def get(self, path, **kwargs):
        validate_region(self.host)
        self.http.headers["Authorization"] = f"Bearer {self.access_token()}"
        try:
            return self.http.get(path, **kwargs)
        except ConnectorError as exc:
            if exc.status == 401:
                raise ConnectorError(
                    "Clio did not accept the sign-in. Sign in again, in the same region as "
                    "the firm's Clio account.",
                    status=401,
                ) from None
            if exc.status == 403:
                raise ConnectorError(
                    "Clio refused the request. Give the firm's Clio app read permission for "
                    "Contacts and Matters, then sign in again.",
                    status=403,
                ) from None
            raise

    def pages(self, path, fields, *, ordered=True):
        """Every item of one Clio list, following its next-page links."""
        params = {"fields": fields, "limit": PAGE_SIZE}
        if ordered:
            # Clio's cursor paging needs the list in ID order.
            params["order"] = "id(asc)"
        count = 0
        while path and count < MOST_ITEMS:
            page = self.get(path, params=params)
            for item in page.get("data") or []:
                count += 1
                yield item
            link = ((page.get("meta") or {}).get("paging") or {}).get("next")
            if link and urlsplit(link).netloc != self.host:
                # The sign-in is never sent anywhere but the firm's Clio region.
                raise ConnectorError("Clio pointed to its next page somewhere else.")
            # The link carries the page token and every other parameter.
            path, params = link, None

    def test_connection(self):
        self.get("/users/who_am_i.json", params={"fields": "id,name"})

    def sync(self, capability, cursor):
        if capability != Capability.MIGRATION:
            raise ConnectorError(f"Clio does not sync {capability}.")
        contacts = [contact_record(item) for item in self.pages("/contacts.json", CONTACT_FIELDS)]
        parties = {}
        for item in self.pages("/relationships.json", RELATIONSHIP_FIELDS, ordered=False):
            matter = (item.get("matter") or {}).get("id")
            contact = (item.get("contact") or {}).get("id")
            if matter and contact:
                role = str(item.get("description") or "").strip()
                parties.setdefault(str(matter), []).append((str(contact), role))
        matters = [
            record
            for item in self.pages("/matters.json", MATTER_FIELDS)
            if (record := matter_record(item, parties))
        ]
        # Every fetch reads everything; core leaves out what was imported before.
        return SyncBatch(records=[*contacts, *matters], cursor={})

    def revoke(self):
        """Clio ends a sign-in by its access token."""
        token = self.credentials.get("access_token")
        if token and self.host in dict(REGIONS):
            ProviderClient(name="Clio", retries=1).request(
                "POST",
                f"https://{self.host}/oauth/deauthorize",
                headers={"Authorization": f"Bearer {token}"},
                data={"token": token},
                expected=(200, 401),
            )
