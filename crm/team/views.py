from axes.utils import reset as reset_sign_in_lockout
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth.views import PasswordResetConfirmView
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from crm.comms import system_email
from crm.notifications.forms import PreferenceForm
from crm.notifications.models import Preference
from crm.roles import MANAGE_STAFF, PERMISSIONS, is_staff, permission_required
from crm.services import audit
from crm.two_factor import confirmed_device, reset_two_factor

from . import reset, services
from .forms import DeactivateForm, PasswordResetRequestForm, PersonForm, ProfileForm

staff_required = user_passes_test(is_staff, login_url="login")


def person_or_404(pk):
    return get_object_or_404(get_user_model(), pk=pk, is_staff=True)


def managed_person_or_403(request, pk):
    person = person_or_404(pk)
    if not services.may_manage(request.user, person):
        raise PermissionDenied
    return person


def role_summary(group):
    granted = services.permission_names(group.permissions.all())
    labels = [label for permission, label, _ in PERMISSIONS if permission in granted]
    return ", ".join(labels) or "Everyday work only"


def invited_page(request, person, link, problem):
    response = render(
        request,
        "team/invited.html",
        {"person": person, "link": link, "problem": problem, "days": services.invite_days()},
    )
    response["Cache-Control"] = "no-store"
    return response


@permission_required(MANAGE_STAFF)
@require_GET
def team_list(request):
    people = [
        {
            "person": person,
            "role": services.role_of(person),
            "status": services.status(person),
            "two_factor": confirmed_device(person) is not None,
            "manageable": services.may_manage(request.user, person),
        }
        for person in services.staff()
    ]
    return render(
        request,
        "team/team_list.html",
        {
            "people": people,
            "roles": [(group, role_summary(group)) for group in services.roles()],
        },
    )


@permission_required(MANAGE_STAFF)
@require_http_methods(["GET", "POST"])
def person_add(request):
    form = PersonForm(request.POST or None, actor=request.user)
    if form.is_valid():
        person = form.save()
        audit(request, "user.invited", person, f"role={services.role_of(person) or 'none'}")
        link, problem = services.invite(request, person)
        return invited_page(request, person, link, problem)
    return render(
        request,
        "crm/form_page.html",
        {
            "form": form,
            "title": "Add a person",
            "eyebrow": "Team",
            "submit_label": "Add and invite",
            "cancel_url": reverse("team_list"),
            "prompt_capability": "system_email",
            "prompt_purpose": "to email invitations from here",
        },
    )


@permission_required(MANAGE_STAFF)
@require_http_methods(["GET", "POST"])
def person_detail(request, pk):
    person = person_or_404(pk)
    manageable = services.may_manage(request.user, person)
    form = PersonForm(request.POST or None, instance=person, actor=request.user)
    if request.method == "POST":
        if not manageable:
            raise PermissionDenied
        if form.is_valid():
            before = services.role_of(person)
            person = form.save()
            after = services.role_of(person)
            audit(request, "user.updated", person)
            if before != after:
                audit(
                    request, "user.role_changed", person, f"{before or 'none'} -> {after or 'none'}"
                )
            messages.success(request, f"Saved {person.get_full_name() or person.username}.")
            return redirect("team_detail", pk=person.pk)
    return render(
        request,
        "team/person_detail.html",
        {
            "person": person,
            "form": form,
            "manageable": manageable,
            "is_self": person.pk == request.user.pk,
            "status": services.status(person),
            "invited": services.is_invited(person),
            "two_factor": confirmed_device(person) is not None,
            "role": services.role_of(person),
        },
    )


@permission_required(MANAGE_STAFF)
@require_POST
def person_invite(request, pk):
    person = managed_person_or_403(request, pk)
    if not (person.is_active and services.is_invited(person)):
        messages.error(request, "They have already set a password, so there is nothing to resend.")
        return redirect("team_detail", pk=pk)
    link, problem = services.invite(request, person)
    audit(request, "user.invitation_resent", person)
    return invited_page(request, person, link, problem)


@permission_required(MANAGE_STAFF)
@require_POST
def person_two_factor_reset(request, pk):
    person = managed_person_or_403(request, pk)
    if reset_two_factor(person):
        audit(request, "user.two_factor_reset", person)
        messages.success(
            request,
            f"Two-factor sign-in was reset. {person.get_full_name() or person.username} sets it "
            "up again after signing in with their password.",
        )
    else:
        messages.info(request, "They had not set up two-factor sign-in.")
    return redirect("team_detail", pk=pk)


@permission_required(MANAGE_STAFF)
@require_http_methods(["GET", "POST"])
def person_deactivate(request, pk):
    person = managed_person_or_403(request, pk)
    if not person.is_active:
        return redirect("team_detail", pk=pk)
    form = DeactivateForm(request.POST or None, person=person)
    if form.is_valid():
        successor = form.cleaned_data["successor"]
        moved = services.deactivate(person, successor=successor, actor=request.user)
        audit(
            request,
            "user.deactivated",
            person,
            f"work={moved['work']}, matters={moved['matters']}, contacts={moved['contacts']}, "
            f"to={successor.pk if successor else 'nobody'}",
        )
        handed = ", ".join(
            [
                services.counted(moved["work"], "work item"),
                services.counted(moved["matters"], "matter"),
                services.counted(moved["contacts"], "contact"),
            ]
        )
        receiver = (successor.get_full_name() or successor.username) if successor else "nobody"
        messages.success(
            request,
            f"{person.get_full_name() or person.username} can no longer sign in. "
            f"Handed to {receiver}: {handed}.",
        )
        return redirect("team_list")
    return render(
        request,
        "team/deactivate.html",
        {
            "person": person,
            "form": form,
            "counts": {
                name: records.count() for name, records in services.open_assignments(person).items()
            },
            "connections": list(services.personal_connections(person)),
        },
    )


@permission_required(MANAGE_STAFF)
@require_POST
def person_reactivate(request, pk):
    person = managed_person_or_403(request, pk)
    person.is_active = True
    person.save(update_fields=["is_active"])
    audit(request, "user.reactivated", person)
    messages.success(
        request,
        f"{person.get_full_name() or person.username} can sign in again. Their work was not "
        "given back, and personal connections must be connected again.",
    )
    return redirect("team_detail", pk=pk)


@staff_required
@require_http_methods(["GET", "POST"])
def profile(request):
    form = ProfileForm(request.POST or None, instance=request.user)
    alerts = PreferenceForm(
        request.POST or None, instance=Preference.for_user(request.user), prefix="alerts"
    )
    if request.method == "POST" and form.is_valid() and alerts.is_valid():
        form.save()
        alerts.save()
        audit(request, "user.profile_updated", request.user)
        messages.success(request, "Your profile is saved.")
        return redirect("profile")
    return render(
        request,
        "team/profile.html",
        {"form": form, "alerts": alerts, "can_email": system_email.sender() is not None},
    )


class ChoosePasswordView(PasswordResetConfirmView):
    """Where someone chooses a password through a link the CRM made for them."""

    template_name = "team/choose_password.html"
    reset_url_token = "choose-password"
    audit_action = ""

    def get_user(self, uidb64):
        person = super().get_user(uidb64)
        if person is None or not (person.is_active and person.is_staff):
            return None
        return person

    def form_valid(self, form):
        response = super().form_valid(form)
        audit(self.request, self.audit_action, form.user)
        return response


class WelcomeView(ChoosePasswordView):
    """Where an invited person chooses their first password."""

    token_generator = services.invite_tokens
    success_url = reverse_lazy("welcome_done")
    audit_action = "user.password_set_from_invitation"
    extra_context = {"eyebrow": "Welcome", "heading": "Choose a password"}


class ResetPasswordView(ChoosePasswordView):
    """Where someone who asked by email chooses a new password."""

    token_generator = reset.reset_tokens
    success_url = reverse_lazy("password_reset_complete")
    audit_action = "user.password_reset"
    extra_context = {"eyebrow": "Password reset", "heading": "Choose a new password"}

    def form_valid(self, form):
        response = super().form_valid(form)
        # A sign-in lockout from guessing the old password should not outlast it.
        reset_sign_in_lockout(username=form.user.get_username())
        return response


def password_saved(request):
    return render(request, "team/password_saved.html")


@require_http_methods(["GET", "POST"])
def password_reset(request):
    if not reset.offered():
        return render(request, "team/password_reset_unavailable.html", status=404)
    form = PasswordResetRequestForm(request.POST or None)
    if form.is_valid():
        key = reset.address_key(form.cleaned_data["email"])
        client_limited, address_limited = reset.limits_reached(request, key)
        if client_limited:
            return render(request, "team/password_reset_limited.html", status=429)
        audit(request, reset.REQUESTED, detail=key)
        if not address_limited:
            site_url = request.build_absolute_uri("/").rstrip("/")
            transaction.on_commit(lambda: reset.send_links.enqueue(key, site_url))
        return redirect("password_reset_sent")
    return render(request, "team/password_reset.html", {"form": form})


@require_GET
def password_reset_sent(request):
    minutes = int(reset.LINK_LIFETIME.total_seconds() // 60)
    return render(request, "team/password_reset_sent.html", {"minutes": minutes})
