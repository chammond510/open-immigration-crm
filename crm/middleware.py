from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect
from django.urls import Resolver404, resolve

from .two_factor import SESSION_VERIFIED_KEY, confirmed_device, firm_requires_two_factor


class TwoFactorMiddleware:
    """Hold signed-in staff to the two-factor rules on every request.

    An enrolled user whose session skipped the second step (for example through
    another login form) is signed out. When the firm requires two-factor
    sign-in, unenrolled staff can reach only the setup page and sign-out.
    """

    ENROLLMENT_URL_NAMES = {"two_factor_settings", "logout"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and not request.session.get(SESSION_VERIFIED_KEY)
        ):
            if confirmed_device(user) is not None:
                logout(request)
                return redirect_to_login(request.get_full_path())
            if firm_requires_two_factor() and self.url_name(request) not in (
                self.ENROLLMENT_URL_NAMES
            ):
                if request.method == "GET":
                    messages.info(
                        request, "Your firm requires two-factor sign-in. Set it up to continue."
                    )
                return redirect("two_factor_settings")
        return self.get_response(request)

    @staticmethod
    def url_name(request):
        try:
            return resolve(request.path_info).url_name
        except Resolver404:
            return None


CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
    "script-src 'self'; font-src 'self'; object-src 'none'; "
    "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)


class SecurityHeadersMiddleware:
    """Small, dependency-free baseline for browser security headers."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.setdefault("Referrer-Policy", "same-origin")
        response.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        return response
