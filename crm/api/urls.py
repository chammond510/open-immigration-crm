from django.urls import path

from . import views
from .http import not_found

urlpatterns = [
    path("v1/", views.index, name="api_index"),
    path("v1/users/", views.users, name="api_users"),
    path("v1/contacts/", views.contacts, name="api_contacts"),
    path("v1/contacts/<uuid:pk>/", views.contact_detail, name="api_contact"),
    path("v1/matters/", views.matters, name="api_matters"),
    path("v1/matters/<uuid:pk>/", views.matter_detail, name="api_matter"),
    path("v1/work-items/", views.work_items, name="api_work_items"),
    path("v1/work-items/<int:pk>/", views.work_item_detail, name="api_work_item"),
    path("v1/notes/", views.notes, name="api_notes"),
    path("v1/notes/<int:pk>/", views.note_detail, name="api_note"),
    path("v1/communications/", views.communications, name="api_communications"),
    path(
        "v1/communications/<uuid:pk>/",
        views.communication_detail,
        name="api_communication",
    ),
    path("v1/time-entries/", views.time_entries, name="api_time_entries"),
    path("v1/time-entries/<int:pk>/", views.time_entry_detail, name="api_time_entry"),
    path("v1/notices/", views.notices, name="api_notices"),
    path("v1/notices/<uuid:pk>/", views.notice_detail, name="api_notice"),
    path("v1/status-checks/", views.status_checks, name="api_status_checks"),
    path("v1/invoices/", views.invoices, name="api_invoices"),
    path("v1/invoices/<uuid:pk>/", views.invoice_detail, name="api_invoice"),
    path("v1/reports/summary/", views.report_summary, name="api_report_summary"),
    path("<path:rest>", not_found),
]
