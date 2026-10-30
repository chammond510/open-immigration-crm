from django.urls import path

from . import views

urlpatterns = [
    path("", views.field_list, name="custom_field_list"),
    path("new/", views.field_edit, name="custom_field_create"),
    path("<int:pk>/edit/", views.field_edit, name="custom_field_update"),
]
