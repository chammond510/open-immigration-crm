from django.urls import path

from . import views

urlpatterns = [
    path("connections/<uuid:pk>/", views.connection_events, name="connection_events"),
]
