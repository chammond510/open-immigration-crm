from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from crm.comms.models import Communication
from crm.models import IntakeSubmission
from crm.services import audit
from crm.views import staff_required

from .forms import TranslateForm
from .services import (
    TranslationUnavailable,
    language_name,
    translate_communication,
    translate_submission,
)


def back(request, fallback):
    next_url = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect(fallback)


def translate(request, record, translate_record, fallback):
    form = TranslateForm(request.POST, user=request.user)
    if not form.is_valid():
        messages.error(request, "Choose a translation service you may use and a language.")
        return back(request, fallback)
    translator, language = form.chosen(), form.cleaned_data["language"]
    try:
        translate_record(record, translator, language, actor=request.user)
    except TranslationUnavailable as exc:
        messages.error(request, f"Not translated: {exc}")
    else:
        audit(request, "record.translated", record, detail=f"into {language} by {translator.label}")
        messages.success(request, f"Translated into {language_name(language)}.")
    return back(request, fallback)


@staff_required
@require_POST
def translate_communication_view(request, pk):
    communication = get_object_or_404(Communication, pk=pk)
    return translate(request, communication, translate_communication, reverse("communication_list"))


@staff_required
@require_POST
def translate_submission_view(request, pk):
    submission = get_object_or_404(IntakeSubmission, pk=pk)
    return translate(
        request,
        submission,
        translate_submission,
        reverse("intake_submission_detail", args=[submission.pk]),
    )
