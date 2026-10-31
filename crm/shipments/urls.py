from django.urls import path

from . import views

urlpatterns = [
    path("matters/<uuid:pk>/new/", views.shipment_create, name="shipment_create"),
    path("<uuid:pk>/", views.shipment_update, name="shipment_update"),
    path("<uuid:pk>/delete/", views.shipment_delete, name="shipment_delete"),
]
