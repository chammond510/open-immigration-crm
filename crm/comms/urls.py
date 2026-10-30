from django.urls import path

from . import views

urlpatterns = [
    path("", views.communication_list, name="communication_list"),
    path("new/", views.communication_create, name="communication_create"),
    path("text/", views.communication_send_text, name="communication_send_text"),
    path("email/", views.communication_send_email, name="communication_send_email"),
    path("<uuid:pk>/delete/", views.communication_delete, name="communication_delete"),
    path("<uuid:pk>/match/", views.communication_match, name="communication_match"),
    path(
        "contacts/<uuid:pk>/texting/",
        views.contact_texting_opt_out,
        name="contact_texting_opt_out",
    ),
]
