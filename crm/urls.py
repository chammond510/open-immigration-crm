from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("setup/", views.first_run_setup, name="first_run_setup"),
    path("search/", views.search, name="search"),
    path("contacts/", views.contact_list, name="contact_list"),
    path("contacts/pipeline/", views.pipeline, name="pipeline"),
    path("contacts/new/", views.contact_create, name="contact_create"),
    path("contacts/<uuid:pk>/", views.contact_detail, name="contact_detail"),
    path("contacts/<uuid:pk>/edit/", views.contact_update, name="contact_update"),
    path(
        "contacts/<uuid:pk>/activities/new/",
        views.contact_add_activity,
        name="contact_add_activity",
    ),
    path("matters/", views.matter_list, name="matter_list"),
    path("matters/new/", views.matter_create, name="matter_create"),
    path("matters/<uuid:pk>/", views.matter_detail, name="matter_detail"),
    path("matters/<uuid:pk>/edit/", views.matter_update, name="matter_update"),
    path("matters/<uuid:pk>/parties/new/", views.matter_add_party, name="matter_add_party"),
    path("matter-parties/<int:pk>/remove/", views.matter_remove_party, name="matter_remove_party"),
    path(
        "matters/<uuid:pk>/activities/new/", views.matter_add_activity, name="matter_add_activity"
    ),
    path("work/", views.work_list, name="work_list"),
    path("work/new/", views.work_create, name="work_create"),
    path("work/<int:pk>/toggle/", views.work_toggle, name="work_toggle"),
    path("time/start/", views.timer_start, name="timer_start"),
    path("time/stop/", views.timer_stop, name="timer_stop"),
    path("time/<int:pk>/delete/", views.time_entry_delete, name="time_entry_delete"),
    path("matters/<uuid:matter_pk>/checklist/new/", views.checklist_add, name="checklist_add"),
    path("checklist/<int:pk>/toggle/", views.checklist_toggle, name="checklist_toggle"),
    path(
        "matters/<uuid:matter_pk>/documents/upload/", views.document_upload, name="document_upload"
    ),
    path("documents/<uuid:pk>/download/", views.document_download, name="document_download"),
    path("documents/<uuid:pk>/delete/", views.document_delete, name="document_delete"),
    path("intake/", views.intake_workspace, name="intake_workspace"),
    path("intake/forms/new/", views.intake_form_create, name="intake_form_create"),
    path("intake/forms/<uuid:pk>/edit/", views.intake_form_update, name="intake_form_update"),
    path("intake/invites/new/", views.intake_invite_create, name="intake_invite_create"),
    path(
        "intake/invites/<uuid:pk>/revoke/", views.intake_invite_revoke, name="intake_invite_revoke"
    ),
    path(
        "intake/submissions/<uuid:pk>/",
        views.intake_submission_detail,
        name="intake_submission_detail",
    ),
    path(
        "intake/submissions/<uuid:pk>/review/",
        views.intake_submission_review,
        name="intake_submission_review",
    ),
    path("i/<str:token>/", views.public_intake, name="public_intake"),
    path("settings/firm/", views.firm_settings, name="firm_settings"),
    path("exports/contacts-and-matters.csv", views.export_csv, name="export_csv"),
]
