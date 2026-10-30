from django.urls import path

from . import views

urlpatterns = [
    path("", views.invoice_list, name="invoice_list"),
    path("settings/", views.billing_settings, name="billing_settings"),
    path("invoices/new/", views.invoice_create, name="invoice_create"),
    path("invoices/<uuid:pk>/", views.invoice_detail, name="invoice_detail"),
    path("invoices/<uuid:pk>/notes/", views.invoice_notes, name="invoice_notes"),
    path("invoices/<uuid:pk>/lines/", views.invoice_line_add, name="invoice_line_add"),
    path(
        "invoices/<uuid:pk>/lines/<int:line_pk>/delete/",
        views.invoice_line_delete,
        name="invoice_line_delete",
    ),
    path("invoices/<uuid:pk>/time/", views.invoice_time_add, name="invoice_time_add"),
    path("invoices/<uuid:pk>/issue/", views.invoice_issue, name="invoice_issue"),
    path("invoices/<uuid:pk>/delete/", views.invoice_delete, name="invoice_delete"),
    path("invoices/<uuid:pk>/void/", views.invoice_void, name="invoice_void"),
    path("invoices/<uuid:pk>/print/", views.invoice_print, name="invoice_print"),
    path("invoices/<uuid:pk>/payments/", views.payment_record, name="payment_record"),
    path("invoices/<uuid:pk>/plan/", views.plan_create, name="plan_create"),
    path("invoices/<uuid:pk>/plan/cancel/", views.plan_cancel, name="plan_cancel"),
    path(
        "invoices/<uuid:pk>/payments/<uuid:payment_pk>/void/",
        views.payment_void,
        name="payment_void",
    ),
]
