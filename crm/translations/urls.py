from django.urls import path

from . import views

urlpatterns = [
    path(
        "communications/<uuid:pk>/",
        views.translate_communication_view,
        name="translate_communication",
    ),
    path(
        "intake-submissions/<uuid:pk>/",
        views.translate_submission_view,
        name="translate_submission",
    ),
]
