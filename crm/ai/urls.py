from django.urls import path

from . import views

urlpatterns = [
    path("", views.settings_page, name="ai_settings"),
    path("test/", views.connection_test, name="ai_connection_test"),
]
