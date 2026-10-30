from django.urls import path

from . import views

urlpatterns = [
    path("", views.template_list, name="workflow_template_list"),
    path("new/", views.template_edit, name="workflow_template_create"),
    path("<uuid:pk>/edit/", views.template_edit, name="workflow_template_update"),
    path("matters/<uuid:matter_pk>/apply/", views.workflow_apply, name="workflow_apply"),
    path("matters/<uuid:matter_pk>/remove/", views.workflow_remove, name="workflow_remove"),
    path("phases/<int:pk>/finish/", views.phase_finish, name="workflow_phase_finish"),
]
