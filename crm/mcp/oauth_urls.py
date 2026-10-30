"""django-oauth-toolkit routes for MCP sign-in, mounted at the site root.

Only what MCP clients need is exposed: the two discovery documents, consent,
token, revocation, and client registration. The toolkit's application
management, device flow, and OpenID Connect views are not mounted.
"""

from django.urls import path
from oauth2_provider import views as oauth_views

from .oauth import ConsentView

app_name = "oauth2_provider"

urlpatterns = [
    path(
        ".well-known/oauth-authorization-server",
        oauth_views.OAuthServerMetadataView.as_view(),
        name="oauth-server-metadata",
    ),
    path(
        ".well-known/oauth-protected-resource",
        oauth_views.OAuthProtectedResourceMetadataView.as_view(),
        name="oauth-resource-metadata",
    ),
    path(
        ".well-known/oauth-protected-resource/<path:resource_path>",
        oauth_views.OAuthProtectedResourceMetadataView.as_view(),
        name="oauth-resource-metadata-path",
    ),
    path("o/authorize/", ConsentView.as_view(), name="authorize"),
    path("o/token/", oauth_views.TokenView.as_view(), name="token"),
    path("o/revoke_token/", oauth_views.RevokeTokenView.as_view(), name="revoke-token"),
    path(
        "o/register/",
        oauth_views.DynamicClientRegistrationView.as_view(),
        name="dcr-register",
    ),
    path(
        "o/register/<str:client_id>/",
        oauth_views.DynamicClientRegistrationManagementView.as_view(),
        name="dcr-register-management",
    ),
]
