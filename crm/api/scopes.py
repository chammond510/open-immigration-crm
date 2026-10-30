"""What an API token may reach. Each resource has a read scope and, unless it is
read-only, a write scope; scopes are stored as "<resource>:<access>" strings."""

READ = "read"
WRITE = "write"

RESOURCES = {
    "contacts": "Contacts",
    "matters": "Matters and their parties",
    "work": "Tasks, deadlines, and appointments",
    "notes": "Notes and activity",
    "communications": "Communications log",
    "time": "Time entries",
    "notices": "USCIS notices and status checks",
    "billing": "Invoices and payments",
}

# Billing is read-only through the API, and needs the billing permission too.
READ_ONLY = {"billing"}


def scope(resource, access):
    return f"{resource}:{access}"


def accesses(resource):
    return (READ,) if resource in READ_ONLY else (READ, WRITE)


ALL_SCOPES = [scope(resource, access) for resource in RESOURCES for access in accesses(resource)]


def normalize(scopes):
    """Keep known scopes in a stable order; write access includes read access."""
    chosen = set(scopes) & set(ALL_SCOPES)
    for resource in RESOURCES:
        if scope(resource, WRITE) in chosen:
            chosen.add(scope(resource, READ))
    return [name for name in ALL_SCOPES if name in chosen]


def describe(scopes):
    """Plain-language summary, such as "Contacts (read and write), Matters (read)"."""
    parts = []
    for resource, label in RESOURCES.items():
        if scope(resource, WRITE) in scopes:
            parts.append(f"{label} (read and write)")
        elif scope(resource, READ) in scopes:
            parts.append(f"{label} (read)")
    return ", ".join(parts)
