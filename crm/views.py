import csv
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth.views import LoginView, PasswordChangeView
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Case, Count, F, IntegerField, Value, When
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .comms import system_email
from .connect.sign_in import sign_in_connections
from .forms import (
    ActivityForm,
    ChecklistItemForm,
    ContactForm,
    DocumentUploadForm,
    FirmProfileForm,
    FirstRunSetupForm,
    IntakeFormConfigForm,
    IntakeInviteForm,
    MatterForm,
    MatterPartyForm,
    PublicIntakeForm,
    TwoFactorCodeForm,
    TwoFactorDisableForm,
    WorkItemForm,
)
from .models import (
    Activity,
    ChecklistItem,
    Contact,
    Document,
    FirmProfile,
    IntakeForm,
    IntakeInvite,
    IntakeSubmission,
    Matter,
    MatterParty,
    TimeEntry,
    TwoFactorDevice,
    WorkItem,
)
from .roles import (
    DELETE_RECORDS,
    EXPORT_RECORDS,
    MANAGE_FIRM_SETTINGS,
    permission_required,
)
from .search import CONTACT_SEARCH_FIELDS, MATTER_SEARCH_FIELDS, words_match
from .services import add_activity, audit
from .two_factor import (
    ACCEPTED,
    ACCEPTED_RECOVERY,
    LOCKED,
    SESSION_PENDING_KEY,
    SESSION_VERIFIED_KEY,
    check_second_factor,
    confirm_device,
    confirmed_device,
    firm_requires_two_factor,
    format_secret,
    generate_secret,
    issuer_name,
    provisioning_uri,
    qr_code_data_uri,
    remaining_recovery_codes,
    replace_recovery_codes,
    reset_two_factor,
)
from .workflows.models import MatterPhase

staff_required = user_passes_test(
    lambda user: user.is_active and user.is_staff,
    login_url="login",
)


def csv_safe(value):
    text = str(value or "")
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{text}"
    return text


def optional_record(model, raw_pk):
    """Resolve an optional record id from a query string, ignoring bad values."""
    if not raw_pk:
        return None
    try:
        return model.objects.filter(pk=raw_pk).first()
    except (ValidationError, ValueError):
        return None


def installation_has_accounts():
    return get_user_model().objects.exists()


def time_total_display(entries):
    total_minutes = sum(int(entry.duration.total_seconds() // 60) for entry in entries)
    return f"{total_minutes // 60}:{total_minutes % 60:02d}"


class FirstRunAwareLoginView(LoginView):
    """Send a pristine installation to first-run setup instead of a dead end."""

    template_name = "registration/login.html"

    def dispatch(self, request, *args, **kwargs):
        if not installation_has_accounts():
            return redirect("first_run_setup")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        return {
            **super().get_context_data(**kwargs),
            "sign_in_connections": sign_in_connections(),
            "password_reset_offered": system_email.sender() is not None,
        }

    def form_valid(self, form):
        """Hold enrolled users at the second step instead of signing them in."""
        user = form.get_user()
        if confirmed_device(user) is None:
            return super().form_valid(form)
        self.request.session.cycle_key()
        self.request.session[SESSION_PENDING_KEY] = {
            "user_id": user.pk,
            "backend": user.backend,
            "next": self.get_redirect_url(),
            "started_at": timezone.now().timestamp(),
        }
        return redirect("two_factor_verify")


def lockout_minutes():
    return int(settings.AXES_COOLOFF_TIME.total_seconds() // 60)


def lockout_message():
    return f"Too many incorrect codes. Try again in {lockout_minutes()} minutes."


@require_http_methods(["GET", "POST"])
def two_factor_verify(request):
    """Second sign-in step: an authenticator code or a single-use recovery code."""
    pending = request.session.get(SESSION_PENDING_KEY) or {}
    age = timezone.now().timestamp() - pending.get("started_at", 0)
    user = None
    if pending and age <= settings.TWO_FACTOR_LOGIN_TIMEOUT.total_seconds():
        user = get_user_model().objects.filter(pk=pending["user_id"], is_active=True).first()
    if user is None:
        request.session.pop(SESSION_PENDING_KEY, None)
        if pending:
            messages.error(request, "Your sign-in expired. Sign in again.")
        return redirect("login")

    form = TwoFactorCodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        outcome = check_second_factor(user, form.cleaned_data["code"])
        if outcome in (ACCEPTED, ACCEPTED_RECOVERY):
            del request.session[SESSION_PENDING_KEY]
            login(request, user, backend=pending["backend"])
            request.session[SESSION_VERIFIED_KEY] = True
            if outcome == ACCEPTED_RECOVERY:
                remaining = remaining_recovery_codes(user)
                audit(request, "user.recovery_code_used", user, f"{remaining} remaining")
                messages.info(
                    request,
                    f"You signed in with a recovery code. {remaining} unused codes remain; "
                    "create a new set under Two-factor sign-in if you are running low.",
                )
            else:
                audit(request, "user.two_factor_verified", user)
            return redirect(pending["next"] or settings.LOGIN_REDIRECT_URL)
        if outcome == LOCKED:
            request.session.pop(SESSION_PENDING_KEY, None)
            audit(request, "user.two_factor_locked", user)
            messages.error(
                request,
                "Too many incorrect codes. Two-factor sign-in is locked for "
                f"{lockout_minutes()} minutes.",
            )
            return redirect("login")
        audit(request, "user.two_factor_failed", user)
        form.add_error("code", "That code was not accepted. Codes change every 30 seconds.")
    return render(request, "registration/two_factor_verify.html", {"form": form})


@staff_required
@require_http_methods(["GET", "POST"])
def two_factor_settings(request):
    device, _ = TwoFactorDevice.objects.get_or_create(
        user=request.user, defaults={"secret": generate_secret()}
    )
    if device.is_confirmed:
        return render(
            request,
            "crm/two_factor_settings.html",
            {
                "device": device,
                "remaining_codes": remaining_recovery_codes(request.user),
                "firm_requires": firm_requires_two_factor(),
                "regenerate_form": TwoFactorCodeForm(prefix="regenerate"),
                "disable_form": TwoFactorDisableForm(prefix="disable"),
            },
        )

    form = TwoFactorCodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        codes = confirm_device(device, form.cleaned_data["code"])
        if codes is not None:
            request.session.cycle_key()
            request.session[SESSION_VERIFIED_KEY] = True
            audit(request, "user.two_factor_enabled", request.user)
            return render(
                request,
                "crm/two_factor_recovery_codes.html",
                {"codes": codes, "first_setup": True},
            )
        form.add_error(
            "code", "That code did not match. Check the time on your phone and use the newest code."
        )
    uri = provisioning_uri(device.secret, account=request.user.get_username(), issuer=issuer_name())
    return render(
        request,
        "crm/two_factor_setup.html",
        {
            "form": form,
            "qr_code": qr_code_data_uri(uri),
            "secret": format_secret(device.secret),
            "firm_requires": firm_requires_two_factor(),
        },
    )


@staff_required
@require_POST
def two_factor_recovery_codes(request):
    form = TwoFactorCodeForm(request.POST, prefix="regenerate")
    outcome = check_second_factor(request.user, form.data.get("regenerate-code", ""))
    if form.is_valid() and outcome in (ACCEPTED, ACCEPTED_RECOVERY):
        codes = replace_recovery_codes(request.user)
        audit(request, "user.recovery_codes_replaced", request.user)
        return render(request, "crm/two_factor_recovery_codes.html", {"codes": codes})
    if outcome == LOCKED:
        messages.error(request, lockout_message())
    else:
        messages.error(
            request, "That code was not accepted, so your recovery codes did not change."
        )
    return redirect("two_factor_settings")


@staff_required
@require_POST
def two_factor_disable(request):
    if firm_requires_two_factor():
        messages.error(
            request, "Your firm requires two-factor sign-in, so it cannot be turned off."
        )
        return redirect("two_factor_settings")
    form = TwoFactorDisableForm(request.POST, prefix="disable")
    # Check the rate-limited code before the password so the form cannot be
    # used to guess passwords.
    outcome = check_second_factor(request.user, form.data.get("disable-code", ""))
    if (
        form.is_valid()
        and outcome in (ACCEPTED, ACCEPTED_RECOVERY)
        and request.user.check_password(form.cleaned_data["password"])
    ):
        reset_two_factor(request.user)
        request.session.pop(SESSION_VERIFIED_KEY, None)
        audit(request, "user.two_factor_disabled", request.user)
        messages.success(request, "Two-factor sign-in is off for your account.")
        return redirect("dashboard")
    if outcome == LOCKED:
        messages.error(request, lockout_message())
    else:
        messages.error(
            request, "Your password or code was not accepted, so two-factor sign-in is still on."
        )
    return redirect("two_factor_settings")


@require_http_methods(["GET", "POST"])
def first_run_setup(request):
    """One-time administrator creation; disabled once any account exists."""
    if installation_has_accounts():
        return redirect("login")
    form = FirstRunSetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            firm = FirmProfile.objects.select_for_update().filter(pk=1).first()
            if firm is None:
                FirmProfile.objects.create(pk=1)
                FirmProfile.objects.select_for_update().get(pk=1)
            if installation_has_accounts():
                return redirect("login")
            user = form.save(commit=False)
            user.is_staff = True
            user.is_superuser = True
            user.save()
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        audit(request, "user.first_admin_created", user)
        messages.success(request, "Administrator account created.")
        return redirect("dashboard")
    return render(request, "crm/first_run_setup.html", {"form": form})


# Most pressing first; the model's own ordering sorts priorities alphabetically.
WORK_ORDER = (
    "is_completed",
    F("scheduled_for").asc(nulls_last=True),
    Case(
        When(priority=WorkItem.Priority.URGENT, then=Value(0)),
        When(priority=WorkItem.Priority.HIGH, then=Value(1)),
        When(priority=WorkItem.Priority.NORMAL, then=Value(2)),
        default=Value(3),
        output_field=IntegerField(),
    ),
    "title",
)


@staff_required
@require_GET
def dashboard(request):
    """The signed-in person's own work, or the whole firm's with ?view=firm."""
    today = timezone.localdate()
    firm_wide = request.GET.get("view") == "firm"
    work, matters, contacts = WorkItem.objects.all(), Matter.objects.all(), Contact.objects.all()
    if not firm_wide:
        work, matters, contacts = (
            records.filter(assigned_to=request.user) for records in (work, matters, contacts)
        )
    open_work = (
        work.filter(is_completed=False)
        .select_related("contact", "matter__primary_contact", "assigned_to")
        .order_by(*WORK_ORDER)[:12]
    )
    recent_matters = matters.select_related("primary_contact", "assigned_to")[:8]
    stage_rows = contacts.values("stage").annotate(total=Count("id"))
    stage_counts = {row["stage"]: row["total"] for row in stage_rows}
    pipeline = [
        {"key": key, "label": label, "count": stage_counts.get(key, 0)}
        for key, label in Contact.Stage.choices
    ]
    context = {
        "today": today,
        "firm_wide": firm_wide,
        # Lists the figures link to, narrowed to the same people.
        "owner_query": "" if firm_wide else "owner=mine",
        "open_matters": matters.exclude(status=Matter.Status.CLOSED).count(),
        "active_contacts": contacts.exclude(
            stage__in=[Contact.Stage.CLOSED, Contact.Stage.LOST]
        ).count(),
        "overdue_count": work.filter(
            is_completed=False,
            scheduled_for__lt=timezone.now(),
        ).count(),
        "open_work": open_work,
        "recent_matters": recent_matters,
        "pipeline": pipeline,
    }
    return render(request, "crm/dashboard.html", context)


@staff_required
@require_GET
def contact_list(request):
    query = request.GET.get("q", "").strip()
    stage = request.GET.get("stage", "").strip()
    owner = request.GET.get("owner", "all").strip()
    contacts = Contact.objects.select_related("assigned_to")
    if query:
        contacts = contacts.filter(words_match(query, CONTACT_SEARCH_FIELDS))
    if stage in Contact.Stage.values:
        contacts = contacts.filter(stage=stage)
    if owner == "mine":
        contacts = contacts.filter(assigned_to=request.user)
    return render(
        request,
        "crm/contact_list.html",
        {
            "contacts": contacts[:250],
            "limit": 250,
            "query": query,
            "selected_stage": stage,
            "owner": owner,
        },
    )


@staff_required
@require_GET
def pipeline(request):
    contacts = Contact.objects.select_related("assigned_to").order_by("-updated_at")
    columns = [
        {
            "key": key,
            "label": label,
            "contacts": [contact for contact in contacts if contact.stage == key],
        }
        for key, label in Contact.Stage.choices
    ]
    return render(request, "crm/pipeline.html", {"columns": columns})


@staff_required
@require_http_methods(["GET", "POST"])
def contact_create(request):
    initial = {"assigned_to": request.user}
    for name in ("phone", "email"):
        if request.GET.get(name):
            initial[name] = request.GET[name][:254]
    form = ContactForm(request.POST or None, initial=initial)
    if form.is_valid():
        contact = form.save()
        add_activity(
            request=request,
            contact=contact,
            subject="Contact created",
            kind=Activity.Kind.SYSTEM,
        )
        audit(request, "contact.created", contact)
        messages.success(request, "Contact created.")
        return redirect("contact_detail", pk=contact.pk)
    return render(
        request,
        "crm/form_page.html",
        {
            "form": form,
            "title": "New contact",
            "submit_label": "Create contact",
            "eyebrow": "Contacts",
            "cancel_url": reverse("contact_list"),
        },
    )


@staff_required
@require_GET
def contact_detail(request, pk):
    contact = get_object_or_404(Contact.objects.select_related("assigned_to"), pk=pk)
    context = {
        "contact": contact,
        "matters": contact.matters.select_related("assigned_to"),
        "activities": contact.activities.select_related("created_by")[:30],
        "work_items": contact.work_items.filter(is_completed=False)
        .select_related("matter", "assigned_to")
        .order_by(*WORK_ORDER)[:20],
        "time_entries": contact.time_entries.select_related("user")[:50],
        "time_total": time_total_display(contact.time_entries.all()),
        "activity_form": ActivityForm(
            instance=Activity(contact=contact),
            initial={"occurred_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M")},
        ),
    }
    return render(request, "crm/contact_detail.html", context)


@staff_required
@require_http_methods(["GET", "POST"])
def contact_update(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    before_stage = contact.stage
    form = ContactForm(request.POST or None, instance=contact)
    if form.is_valid():
        contact = form.save()
        if contact.stage != before_stage:
            add_activity(
                request=request,
                contact=contact,
                subject=f"Pipeline stage changed to {contact.get_stage_display()}",
                kind=Activity.Kind.STATUS,
            )
        audit(request, "contact.updated", contact)
        messages.success(request, "Contact updated.")
        return redirect("contact_detail", pk=contact.pk)
    return render(
        request,
        "crm/form_page.html",
        {
            "form": form,
            "title": "Edit contact",
            "submit_label": "Save changes",
            "eyebrow": contact.full_name,
            "cancel_url": reverse("contact_detail", args=[contact.pk]),
        },
    )


@staff_required
@require_POST
def contact_add_activity(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    form = ActivityForm(request.POST, instance=Activity(contact=contact))
    if form.is_valid():
        activity = form.save(commit=False)
        activity.contact = contact
        activity.created_by = request.user
        activity.save()
        audit(request, "activity.created", activity)
        messages.success(request, "Activity added.")
    else:
        messages.error(request, "Review the activity fields and try again.")
    return redirect("contact_detail", pk=contact.pk)


@staff_required
@require_GET
def matter_list(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    matters = Matter.objects.select_related("primary_contact", "assigned_to")
    if query:
        matters = matters.filter(words_match(query, MATTER_SEARCH_FIELDS))
    if status in Matter.Status.values:
        matters = matters.filter(status=status)
    case_type = request.GET.get("type", "").strip()
    if case_type in Matter.CaseType.values:
        matters = matters.filter(case_type=case_type)
    owner = request.GET.get("owner", "all").strip()
    if owner == "mine":
        matters = matters.filter(assigned_to=request.user)
    return render(
        request,
        "crm/matter_list.html",
        {
            "matters": matters[:250],
            "limit": 250,
            "query": query,
            "selected_status": status,
            "selected_type": case_type,
            "owner": owner,
        },
    )


@staff_required
@require_http_methods(["GET", "POST"])
def matter_create(request):
    initial = {"assigned_to": request.user}
    contact = optional_record(Contact, request.GET.get("contact", "").strip())
    if contact:
        initial["primary_contact"] = contact
    form = MatterForm(request.POST or None, initial=initial)
    if form.is_valid():
        matter = form.save()
        add_activity(
            request=request,
            matter=matter,
            contact=matter.primary_contact,
            subject=f"Matter {matter.matter_number} created",
            kind=Activity.Kind.SYSTEM,
        )
        audit(request, "matter.created", matter)
        messages.success(request, f"Matter {matter.matter_number} created.")
        return redirect("matter_detail", pk=matter.pk)
    return render(
        request,
        "crm/form_page.html",
        {
            "form": form,
            "title": "New matter",
            "submit_label": "Create matter",
            "eyebrow": "Matters",
            "cancel_url": reverse("matter_list"),
        },
    )


@staff_required
@require_GET
def matter_detail(request, pk):
    matter = get_object_or_404(
        Matter.objects.select_related("primary_contact", "assigned_to"),
        pk=pk,
    )
    context = {
        "matter": matter,
        "parties": matter.parties.select_related("contact"),
        "activities": matter.activities.select_related("created_by")[:30],
        "work_items": matter.work_items.filter(is_completed=False)
        .select_related("contact", "assigned_to")
        .order_by(*WORK_ORDER)[:20],
        "documents": matter.documents.select_related("contact", "uploaded_by"),
        "time_entries": matter.time_entries.select_related("user")[:50],
        "time_total": time_total_display(matter.time_entries.all()),
        "party_form": MatterPartyForm(auto_id="party_%s"),
        "activity_form": ActivityForm(
            instance=Activity(contact=matter.primary_contact, matter=matter),
            initial={"occurred_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M")},
        ),
        "document_form": DocumentUploadForm(
            auto_id="document_%s", initial={"contact": matter.primary_contact}
        ),
    }
    return render(request, "crm/matter_detail.html", context)


@staff_required
@require_http_methods(["GET", "POST"])
def matter_update(request, pk):
    matter = get_object_or_404(Matter, pk=pk)
    before_status = matter.status
    form = MatterForm(request.POST or None, instance=matter)
    if form.is_valid():
        matter = form.save()
        if matter.status != before_status:
            add_activity(
                request=request,
                matter=matter,
                contact=matter.primary_contact,
                subject=f"Matter status changed to {matter.get_status_display()}",
                kind=Activity.Kind.STATUS,
            )
        audit(request, "matter.updated", matter)
        messages.success(request, "Matter updated.")
        return redirect("matter_detail", pk=matter.pk)
    return render(
        request,
        "crm/form_page.html",
        {
            "form": form,
            "title": f"Edit {matter.matter_number}",
            "submit_label": "Save changes",
            "eyebrow": "Matters",
            "cancel_url": reverse("matter_detail", args=[matter.pk]),
        },
    )


@staff_required
@require_POST
def matter_add_party(request, pk):
    matter = get_object_or_404(Matter, pk=pk)
    form = MatterPartyForm(request.POST)
    if form.is_valid():
        party = form.save(commit=False)
        party.matter = matter
        try:
            party.save()
        except IntegrityError:
            messages.error(request, "That party and role are already linked to this matter.")
        else:
            audit(request, "matter_party.created", party)
            messages.success(request, "Matter party added.")
    else:
        messages.error(request, "Review the party fields and try again.")
    return redirect("matter_detail", pk=matter.pk)


@permission_required(DELETE_RECORDS)
@require_POST
def matter_remove_party(request, pk):
    party = get_object_or_404(MatterParty, pk=pk)
    matter_pk = party.matter_id
    audit(request, "matter_party.deleted", party, detail=str(party))
    party.delete()
    messages.success(request, "Matter party removed.")
    return redirect("matter_detail", pk=matter_pk)


@staff_required
@require_POST
def matter_add_activity(request, pk):
    matter = get_object_or_404(Matter, pk=pk)
    form = ActivityForm(
        request.POST,
        instance=Activity(contact=matter.primary_contact, matter=matter),
    )
    if form.is_valid():
        activity = form.save(commit=False)
        activity.matter = matter
        activity.contact = matter.primary_contact
        activity.created_by = request.user
        activity.save()
        audit(request, "activity.created", activity)
        messages.success(request, "Activity added.")
    else:
        messages.error(request, "Review the activity fields and try again.")
    return redirect("matter_detail", pk=matter.pk)


@staff_required
@require_GET
def work_list(request):
    kind = request.GET.get("kind", "").strip()
    owner = request.GET.get("owner", "all").strip()
    work_items = WorkItem.objects.select_related(
        "contact", "matter__primary_contact", "assigned_to"
    ).order_by(*WORK_ORDER)
    if kind in WorkItem.Kind.values:
        work_items = work_items.filter(kind=kind)
    if owner == "mine":
        work_items = work_items.filter(assigned_to=request.user)
    if request.GET.get("completed") != "1":
        work_items = work_items.filter(is_completed=False)
    work_items = list(work_items[:300])
    return render(
        request,
        "crm/work_list.html",
        {
            "work_items": work_items,
            "limit": 300,
            "work_groups": work_groups(work_items),
            "kinds": WorkItem.Kind.choices,
            "selected_kind": kind,
            "owner": owner,
        },
    )


def work_groups(items):
    """Work items grouped by when they are due, in the order the work list shows them."""
    now = timezone.now()
    today = timezone.localdate()
    groups = {}
    for item in items:
        if item.is_completed:
            label = "Done"
        elif item.scheduled_for is None:
            label = "Not scheduled"
        elif item.scheduled_for < now:
            label = "Overdue"
        else:
            day = timezone.localdate(item.scheduled_for)
            if day == today:
                label = "Today"
            elif (day - today).days < 7:
                label = "Next 7 days"
            else:
                label = "Later"
        groups.setdefault(label, []).append(item)
    order = ("Overdue", "Today", "Next 7 days", "Later", "Not scheduled", "Done")
    return [(label, groups[label]) for label in order if label in groups]


@staff_required
@require_http_methods(["GET", "POST"])
def work_create(request):
    initial = {"assigned_to": request.user}
    matter = optional_record(Matter, request.GET.get("matter"))
    if matter:
        initial["matter"] = matter
    contact = optional_record(Contact, request.GET.get("contact"))
    if contact:
        initial["contact"] = contact
    form = WorkItemForm(request.POST or None, initial=initial)
    if form.is_valid():
        item = form.save()
        audit(request, "work_item.created", item)
        messages.success(request, f"{item.get_kind_display()} created.")
        return redirect("work_list")
    return render(
        request,
        "crm/form_page.html",
        {
            "form": form,
            "title": "New work item",
            "submit_label": "Create item",
            "eyebrow": "Work",
            "cancel_url": reverse("work_list"),
            "prompt_capability": "video",
            "prompt_purpose": "to make a meeting for each of your appointments",
        },
    )


@staff_required
@require_http_methods(["GET", "POST"])
def work_update(request, pk):
    item = get_object_or_404(WorkItem, pk=pk)
    form = WorkItemForm(request.POST or None, instance=item)
    if form.is_valid():
        item = form.save()
        audit(request, "work_item.updated", item)
        messages.success(request, f"{item.get_kind_display()} updated.")
        return redirect("work_list")
    return render(
        request,
        "crm/form_page.html",
        {
            "form": form,
            "title": "Edit work item",
            "submit_label": "Save changes",
            "eyebrow": "Work",
            "cancel_url": reverse("work_list"),
        },
    )


@staff_required
@require_POST
def work_toggle(request, pk):
    item = get_object_or_404(WorkItem, pk=pk)
    item.is_completed = not item.is_completed
    item.save(update_fields=["is_completed", "completed_at", "updated_at"])
    audit(request, "work_item.toggled", item, detail=f"completed={item.is_completed}")
    next_url = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect("work_list")


@staff_required
@require_POST
def timer_start(request):
    matter = optional_record(Matter, request.POST.get("matter"))
    contact = optional_record(Contact, request.POST.get("contact"))
    if matter and contact is None:
        contact = matter.primary_contact
    if not matter and not contact:
        messages.error(request, "Link the timer to a contact or matter.")
        return redirect("dashboard")
    with transaction.atomic():
        running = (
            TimeEntry.objects.select_for_update()
            .filter(user=request.user, stopped_at__isnull=True)
            .first()
        )
        if running:
            running.stopped_at = timezone.now()
            running.save(update_fields=["stopped_at"])
            messages.info(request, f"Previous timer stopped at {running.duration_display}.")
        entry = TimeEntry.objects.create(matter=matter, contact=contact, user=request.user)
    audit(request, "timer.started", entry)
    messages.success(request, "Timer started.")
    next_url = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    if matter:
        return redirect("matter_detail", pk=matter.pk)
    return redirect("contact_detail", pk=contact.pk)


@staff_required
@require_POST
def timer_stop(request):
    entry = TimeEntry.objects.filter(user=request.user, stopped_at__isnull=True).first()
    if entry:
        entry.stopped_at = timezone.now()
        note = request.POST.get("note", "").strip()
        if note:
            entry.note = note[:500]
        entry.save(update_fields=["stopped_at", "note"])
        audit(request, "timer.stopped", entry, detail=entry.duration_display)
        messages.success(request, f"Timer stopped at {entry.duration_display}.")
    else:
        messages.info(request, "No timer is running.")
    next_url = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect("dashboard")


@permission_required(DELETE_RECORDS)
@require_POST
def time_entry_delete(request, pk):
    entry = get_object_or_404(TimeEntry, pk=pk)
    matter_pk = entry.matter_id
    contact_pk = entry.contact_id
    audit(request, "time_entry.deleted", entry, detail=str(entry))
    entry.delete()
    messages.success(request, "Time entry deleted.")
    if matter_pk:
        return redirect("matter_detail", pk=matter_pk)
    if contact_pk:
        return redirect("contact_detail", pk=contact_pk)
    return redirect("dashboard")


@staff_required
@require_POST
def checklist_add(request, matter_pk):
    matter = get_object_or_404(Matter, pk=matter_pk)
    form = ChecklistItemForm(request.POST)
    if form.is_valid():
        item = form.save(commit=False)
        item.matter = matter
        # With a workflow applied, new items join the phase in progress.
        item.phase = MatterPhase.objects.filter(
            workflow__matter=matter, status=MatterPhase.Status.IN_PROGRESS
        ).first()
        item.save()
        audit(request, "checklist_item.created", item)
        messages.success(request, "Checklist item added.")
    else:
        messages.error(request, "Review the checklist item and try again.")
    return redirect(reverse("matter_detail", args=[matter.pk]) + "#checklist")


@staff_required
@require_POST
def checklist_toggle(request, pk):
    item = get_object_or_404(ChecklistItem, pk=pk)
    item.is_completed = not item.is_completed
    item.completed_at = timezone.now() if item.is_completed else None
    item.completed_by = request.user if item.is_completed else None
    item.save(update_fields=["is_completed", "completed_at", "completed_by"])
    audit(request, "checklist_item.toggled", item, detail=f"completed={item.is_completed}")
    return redirect(reverse("matter_detail", args=[item.matter_id]) + "#checklist")


@staff_required
@require_POST
def document_upload(request, matter_pk):
    matter = get_object_or_404(Matter, pk=matter_pk)
    form = DocumentUploadForm(request.POST, request.FILES)
    if form.is_valid():
        document = form.save(commit=False)
        document.matter = matter
        document.contact = document.contact or matter.primary_contact
        linked_contact_ids = {
            matter.primary_contact_id,
            *matter.parties.values_list("contact_id", flat=True),
        }
        if document.contact_id not in linked_contact_ids:
            messages.error(request, "The document contact must be linked to this matter.")
            return redirect("matter_detail", pk=matter.pk)
        document.original_filename = request.FILES["file"].name[:255]
        document.mime_type = form.detected_mime
        document.size_bytes = request.FILES["file"].size
        document.uploaded_by = request.user
        document.save()
        add_activity(
            request=request,
            matter=matter,
            contact=document.contact,
            subject=f"Document uploaded: {document.title}",
            kind=Activity.Kind.DOCUMENT,
        )
        audit(request, "document.uploaded", document)
        messages.success(request, "Document uploaded.")
    else:
        messages.error(request, "Review the document fields and file type.")
    return redirect("matter_detail", pk=matter.pk)


@staff_required
@require_GET
def document_download(request, pk):
    document = get_object_or_404(Document, pk=pk)
    try:
        file_handle = document.file.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404("Document file is unavailable.") from None
    audit(request, "document.downloaded", document)
    response = FileResponse(
        file_handle,
        as_attachment=True,
        filename=document.original_filename,
        content_type=document.mime_type,
    )
    response["Cache-Control"] = "private, no-store"
    return response


@permission_required(DELETE_RECORDS)
@require_POST
def document_delete(request, pk):
    document = get_object_or_404(Document, pk=pk)
    matter_pk = document.matter_id
    audit(request, "document.deleted", document, detail=document.title)
    storage = document.file.storage
    filename = document.file.name
    document.delete()
    if filename:
        storage.delete(filename)
    messages.success(request, "Document deleted.")
    return redirect("matter_detail", pk=matter_pk)


@staff_required
@require_GET
def intake_workspace(request):
    return render(
        request,
        "crm/intake_workspace.html",
        {
            "intake_forms": IntakeForm.objects.annotate(invite_count=Count("invites")),
            "invites": IntakeInvite.objects.select_related("intake_form", "contact", "matter")[:50],
            "submissions": IntakeSubmission.objects.select_related(
                "invite__contact", "invite__intake_form"
            )[:50],
        },
    )


@staff_required
@require_http_methods(["GET", "POST"])
def intake_form_create(request):
    form = IntakeFormConfigForm(request.POST or None)
    if form.is_valid():
        intake_form = form.save()
        audit(request, "intake_form.created", intake_form)
        messages.success(request, "Intake form created.")
        return redirect("intake_workspace")
    return render(
        request,
        "crm/form_page.html",
        {
            "form": form,
            "title": "New intake form",
            "submit_label": "Create form",
            "eyebrow": "Intake",
            "cancel_url": reverse("intake_workspace"),
        },
    )


@staff_required
@require_http_methods(["GET", "POST"])
def intake_form_update(request, pk):
    intake_form = get_object_or_404(IntakeForm, pk=pk)
    form = IntakeFormConfigForm(request.POST or None, instance=intake_form)
    if form.is_valid():
        form.save()
        audit(request, "intake_form.updated", intake_form)
        messages.success(request, "Intake form updated.")
        return redirect("intake_workspace")
    return render(
        request,
        "crm/form_page.html",
        {
            "form": form,
            "title": "Edit intake form",
            "submit_label": "Save changes",
            "eyebrow": "Intake",
            "cancel_url": reverse("intake_workspace"),
        },
    )


def email_intake_link(invite, url, actor):
    """Email a new intake link to its contact; return why it was not sent, or ""."""
    firm = FirmProfile.load().name
    name = invite.intake_form.name
    expires = f"{invite.expires_at:%B} {invite.expires_at.day}, {invite.expires_at:%Y}"
    try:
        sent = system_email.send_system_email(
            invite.contact,
            subject=f"{name}: your secure link from {firm}",
            body=(
                f"Hello {invite.contact.first_name},\n\n"
                f'{firm} asks you to fill in "{name}". Use this private link:\n\n{url}\n\n'
                f"It works once and expires on {expires}. Please do not forward it; it is "
                "meant only for you. If you did not expect this email, you can ignore it.\n"
            ),
            log_summary=f'Secure intake link for "{name}", expiring {expires}. '
            "The link itself is not kept.",
            purpose="Intake link",
            actor=actor,
            matter=invite.matter,
        )
    except ValidationError as exc:
        return " ".join(exc.messages).rstrip(".")
    return system_email.not_sent_reason(sent)


@staff_required
@require_http_methods(["GET", "POST"])
def intake_invite_create(request):
    form = IntakeInviteForm(request.POST or None, can_email=system_email.sender() is not None)
    if form.is_valid():
        invite, raw_token = form.issue(request.user)
        audit(request, "intake_invite.created", invite)
        url = request.build_absolute_uri(reverse("public_intake", kwargs={"token": raw_token}))
        context = {"invite": invite, "intake_url": url, "can_email": "send_email" in form.fields}
        if form.cleaned_data.get("send_email"):
            context["email_problem"] = email_intake_link(invite, url, request.user)
            context["emailed"] = not context["email_problem"]
            if context["emailed"]:
                audit(request, "intake_invite.emailed", invite)
        return render(request, "crm/intake_invite_created.html", context)
    return render(
        request,
        "crm/form_page.html",
        {
            "form": form,
            "title": "Create secure intake link",
            "submit_label": "Create link",
            "eyebrow": "Intake",
            "cancel_url": reverse("intake_workspace"),
        },
    )


@staff_required
@require_POST
def intake_invite_revoke(request, pk):
    invite = get_object_or_404(IntakeInvite, pk=pk)
    if not invite.revoked_at:
        invite.revoked_at = timezone.now()
        invite.save(update_fields=["revoked_at"])
        audit(request, "intake_invite.revoked", invite)
    messages.success(request, "Intake link revoked.")
    return redirect("intake_workspace")


@require_http_methods(["GET", "POST"])
def public_intake(request, token):
    token_hash = IntakeInvite.hash_token(token)
    invite = (
        IntakeInvite.objects.select_related("intake_form", "contact", "matter")
        .filter(token_hash=token_hash)
        .first()
    )
    if not invite or not invite.is_active:
        return render(request, "crm/intake_unavailable.html", status=410)

    if request.method == "GET" and not invite.opened_at:
        invite.opened_at = timezone.now()
        invite.save(update_fields=["opened_at"])

    form = PublicIntakeForm(request.POST or None, intake_form=invite.intake_form)
    if form.is_valid():
        with transaction.atomic():
            locked = IntakeInvite.objects.select_for_update().get(pk=invite.pk)
            if not locked.is_active:
                return render(request, "crm/intake_unavailable.html", status=410)
            IntakeSubmission.objects.create(
                invite=locked,
                email=form.cleaned_data.get("email", ""),
                phone=form.cleaned_data.get("phone", ""),
                answers=form.answers,
            )
            locked.submitted_at = timezone.now()
            locked.save(update_fields=["submitted_at"])
            Activity.objects.create(
                contact=locked.contact,
                matter=locked.matter,
                kind=Activity.Kind.INTAKE,
                subject=f"Intake submitted: {locked.intake_form.name}",
            )
        return render(request, "crm/intake_complete.html")

    return render(request, "crm/public_intake.html", {"invite": invite, "form": form})


@staff_required
@require_GET
def intake_submission_detail(request, pk):
    submission = get_object_or_404(
        IntakeSubmission.objects.select_related(
            "invite__contact",
            "invite__matter",
            "invite__intake_form",
            "reviewed_by",
        ),
        pk=pk,
    )
    return render(request, "crm/intake_submission_detail.html", {"submission": submission})


@staff_required
@require_POST
def intake_submission_review(request, pk):
    submission = get_object_or_404(IntakeSubmission, pk=pk)
    if not submission.reviewed_at:
        submission.reviewed_at = timezone.now()
        submission.reviewed_by = request.user
        submission.save(update_fields=["reviewed_at", "reviewed_by"])
        audit(request, "intake_submission.reviewed", submission)
    messages.success(request, "Submission marked reviewed.")
    return redirect("intake_submission_detail", pk=submission.pk)


@permission_required(MANAGE_FIRM_SETTINGS)
@require_http_methods(["GET", "POST"])
def firm_settings(request):
    profile = FirmProfile.load()
    form = FirmProfileForm(request.POST or None, instance=profile)
    if form.is_valid():
        profile = form.save()
        audit(request, "firm_profile.updated", profile)
        messages.success(request, "Firm settings updated.")
        return redirect("firm_settings")
    return render(
        request,
        "crm/form_page.html",
        {
            "form": form,
            "title": "Firm settings",
            "submit_label": "Save settings",
            "eyebrow": "Settings",
        },
    )


@staff_required
@require_GET
def search(request):
    query = request.GET.get("q", "").strip()
    contacts = Contact.objects.none()
    matters = Matter.objects.none()
    if query:
        contacts = Contact.objects.filter(words_match(query, CONTACT_SEARCH_FIELDS))[:30]
        matters = Matter.objects.select_related("primary_contact").filter(
            words_match(query, MATTER_SEARCH_FIELDS)
        )[:30]
    return render(
        request, "crm/search.html", {"query": query, "contacts": contacts, "matters": matters}
    )


@permission_required(EXPORT_RECORDS)
@require_GET
def export_csv(request):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(
        f"open-immigration-crm-{timezone.localdate().isoformat()}.csv"
    )
    response["Cache-Control"] = "private, no-store"
    writer = csv.writer(response)
    writer.writerow(
        ["record_type", "record_id", "name_or_number", "email", "phone", "status", "updated_at"]
    )
    for contact in Contact.objects.all():
        writer.writerow(
            [
                "contact",
                contact.pk,
                csv_safe(contact.full_name),
                csv_safe(contact.email),
                csv_safe(contact.phone),
                contact.stage,
                contact.updated_at.isoformat(),
            ]
        )
    for matter in Matter.objects.all():
        writer.writerow(
            [
                "matter",
                matter.pk,
                matter.matter_number,
                "",
                "",
                matter.status,
                matter.updated_at.isoformat(),
            ]
        )
    audit(request, "data.exported", detail="contacts_and_matters_csv")
    return response


class AuditedPasswordChangeView(PasswordChangeView):
    template_name = "registration/password_change_form.html"
    success_url = reverse_lazy("password_change_done")

    def form_valid(self, form):
        response = super().form_valid(form)
        audit(self.request, "user.password_changed", self.request.user)
        return response
