from django.urls import path

from . import pages

urlpatterns = [
    path("", pages.api_access, name="api_access"),
    path("settings/", pages.api_settings_update, name="api_settings_update"),
    path("tokens/new/", pages.api_token_create, name="api_token_create"),
    path("tokens/<uuid:pk>/revoke/", pages.api_token_revoke, name="api_token_revoke"),
]
