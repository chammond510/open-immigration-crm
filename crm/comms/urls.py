from django.urls import path

from . import views

urlpatterns = [
    path("", views.communication_list, name="communication_list"),
    path("new/", views.communication_create, name="communication_create"),
    path("<uuid:pk>/delete/", views.communication_delete, name="communication_delete"),
    path(
        "contacts/<uuid:pk>/texting/",
        views.contact_texting_opt_out,
        name="contact_texting_opt_out",
    ),
]
