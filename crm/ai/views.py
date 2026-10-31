from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from crm.roles import MANAGE_CONNECTIONS, can, permission_required, require
from crm.services import audit

from . import features
from .client import AIUnavailable, Message, ai_connections, generate
from .forms import AISettingsForm, ConnectionTestForm
from .models import AIRequest, AISettings, BackgroundRun

staff_required = user_passes_test(lambda user: user.is_active and user.is_staff, login_url="login")
TEST_PROMPT = "This is a connection check. Reply with the single word: ready."


@staff_required
@require_http_methods(["GET", "POST"])
def settings_page(request):
    ai_settings = AISettings.load()
    form = AISettingsForm(request.POST or None, ai_settings=ai_settings)
    manages = can(request.user, MANAGE_CONNECTIONS)
    if request.method == "POST":
        require(request.user, MANAGE_CONNECTIONS)
        if form.is_valid():
            ai_settings.enabled = form.cleaned_data["enabled"]
            ai_settings.background_connection = form.cleaned_data["background_connection"]
            ai_settings.features = form.feature_values()
            ai_settings.updated_by = request.user
            ai_settings.save()
            turned_on = sorted(key for key, on in ai_settings.features.items() if on)
            audit(
                request,
                "ai_settings.updated",
                detail=f"enabled={ai_settings.enabled}, features={','.join(turned_on) or 'none'}",
            )
            messages.success(
                request, "AI features are on." if ai_settings.enabled else "AI features are off."
            )
        return redirect("ai_settings")
    requests = AIRequest.objects.select_related("connection", "actor")
    if not manages:
        requests = requests.filter(actor=request.user)
    requests = list(requests[:50])
    for item in requests:
        item.feature_label = features.label_for(item.feature)
    return render(
        request,
        "ai/settings.html",
        {
            "ai_settings": ai_settings,
            "form": form,
            "features": [
                (feature, ai_settings.feature_on(feature.key))
                for feature in features.all_features()
            ],
            "background_runs": [
                (feature, BackgroundRun.objects.filter(feature=feature.key).first())
                for feature in features.all_features()
                if feature.background
            ],
            "test_form": ConnectionTestForm(user=request.user),
            "connections": ai_connections(request.user),
            "recent_requests": requests,
        },
    )


@permission_required(MANAGE_CONNECTIONS)
@require_POST
def connection_test(request):
    """Send a fixed prompt with no client data, to check a connection works."""
    form = ConnectionTestForm(request.POST, user=request.user)
    if not form.is_valid():
        messages.error(request, "Choose an AI connection to test.")
        return redirect("ai_settings")
    connection = form.cleaned_data["connection"]
    try:
        reply = generate(
            feature=features.CONNECTION_TEST,
            messages=[Message(role="user", content=TEST_PROMPT)],
            actor=request.user,
            connection=connection,
            max_tokens=50,
        )
    except AIUnavailable as exc:
        messages.error(request, f"The test did not run: {exc}")
    else:
        messages.success(
            request,
            f"{connection.label} answered with {reply.model or 'its model'}: "
            f"“{reply.text.strip()[:200]}” ({reply.input_tokens or 0} tokens in, "
            f"{reply.output_tokens or 0} out).",
        )
    return redirect("ai_settings")
