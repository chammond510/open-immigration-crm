from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from crm.models import Matter

from . import services
from .forms import SendForm
from .models import SignatureRequest

staff_required = user_passes_test(lambda user: user.is_active and user.is_staff, login_url="login")


def matter_signatures(matter):
    return f"{reverse('matter_detail', args=[matter.pk])}#signatures"


@staff_required
@require_POST
def send(request, pk):
    matter = get_object_or_404(Matter, pk=pk)
    form = SendForm(request.POST, matter=matter, user=request.user)
    if not form.is_valid():
        messages.error(request, "Choose a PDF, at least one signer, and a service to send it.")
        return redirect(matter_signatures(matter))
    try:
        sent = services.send(
            form.cleaned_data["connection"],
            form.cleaned_data["document"],
            signers=form.cleaned_data["signers"],
            title=form.cleaned_data["title"],
            message=form.cleaned_data["message"],
            actor=request.user,
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, f"Sent for signature through {sent.connection.label}.")
    return redirect(matter_signatures(matter))


@staff_required
@require_POST
def cancel(request, pk):
    signature_request = get_object_or_404(SignatureRequest, pk=pk)
    try:
        services.cancel(signature_request, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "Signature request cancelled.")
    return redirect(matter_signatures(signature_request.matter))
