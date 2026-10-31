from django.urls import path

from . import views

urlpatterns = [
    path("", views.import_list, name="import_list"),
    path("template.csv", views.import_template, name="import_template"),
    path("<uuid:pk>/", views.import_report, name="import_report"),
    path("<uuid:pk>/columns/", views.import_columns, name="import_columns"),
    path("<uuid:pk>/preview/", views.import_preview, name="import_preview"),
    path("<uuid:pk>/skipped.csv", views.import_skipped, name="import_skipped"),
    path("<uuid:pk>/discard/", views.import_discard, name="import_discard"),
]
