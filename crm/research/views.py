import uuid

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from crm.models import Matter
from crm.services import audit

from . import services
from .forms import AuthorityForm, SearchForm
from .models import Authority, Search

staff_required = user_passes_test(lambda user: user.is_active and user.is_staff, login_url="login")


def matter_research(matter):
    return f"{reverse('matter_detail', args=[matter.pk])}#research"


@staff_required
@require_POST
def search(request, pk):
    matter = get_object_or_404(Matter, pk=pk)
    form = SearchForm(request.POST, user=request.user)
    if not form.is_valid():
        problems = form.non_field_errors() or ["Write what to search for."]
        messages.error(request, " ".join(problems))
        return redirect(matter_research(matter))
    found = services.search(
        matter,
        query=form.cleaned_data["query"],
        connection=form.cleaned_data["connection"],
        scope=form.cleaned_data["scope"],
        actor=request.user,
    )
    return redirect("research_search_detail", pk=found.pk)


@staff_required
@require_GET
def search_detail(request, pk):
    found = get_object_or_404(
        Search.objects.select_related("matter__primary_contact", "connection"), pk=pk
    )
    saved = set(
        found.matter.authorities.filter(
            connector_key=found.connection.connector_key if found.connection else ""
        ).values_list("external_id", flat=True)
    )
    form = SearchForm(
        user=request.user,
        initial={"query": found.query, "target": f"{found.connection_id}:{found.scope}"},
    )
    return render(
        request,
        "research/search_detail.html",
        {
            "search": found,
            "matter": found.matter,
            "results": [
                (index, result, result["id"] in saved) for index, result in enumerate(found.results)
            ],
            "form": form,
        },
    )


@staff_required
@require_POST
def save(request, pk):
    found = get_object_or_404(Search.objects.select_related("matter", "connection"), pk=pk)
    try:
        index = int(request.POST.get("index", ""))
        found.results[index]  # noqa: B018 - checks the index exists
    except (ValueError, IndexError):
        messages.error(request, "That result is no longer available. Search again.")
        return redirect("research_search_detail", pk=found.pk)
    authority, created = services.save(
        found, index, actor=request.user, note=request.POST.get("note", "").strip()[:5000]
    )
    if created:
        audit(request, "authority.saved", authority)
        messages.success(request, f"Saved {authority} to {found.matter.matter_number}.")
    else:
        messages.info(request, f"{authority} was already saved to this matter.")
    return redirect(f"{reverse('research_search_detail', args=[found.pk])}#result-{index}")


@staff_required
@require_POST
def add_by_hand(request, pk):
    matter = get_object_or_404(Matter, pk=pk)
    form = AuthorityForm(request.POST)
    if not form.is_valid():
        problems = [error for errors in form.errors.values() for error in errors]
        messages.error(request, " ".join(problems))
        return redirect(matter_research(matter))
    authority = form.save(commit=False)
    authority.matter = matter
    authority.external_id = uuid.uuid4().hex
    authority.saved_by = request.user
    authority.save()
    audit(request, "authority.saved", authority, "Added by hand")
    messages.success(request, f"Saved {authority} to {matter.matter_number}.")
    return redirect(matter_research(matter))


@staff_required
@require_POST
def remove(request, pk):
    authority = get_object_or_404(Authority.objects.select_related("matter"), pk=pk)
    matter = authority.matter
    audit(request, "authority.removed", authority, str(authority)[:500])
    authority.delete()
    messages.success(request, "Authority removed from the matter.")
    return redirect(matter_research(matter))
