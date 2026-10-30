from django.urls import path

from . import views

urlpatterns = [
    path("", views.notice_list, name="notice_list"),
    path("new/", views.notice_create, name="notice_create"),
    path("<uuid:pk>/edit/", views.notice_update, name="notice_update"),
    path("<uuid:pk>/delete/", views.notice_delete, name="notice_delete"),
    path("matters/<uuid:matter_pk>/status/", views.status_record, name="status_record"),
]
