"""Importing contacts and matters from a spreadsheet saved as CSV.

Each row is a contact, with a matter when the row has one. Rows are checked with the
same forms as the web pages. A row whose email or phone matches a contact already in
the CRM, or one earlier in the file, never changes that contact: its matter, if any,
is added to it, and a row with no matter is skipped. Nothing is saved until staff
confirm, and every skipped row is reported with its reason. Reasons never repeat a
row's values, so the report can be kept after the file is deleted.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from crm.connect.models import ExternalRef
from crm.connect.refs import find as find_ref
from crm.connect.refs import link as link_ref
from crm.customfields.forms import PREFIX as CUSTOM_PREFIX
from crm.customfields.models import CustomField, active_fields
from crm.forms import ContactForm, MatterForm
from crm.matching import normalize_email, normalize_phone
from crm.models import Activity, Contact, Matter, MatterParty
from crm.services import add_activity, audit

from .models import SpreadsheetImport

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
KEEP_UPLOAD = timedelta(days=1)
DELIMITERS = ",;\t"

DATE_ORDERS = {
    "mdy": ("Month/day/year, such as 03/31/2026", ("%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y")),
    "dmy": ("Day/month/year, such as 31/03/2026", ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y")),
    "ymd": ("Year-month-day, such as 2026-03-31", ("%Y/%m/%d", "%Y.%m.%d")),
}


class SpreadsheetError(ValueError):
    """A file that cannot be imported, with a message for staff."""


@dataclass(frozen=True)
class Column:
    """A CRM field a spreadsheet column can fill."""

    key: str
    label: str
    record: str  # "contact" or "matter"
    form_field: str = ""
    aliases: tuple = ()
    choices: tuple = ()  # (value, label) pairs, for a field with a fixed list
    kind: str = "text"  # text, date, choice, yes_no, or staff

    @property
    def headers(self):
        return {header_key(name) for name in (self.label, self.key, *self.aliases)}


STANDARD_COLUMNS = (
    Column(
        "full_name",
        "Full name",
        "contact",
        aliases=("name", "client name", "client", "contact name", "contact"),
    ),
    Column(
        "first_name", "First name", "contact", "first_name", ("first", "given name", "firstname")
    ),
    Column(
        "last_name",
        "Last name",
        "contact",
        "last_name",
        ("last", "surname", "family name", "lastname"),
    ),
    Column("email", "Email", "contact", "email", ("email address", "e-mail")),
    Column(
        "phone",
        "Phone",
        "contact",
        "phone",
        ("phone number", "mobile", "mobile phone", "cell", "cell phone", "telephone"),
    ),
    Column(
        "date_of_birth",
        "Date of birth",
        "contact",
        "date_of_birth",
        ("dob", "birth date", "birthdate", "birthday"),
        kind="date",
    ),
    Column(
        "preferred_language", "Preferred language", "contact", "preferred_language", ("language",)
    ),
    Column(
        "address_line1",
        "Address",
        "contact",
        "address_line1",
        ("address line 1", "address 1", "street", "street address"),
    ),
    Column(
        "address_line2",
        "Address line 2",
        "contact",
        "address_line2",
        ("address 2", "apartment", "suite", "unit"),
    ),
    Column("city", "City", "contact", "city", ("town",)),
    Column("region", "State or province", "contact", "region", ("state", "province")),
    Column(
        "postal_code",
        "Postal code",
        "contact",
        "postal_code",
        ("zip", "zip code", "zipcode", "postcode"),
    ),
    Column("country", "Country", "contact", "country"),
    Column(
        "country_of_birth", "Country of birth", "contact", "country_of_birth", ("birth country",)
    ),
    Column(
        "nationality",
        "Nationality",
        "contact",
        "nationality",
        ("citizenship", "country of citizenship"),
    ),
    Column(
        "a_number",
        "A-number",
        "contact",
        "a_number",
        ("a number", "a#", "alien number", "alien registration number"),
    ),
    Column(
        "current_immigration_status",
        "Immigration status",
        "contact",
        "current_immigration_status",
        ("current immigration status", "current status"),
    ),
    Column(
        "contact_status",
        "Contact status",
        "contact",
        "status",
        choices=tuple(Contact.Status.choices),
        kind="choice",
    ),
    Column(
        "stage",
        "Stage",
        "contact",
        "stage",
        ("contact stage", "pipeline stage"),
        choices=tuple(Contact.Stage.choices),
        kind="choice",
    ),
    Column("contact_notes", "Contact notes", "contact", "notes", ("notes",)),
    Column(
        "assigned_to",
        "Assigned to",
        "contact",
        "assigned_to",
        ("responsible attorney", "attorney", "assigned attorney", "owner"),
        kind="staff",
    ),
    Column(
        "matter_title",
        "Matter title",
        "matter",
        "title",
        ("matter", "matter name", "case", "case name", "title"),
    ),
    Column(
        "case_type",
        "Case type",
        "matter",
        "case_type",
        ("matter type", "practice area"),
        choices=tuple(Matter.CaseType.choices),
        kind="choice",
    ),
    Column(
        "matter_status",
        "Matter status",
        "matter",
        "status",
        choices=tuple(Matter.Status.choices),
        kind="choice",
    ),
    Column(
        "receipt_number",
        "Receipt number",
        "matter",
        "receipt_number",
        ("receipt", "receipt #", "receipt no"),
    ),
    Column("priority_date", "Priority date", "matter", "priority_date", kind="date"),
    Column(
        "filing_date",
        "Filing date",
        "matter",
        "filing_date",
        ("date filed", "filed", "filed on"),
        kind="date",
    ),
    Column("matter_description", "Matter description", "matter", "description", ("description",)),
    Column("matter_notes", "Matter notes", "matter", "notes", ("case notes",)),
)
CUSTOM_KINDS = {
    CustomField.FieldType.DATE: "date",
    CustomField.FieldType.CHOICE: "choice",
    CustomField.FieldType.YES_NO: "yes_no",
}


def counted(number, noun):
    return f"{number:,} {noun}{'' if number == 1 else 's'}"


def header_key(text):
    return re.sub(r"[^a-z0-9#]", "", str(text).lower())


def receipt_key(text):
    return re.sub(r"[^A-Z0-9]", "", str(text).upper())


def choice_key(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def columns():
    """Every column an import can fill: the standard ones, then the active custom fields."""
    custom = []
    for record in ("contact", "matter"):
        for definition in active_fields(record):
            custom.append(
                Column(
                    f"{record}:{definition.key}",
                    definition.label,
                    record,
                    CUSTOM_PREFIX + definition.key,
                    (definition.key,),
                    choices=tuple((value, value) for value in definition.choice_list),
                    kind=CUSTOM_KINDS.get(definition.field_type, "text"),
                )
            )
    return [*STANDARD_COLUMNS, *custom]


def template_headers():
    """The header row of the downloadable template: every column but Full name."""
    return [column.label for column in columns() if column.key != "full_name"]


# Reading the file


def read_csv(data):
    """``(text, headers, rows)`` from an uploaded file's bytes, or SpreadsheetError."""
    if len(data) > MAX_BYTES:
        raise SpreadsheetError("The file is larger than 5 MB. Split it into smaller files.")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SpreadsheetError("The file could not be read. Save it as CSV UTF-8 and try again.")
    if "\x00" in text:
        raise SpreadsheetError(
            "This is not a CSV file. In the spreadsheet app, save it as CSV UTF-8 and try again."
        )
    headers, rows = parse(text)
    if not any(headers):
        raise SpreadsheetError("The file needs a header row naming each column.")
    if not rows:
        raise SpreadsheetError("The file has no rows below its header row.")
    if len(rows) > MAX_ROWS:
        raise SpreadsheetError(
            f"The file has more than {MAX_ROWS:,} rows. Split it into smaller files."
        )
    return text, headers, rows


def parse(text):
    """The header row and the rows below it, each padded to the header's width."""
    first_line = text.split("\n", 1)[0]
    delimiter = max(DELIMITERS, key=first_line.count) if first_line else ","
    try:
        records = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    except csv.Error as exc:
        raise SpreadsheetError("The file is not valid CSV.") from exc
    if not records:
        return [], []
    headers = [cell.strip() for cell in records[0]]
    width = len(headers)
    rows = [(record + [""] * width)[:width] for record in records[1:]]
    return headers, rows


def suggest_mapping(headers):
    """Each column's CRM field, where its header names one; each field is used once."""
    available = columns()
    mapping, used = {}, set()
    for index, header in enumerate(headers):
        key = header_key(header)
        for column in available:
            if column.key not in used and key and key in column.headers:
                mapping[str(index)] = column.key
                used.add(column.key)
                break
    return mapping


def examples(headers, rows, count=3):
    """A few values from each column, to help staff match columns to fields."""
    found = [[] for _ in headers]
    for row in rows:
        for index, value in enumerate(row):
            value = value.strip()
            if value and len(found[index]) < count and value not in found[index]:
                found[index].append(value[:60])
    return found


# Planning


@dataclass
class RowPlan:
    number: int
    name: str
    contact: Contact | None = None
    same_as: int | None = None
    contact_form: ContactForm | None = None
    matter_form: MatterForm | None = None
    reason: str = ""
    # For an import from another system: the row's contact, matter, and other parties
    # there, as {"contact": id, "matter": id, "parties": [[id, role], ...]}.
    refs: dict = field(default_factory=dict)


@dataclass
class Plan:
    rows: list = field(default_factory=list)

    @property
    def skipped(self):
        return [row for row in self.rows if row.reason]

    @property
    def new_contacts(self):
        return [row for row in self.rows if row.contact_form and not row.reason]

    @property
    def new_matters(self):
        return [row for row in self.rows if row.matter_form and not row.reason]

    @property
    def matters_for_known_contacts(self):
        return [row for row in self.new_matters if not row.contact_form]


def parse_date(value, order):
    value = re.split(r"[ T]", value.strip(), maxsplit=1)[0]
    formats = ("%Y-%m-%d", *DATE_ORDERS.get(order, DATE_ORDERS["mdy"])[1])
    for pattern in formats:
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def form_value(column, value, options):
    """A cell as the value the record's form expects, or ``(None, reason)``."""
    if column.kind == "date":
        parsed = parse_date(value, options.get("date_order"))
        if parsed is None:
            return None, f"{column.label}: enter a full date, written as the import's dates are."
        return parsed, ""
    if column.kind == "choice":
        wanted = choice_key(value)
        for choice, label in column.choices:
            if wanted in (choice_key(choice), choice_key(label)):
                return choice, ""
        return None, f"{column.label}: not one of the choices."
    if column.kind == "yes_no":
        answer = {
            "yes": "true",
            "y": "true",
            "true": "true",
            "1": "true",
            "no": "false",
            "n": "false",
            "false": "false",
            "0": "false",
        }
        if value.strip().lower() in answer:
            return answer[value.strip().lower()], ""
        return None, f"{column.label}: answer yes or no."
    return value, ""


def describe_errors(form, labels):
    problems = []
    for name, errors in form.errors.as_data().items():
        label = labels.get(name) or (form.fields[name].label if name in form.fields else "")
        for error in errors:
            # Django repeats a rejected choice in its message; the report never repeats values.
            message = (
                "not one of the choices."
                if error.code == "invalid_choice"
                else (" ".join(error.messages))
            )
            problems.append(f"{label}: {message}" if label else message)
    return " ".join(problems)


def blank_values(form):
    return {name: "" if form[name].value() is None else form[name].value() for name in form.fields}


class Planner:
    """Works out what each row of one import would do, without saving anything."""

    def __init__(self, spreadsheet):
        self.options = spreadsheet.options or {}
        available = {column.key: column for column in columns()}
        self.mapping = {
            int(index): available[key]
            for index, key in (spreadsheet.mapping or {}).items()
            if key in available
        }
        self.labels = {
            record: {
                column.form_field: column.label
                for column in available.values()
                if column.record == record and column.form_field
            }
            for record in ("contact", "matter")
        }
        self.contact_defaults = blank_values(ContactForm())
        self.contact_defaults.update(
            status=self.options.get("contact_status") or Contact.Status.CLIENT,
            stage=self.options.get("stage") or Contact.Stage.ACTIVE,
            assigned_to=self.options.get("assigned_to") or "",
        )
        self.matter_defaults = blank_values(MatterForm())
        self.matter_defaults.update(
            status=self.options.get("matter_status") or Matter.Status.OPEN,
            assigned_to=self.options.get("assigned_to") or "",
        )
        self.seen = {}  # email or phone -> number of the row that adds that contact
        # For an import from another system: its record IDs, and those imported before.
        self.connection = spreadsheet.connection
        self.refs = spreadsheet.refs or {}
        self.ref_rows = {}  # the other system's contact ID -> the row that adds it
        self.known_refs = {}  # the other system's contact ID -> a contact already in the CRM
        self.imported_matters = set(
            ExternalRef.objects.filter(
                connection=self.connection,
                content_type=ContentType.objects.get_for_model(Matter),
            ).values_list("external_id", flat=True)
            if self.connection
            else ()
        )
        self.receipts = {
            receipt_key(number)
            for number in Matter.objects.exclude(receipt_number="").values_list(
                "receipt_number", flat=True
            )
        }
        self.staff = {}

    def plan(self, rows):
        plan = Plan()
        for index, cells in enumerate(rows):
            values = {
                column: cells[position].strip()
                for position, column in self.mapping.items()
                if position < len(cells)
            }
            if any(values.values()):
                # Row 1 is the header, so the first row of data is row 2, as in the spreadsheet.
                plan.rows.append(self.plan_row(index + 2, values))
        return plan

    def plan_row(self, number, values):
        cell = {column.key: value for column, value in values.items()}
        first, last = cell.get("first_name", ""), cell.get("last_name", "")
        if not (first or last) and cell.get("full_name"):
            first, _, last = cell["full_name"].rpartition(" ")
            if not first:
                first, last = last, ""
        row = RowPlan(number, " ".join(filter(None, (first, last))) or f"Row {number}")
        row.refs = self.refs.get(str(number), {})
        if row.refs.get("skip"):
            return self.skip(row, row.refs["skip"])
        if row.refs.get("matter") in self.imported_matters:
            return self.skip(row, "This matter was imported before.")
        contact_ref = row.refs.get("contact", "")
        email, phone = normalize_email(cell.get("email")), normalize_phone(cell.get("phone"))
        keys = [key for key in (email, phone) if key]
        has_matter = any(value for column, value in values.items() if column.record == "matter")

        contact_data, matter_data = {}, {}
        for column, value in values.items():
            if not value or not column.form_field:
                continue
            if column.kind == "staff":
                user = self.find_staff(value)
                if user is None:
                    return self.skip(
                        row, "Assigned to: no active staff member has that email or username."
                    )
                contact_data["assigned_to"] = matter_data["assigned_to"] = user.pk
                continue
            converted, problem = form_value(column, value, self.options)
            if problem:
                return self.skip(row, problem)
            target = matter_data if column.record == "matter" else contact_data
            target[column.form_field] = converted
        contact_data.update(first_name=first, last_name=last)

        earlier = self.ref_rows.get(contact_ref) or next(
            (self.seen[key] for key in keys if key in self.seen), None
        )
        if earlier:
            row.same_as = earlier
        else:
            row.contact = self.known_refs.get(contact_ref) or self.linked_contact(contact_ref)
        if not (row.contact or row.same_as):
            found = self.known_contacts(email, phone)
            if len(found) > 1:
                return self.skip(
                    row, "Its email or phone matches more than one contact in the CRM."
                )
            row.contact = found[0] if found else None
        if row.contact and contact_ref:
            self.known_refs[contact_ref] = row.contact
        if (row.contact or row.same_as) and not has_matter and self.connection:
            # From another system, a contact the CRM already has is linked, not skipped.
            return row
        if (row.contact or row.same_as) and not has_matter:
            if row.same_as:
                return self.skip(
                    row, f"Same email or phone as row {row.same_as}, and no matter to add."
                )
            return self.skip(row, "Already in the CRM: a contact has the same email or phone.")

        if not (row.contact or row.same_as):
            form = ContactForm(data={**self.contact_defaults, **contact_data})
            if not form.is_valid():
                return self.skip(row, describe_errors(form, self.labels["contact"]))
            row.contact_form = form
        if has_matter:
            receipt = receipt_key(matter_data.get("receipt_number", ""))
            if receipt and receipt in self.receipts:
                return self.skip(
                    row,
                    "A matter with this receipt number is already in the CRM "
                    "or earlier in the file.",
                )
            form = MatterForm(data={**self.matter_defaults, **matter_data})
            # The client is attached when the import is saved; it may not exist yet.
            form.fields["primary_contact"].required = False
            if not form.is_valid():
                return self.skip(row, describe_errors(form, self.labels["matter"]))
            row.matter_form = form
            if receipt:
                self.receipts.add(receipt)
        if row.contact_form:
            for key in keys:
                self.seen.setdefault(key, number)
            if contact_ref:
                self.ref_rows.setdefault(contact_ref, number)
        return row

    def linked_contact(self, contact_ref):
        if not (self.connection and contact_ref):
            return None
        return find_ref(self.connection, Contact, contact_ref)[0]

    def skip(self, row, reason):
        row.contact_form = row.matter_form = None
        row.reason = reason
        return row

    def known_contacts(self, email, phone):
        query = Q()
        if email:
            query |= Q(email__iexact=email)
        if phone:
            query |= Q(phone_e164=phone)
        return list(Contact.objects.filter(query)[:2]) if query else []

    def find_staff(self, value):
        key = value.strip().lower()
        if key not in self.staff:
            self.staff[key] = (
                get_user_model()
                .objects.filter(is_active=True, is_staff=True)
                .filter(Q(email__iexact=key) | Q(username__iexact=key))
                .first()
            )
        return self.staff[key]


def plan_import(spreadsheet):
    _, rows = parse(spreadsheet.content or "")
    return Planner(spreadsheet).plan(rows)


# Importing


def run_import(spreadsheet, request):
    """Save every row the plan accepts, in one transaction, and keep the report."""
    with transaction.atomic():
        locked = SpreadsheetImport.objects.select_for_update().filter(pk=spreadsheet.pk).get()
        if locked.status == SpreadsheetImport.Status.IMPORTED or locked.content is None:
            return locked
        plan = plan_import(locked)
        connection = locked.connection
        added, matters = {}, []
        for row in plan.rows:
            if row.reason:
                continue
            contact = row.contact or added.get(row.same_as)
            if row.contact_form:
                contact = added[row.number] = row.contact_form.save()
                add_activity(
                    request=request,
                    contact=contact,
                    subject=f"Contact imported from {locked.source}",
                    kind=Activity.Kind.SYSTEM,
                )
            if connection and row.refs.get("contact"):
                link_ref(connection, contact, row.refs["contact"])
            if row.matter_form:
                matter = row.matter_form.save(commit=False)
                matter.primary_contact = contact
                matter.save()
                add_activity(
                    request=request,
                    matter=matter,
                    contact=contact,
                    subject=f"Matter {matter.matter_number} imported from {locked.source}",
                    kind=Activity.Kind.SYSTEM,
                )
                if connection and row.refs.get("matter"):
                    link_ref(connection, matter, row.refs["matter"])
                    matters.append((matter, row.refs.get("parties", [])))
        parties = sum(add_parties(connection, matter, links) for matter, links in matters)
        locked.status = SpreadsheetImport.Status.IMPORTED
        locked.imported_at = timezone.now()
        # A fetched import has no uploader; it is recorded as the work of whoever confirms.
        locked.created_by = locked.created_by or request.user
        locked.contacts_created = len(plan.new_contacts)
        locked.matters_created = len(plan.new_matters)
        locked.skipped = [{"row": row.number, "reason": row.reason} for row in plan.skipped]
        if connection:
            locked.options = {
                **locked.options,
                "contacts_linked": len({row.contact.pk for row in plan.rows if row.contact}),
                "parties_added": parties,
            }
        locked.save()
        audit(
            request,
            "data.imported",
            locked,
            detail=(
                f"{counted(locked.contacts_created, 'contact')} and "
                f"{counted(locked.matters_created, 'matter')} from {locked.source}; "
                f"{counted(len(locked.skipped), 'row')} skipped"
            ),
        )
    return locked


def add_parties(connection, matter, links):
    """Add the matter's other contacts from the other system, where they were imported."""
    added = 0
    for contact_ref, role in links:
        contact, _ = find_ref(connection, Contact, contact_ref)
        if contact is None or contact == matter.primary_contact:
            continue
        _, made = MatterParty.objects.get_or_create(
            matter=matter, contact=contact, role=(role or "Related contact")[:120]
        )
        added += made
    return added


def purge_uploads(sender=None, now=None, **kwargs):
    """Delete uploaded files a day old, and imports that were never confirmed."""
    cutoff = (now or timezone.now()) - KEEP_UPLOAD
    old = SpreadsheetImport.objects.filter(created_at__lt=cutoff)
    old.filter(status=SpreadsheetImport.Status.PREPARING).delete()
    old.filter(content__isnull=False).update(content=None, refs=None)
    return 0
