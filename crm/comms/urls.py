from django.urls import path

from . import views

urlpatterns = [
    path("", views.communication_list, name="communication_list"),
    path("new/", views.communication_create, name="communication_create"),
    path("text/", views.communication_send_text, name="communication_send_text"),
    path("template/", views.communication_send_template, name="communication_send_template"),
    path("email/", views.communication_send_email, name="communication_send_email"),
    path("<uuid:pk>/delete/", views.communication_delete, name="communication_delete"),
    path("<uuid:pk>/match/", views.communication_match, name="communication_match"),
    path(
        "attachments/<uuid:pk>/download/",
        views.communication_attachment_download,
        name="communication_attachment_download",
    ),
    path(
        "attachments/<uuid:pk>/save/",
        views.communication_attachment_save,
        name="communication_attachment_save",
    ),
    path(
        "contacts/<uuid:pk>/texting/",
        views.contact_texting_opt_out,
        name="contact_texting_opt_out",
    ),
]
