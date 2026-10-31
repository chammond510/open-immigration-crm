from django.urls import path

from . import views

urlpatterns = [
    path("matters/<uuid:pk>/send/", views.send, name="signature_send"),
    path("<uuid:pk>/cancel/", views.cancel, name="signature_cancel"),
]
