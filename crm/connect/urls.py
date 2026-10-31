from django.urls import path

from . import pages

urlpatterns = [
    path("", pages.catalog, name="connections"),
    path("add/<str:key>/", pages.connector_detail, name="connector_detail"),
    path("add/<str:key>/app/", pages.connector_app, name="connector_app"),
    path("oauth/callback/", pages.connection_oauth_callback, name="connection_oauth_callback"),
    path("<uuid:pk>/", pages.connection_detail, name="connection_detail"),
    path("<uuid:pk>/sign-in/", pages.connection_oauth_start, name="connection_oauth_start"),
    path("<uuid:pk>/sign-in/paste/", pages.connection_oauth_paste, name="connection_oauth_paste"),
    path("<uuid:pk>/acknowledge/", pages.connection_acknowledge, name="connection_acknowledge"),
    path("<uuid:pk>/test/", pages.connection_test, name="connection_test"),
    path("<uuid:pk>/sync/", pages.connection_sync, name="connection_sync"),
    path("<uuid:pk>/settings/", pages.connection_settings, name="connection_settings"),
    path("<uuid:pk>/config/", pages.connection_config, name="connection_config"),
    path("<uuid:pk>/webhooks/", pages.connection_webhooks, name="connection_webhooks"),
    path("<uuid:pk>/disconnect/", pages.connection_disconnect, name="connection_disconnect"),
]
