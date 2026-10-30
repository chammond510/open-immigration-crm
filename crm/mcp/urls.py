from django.urls import path

from . import views

urlpatterns = [
    path("", views.settings_page, name="mcp_settings"),
    path(
        "connections/<int:application_pk>/<int:user_pk>/disconnect/",
        views.disconnect,
        name="mcp_disconnect",
    ),
]
