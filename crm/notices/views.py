from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from crm.models import Matter

from .forms import CaseNoticeForm, StatusCheckForm
from .models import CaseNotice
from .services import delete_notice, record_status, save_notice

staff_required = user_passes_test(
    lambda user: user.is_active and user.is_staff,
    login_url="login",
)


def matter_notices_url(matter):
    return reverse("matter_detail", args=[matter.pk]) + "#notices"


@staff_required
@require_GET
def notice_list(request):
    query = request.GET.get("q", "").strip()
    kind = request.GET.get("kind", "")
    open_responses = request.GET.get("open") == "responses"
    notices = CaseNotice.objects.select_related("matter", "deadline", "appointment")
    if kind in CaseNotice.Kind.values:
        notices = notices.filter(kind=kind)
    if open_responses:
        notices = notices.filter(
            response_due_date__isnull=False, deadline__is_completed=False
        ).order_by("response_due_date")
    if query:
        notices = notices.filter(
            Q(receipt_number__icontains=query)
            | Q(form_type__icontains=query)
            | Q(matter__matter_number__icontains=query)
            | Q(matter__primary_contact__last_name__icontains=query)
        )
    return render(
        request,
        "notices/notice_list.html",
        {
            "notices": notices[:200],
            "query": query,
            "kinds": CaseNotice.Kind.choices,
            "selected_kind": kind,
            "open_responses": open_responses,
            "today": timezone.localdate(),
        },
    )


@staff_required
@require_http_methods(["GET", "POST"])
def notice_create(request):
    matter = get_object_or_404(Matter, pk=request.GET.get("matter") or request.POST.get("matter"))
    form = CaseNoticeForm(
        request.POST or None, matter=matter, initial={"notice_date": timezone.localdate()}
    )
    if request.method == "POST" and form.is_valid():
        notice = save_notice(form.save(commit=False), request.user)
        messages.success(request, f"{notice.get_kind_display()} logged.")
        return redirect(matter_notices_url(matter))
    return render(request, "notices/notice_form.html", {"form": form, "matter": matter})


@staff_required
@require_http_methods(["GET", "POST"])
def notice_update(request, pk):
    notice = get_object_or_404(CaseNotice.objects.select_related("matter"), pk=pk)
    form = CaseNoticeForm(request.POST or None, instance=notice, matter=notice.matter)
    if request.method == "POST" and form.is_valid():
        save_notice(form.save(commit=False), request.user)
        messages.success(request, "Notice updated; its deadline and appointment were updated too.")
        return redirect(matter_notices_url(notice.matter))
    return render(
        request,
        "notices/notice_form.html",
        {"form": form, "matter": notice.matter, "notice": notice},
    )


@staff_required
@require_POST
def notice_delete(request, pk):
    if not request.user.is_superuser:
        raise PermissionDenied
    notice = get_object_or_404(CaseNotice.objects.select_related("matter"), pk=pk)
    matter = notice.matter
    delete_notice(notice, request.user)
    messages.success(request, "Notice deleted, with any of its open work items.")
    return redirect(matter_notices_url(matter))


@staff_required
@require_POST
def status_record(request, matter_pk):
    matter = get_object_or_404(Matter, pk=matter_pk)
    form = StatusCheckForm(request.POST)
    if not form.is_valid():
        messages.error(request, " ".join(e for errors in form.errors.values() for e in errors))
        return redirect(matter_notices_url(matter))
    try:
        check = record_status(matter, actor=request.user, **form.cleaned_data)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(
            request,
            "Status changed since the last check." if check.changed else "Status recorded.",
        )
    return redirect(matter_notices_url(matter))
