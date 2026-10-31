"""What staff may do beyond everyday work, by the permissions their role grants.

Every active staff member works with every contact, matter, and work item. A
role is a Django group; its permissions add the firm's settings, money, bulk
data, deletion, and staff. The CRM creates four presets (Managing partner,
Attorney, Paralegal, and Office administrator) that the firm may change in
Django admin. Superusers hold every permission.
"""

from functools import wraps

from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import PermissionDenied

BILLING = "billing.access_billing"
MANAGE_BILLING = "billing.manage_billing"
DELETE_RECORDS = "crm.delete_records"
IMPORT_RECORDS = "crm.import_records"
EXPORT_RECORDS = "crm.export_records"
MANAGE_FIRM_SETTINGS = "crm.manage_firm_settings"
MANAGE_CONNECTIONS = "crm.manage_connections"
MANAGE_STAFF = "crm.manage_staff"

# (permission, label, what it allows), in the order people read them.
PERMISSIONS = (
    (BILLING, "Billing", "Invoices, payments, and payment plans"),
    (MANAGE_BILLING, "Voids and rates", "Void invoices and payments, and set billing rates"),
    (
        DELETE_RECORDS,
        "Delete records",
        "Delete documents, communications, notices, time entries, and matter parties",
    ),
    (IMPORT_RECORDS, "Import", "Import contacts and matters"),
    (EXPORT_RECORDS, "Export", "Download every contact and matter as a spreadsheet"),
    (MANAGE_FIRM_SETTINGS, "Firm settings", "Firm settings, workflows, and custom fields"),
    (
        MANAGE_CONNECTIONS,
        "Connections and AI",
        "Firm connections, AI features, AI assistants, and the API",
    ),
    (MANAGE_STAFF, "Manage staff", "Add people, change their roles, and deactivate accounts"),
)


def is_staff(user):
    return user.is_active and user.is_staff


def can(user, permission):
    """Whether ``user`` is active staff holding ``permission``."""
    return is_staff(user) and user.has_perm(permission)


def require(user, permission):
    if not can(user, permission):
        raise PermissionDenied


def permission_required(permission):
    """Staff only, like ``staff_required``; staff without ``permission`` are refused."""

    def decorator(view):
        @user_passes_test(is_staff, login_url="login")
        @wraps(view)
        def checked(request, *args, **kwargs):
            require(request.user, permission)
            return view(request, *args, **kwargs)

        return checked

    return decorator
