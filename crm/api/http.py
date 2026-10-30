"""Request handling shared by every API endpoint: token sign-in, scopes, the
hourly limit, JSON bodies and errors, pagination, and the request log."""

import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from functools import wraps

from django import forms
from django.conf import settings
from django.core.exceptions import RequestDataTooBig, ValidationError
from django.db import transaction
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.csrf import csrf_exempt

from crm.services import client_ip
from crm.two_factor import confirmed_device, firm_requires_two_factor

from . import scopes as api_scopes
from .models import ApiRequestLog, ApiSettings, ApiToken

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
LAST_USED_INTERVAL = timedelta(minutes=1)


class ApiError(Exception):
    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def response(self):
        return JsonResponse(
            {"error": {"code": self.code, "message": self.message, **self.extra}},
            status=self.status,
        )


def authenticate(request):
    api_settings = ApiSettings.load()
    if not api_settings.enabled:
        raise ApiError(
            403, "api_disabled", "The API is turned off. A superuser can turn it on in API access."
        )
    scheme, _, raw_token = request.META.get("HTTP_AUTHORIZATION", "").partition(" ")
    raw_token = raw_token.strip()
    if scheme.lower() != "bearer" or not raw_token:
        raise ApiError(
            401, "unauthorized", "Send an API token in the header Authorization: Bearer <token>."
        )
    token = (
        ApiToken.objects.select_related("user")
        .filter(token_hash=ApiToken.hash_token(raw_token))
        .first()
    )
    if token is None:
        raise ApiError(401, "invalid_token", "The token is not recognized.")
    request.api_token = token
    if token.revoked_at:
        raise ApiError(401, "token_revoked", "The token was revoked.")
    if token.is_expired:
        raise ApiError(401, "token_expired", "The token expired.")
    user = token.user
    if not (user.is_active and user.is_staff):
        raise ApiError(401, "user_inactive", "The token's user can no longer sign in.")
    if not user.is_superuser and not api_settings.staff_tokens_allowed:
        raise ApiError(403, "staff_tokens_off", "Only superusers' tokens are accepted.")
    if firm_requires_two_factor() and confirmed_device(user) is None:
        raise ApiError(
            401,
            "two_factor_required",
            "The firm requires two-factor sign-in. Set it up before using this token.",
        )
    return token


def require_scope(token, resource, method):
    access = api_scopes.READ if method == "GET" else api_scopes.WRITE
    needed = api_scopes.scope(resource, access)
    if not token.has_scope(needed):
        raise ApiError(403, "missing_scope", f"The token does not have the {needed} scope.")
    if resource == "billing" and not token.user.has_perm("billing.access_billing"):
        raise ApiError(403, "no_billing_access", "The token's user does not have billing access.")


def check_hourly_limit(token):
    limit = settings.API_HOURLY_LIMIT
    since = timezone.now() - timedelta(hours=1)
    if ApiRequestLog.objects.filter(token=token, created_at__gte=since).count() >= limit:
        raise ApiError(
            429, "rate_limited", f"The token reached its limit of {limit} requests an hour."
        )


def mark_used(request, token):
    now = timezone.now()
    if token.last_used_at and now - token.last_used_at < LAST_USED_INTERVAL:
        return
    ApiToken.objects.filter(pk=token.pk).update(last_used_at=now, last_used_ip=client_ip(request))


def endpoint(resource=None, methods=("GET",)):
    """Wrap an API view. GET needs the resource's read scope; anything else needs write.

    Session cookies are never used: the token's user becomes ``request.user``, so
    CSRF protection is not needed and does not apply.
    """

    def decorator(view):
        @csrf_exempt
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            started = time.monotonic()
            request.api_token = None
            try:
                if request.method not in methods:
                    raise ApiError(
                        405,
                        "method_not_allowed",
                        f"Use {' or '.join(methods)} here.",
                    )
                token = authenticate(request)
                if resource:
                    require_scope(token, resource, request.method)
                check_hourly_limit(token)
                mark_used(request, token)
                request.user = token.user
                if request.method == "GET":
                    response = view(request, *args, **kwargs)
                else:
                    with transaction.atomic():
                        response = view(request, *args, **kwargs)
            except ApiError as exc:
                response = exc.response()
            except Http404:
                response = ApiError(404, "not_found", "No record has that ID.").response()
            except ValidationError as exc:
                response = ApiError(400, "invalid", " ".join(exc.messages)).response()
            except Exception:
                logger.exception("API request failed")
                response = ApiError(500, "server_error", "The request failed.").response()
            token = request.api_token
            ApiRequestLog.objects.create(
                token=token,
                user=token.user if token else None,
                method=request.method[:10],
                path=request.path[:255],
                status_code=response.status_code,
                duration_ms=int((time.monotonic() - started) * 1000),
                ip_address=client_ip(request),
            )
            if response.status_code == 405:
                response["Allow"] = ", ".join(methods)
            if response.status_code == 429:
                response["Retry-After"] = "60"
            response["Cache-Control"] = "no-store"
            return response

        return wrapper

    return decorator


def not_found(request, rest=""):
    """JSON 404 for unknown paths under the API."""
    message = "There is no API endpoint at this path."
    if not request.path.endswith("/"):
        message += " API paths end with a slash."
    response = ApiError(404, "not_found", message).response()
    response["Cache-Control"] = "no-store"
    return response


def read_json(request):
    if request.content_type != "application/json":
        raise ApiError(
            415, "unsupported_media_type", "Send a JSON body with Content-Type: application/json."
        )
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    too_big = ApiError(413, "too_large", f"The body is over {MAX_BODY_BYTES // 1024} KB.")
    if length > MAX_BODY_BYTES:
        raise too_big
    try:
        body = request.body
    except RequestDataTooBig:
        raise too_big from None
    if len(body) > MAX_BODY_BYTES:
        raise too_big
    try:
        data = json.loads(body or b"null")
    except ValueError:
        raise ApiError(400, "invalid_json", "The body is not valid JSON.") from None
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "The body must be a JSON object.")
    return data


def form_errors(form):
    return {
        ("record" if name == "__all__" else name): [error["message"] for error in errors]
        for name, errors in form.errors.get_json_data().items()
    }


def bind(form_class, data, *, writable, instance=None, **form_kwargs):
    """Validate JSON fields with the same form the web pages use.

    Fields the request leaves out keep the record's current values, or for a new
    record the form's defaults, so a PATCH changes only what it names.
    """
    unknown = sorted(set(data) - set(writable))
    if unknown:
        raise ApiError(
            400,
            "unknown_fields",
            "These fields are unknown or read-only: " + ", ".join(unknown) + ".",
            fields=unknown,
        )
    nested = sorted(name for name, value in data.items() if isinstance(value, (dict, list)))
    if nested:
        raise ApiError(
            400, "invalid", "These fields take a single value: " + ", ".join(nested) + "."
        )
    if issubclass(form_class, forms.BaseModelForm):
        form_kwargs["instance"] = instance
    current = form_class(**form_kwargs)
    values = {name: current[name].value() for name in current.fields}
    values.update({name: "" if value is None else value for name, value in data.items()})
    form = form_class(data=values, **form_kwargs)
    if not form.is_valid():
        raise ApiError(400, "invalid", "Some fields are not valid.", fields=form_errors(form))
    return form


def created(data):
    return JsonResponse(data, status=201)


def paginate(request, queryset, serialize):
    limit = int_param(request, "limit", DEFAULT_LIMIT, maximum=MAX_LIMIT, minimum=1)
    offset = int_param(request, "offset", 0)
    rows = list(queryset[offset : offset + limit + 1])
    next_url = None
    if len(rows) > limit:
        params = request.GET.copy()
        params["offset"] = offset + limit
        next_url = request.build_absolute_uri(f"{request.path}?{params.urlencode()}")
    return JsonResponse({"results": [serialize(row) for row in rows[:limit]], "next": next_url})


def int_param(request, name, default=None, *, minimum=0, maximum=None):
    raw = request.GET.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(400, "invalid_parameter", f"{name} must be a whole number.") from None
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f"from {minimum} to {maximum}" if maximum is not None else f"at least {minimum}"
        raise ApiError(400, "invalid_parameter", f"{name} must be {bounds}.")
    return value


def uuid_param(request, name):
    raw = request.GET.get(name, "").strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise ApiError(400, "invalid_parameter", f"{name} must be a record ID.") from None


def choice_param(request, name, choices):
    raw = request.GET.get(name, "").strip()
    if not raw:
        return None
    if raw not in choices:
        raise ApiError(400, "invalid_parameter", f"{name} must be one of: {', '.join(choices)}.")
    return raw


def bool_param(request, name):
    raw = request.GET.get(name, "").strip().lower()
    if not raw:
        return None
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    raise ApiError(400, "invalid_parameter", f"{name} must be true or false.")


def datetime_param(request, name):
    raw = request.GET.get(name, "").strip()
    if not raw:
        return None
    try:
        value = parse_datetime(raw)
        day = None if value else parse_date(raw)
    except ValueError:
        value = day = None
    if value is None and day is None:
        raise ApiError(
            400,
            "invalid_parameter",
            f"{name} must be an ISO 8601 date or date and time, such as 2026-03-02T09:00Z.",
        )
    if value is None:
        value = datetime.combine(day, datetime.min.time())
    if timezone.is_naive(value):
        value = timezone.make_aware(value)
    return value


def user_param(request, name):
    """A user ID, or "me" for the token's own user."""
    raw = request.GET.get(name, "").strip()
    if not raw:
        return None
    if raw == "me":
        return request.user.pk
    return int_param(request, name, minimum=1)
