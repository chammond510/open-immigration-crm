import csv
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth.views import LoginView, PasswordChangeView
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

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
    WorkItem,
)
from .services import add_activity, audit

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


class FirstRunAwareLoginView(LoginView):
    """Send a pristine installation to first-run setup instead of a dead end."""

    template_name = "registration/login.html"

    def dispatch(self, request, *args, **kwargs):
        if not installation_has_accounts():
            return redirect("first_run_setup")
        return super().dispatch(request, *args, **kwargs)


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


superuser_required = user_passes_test(
    lambda user: user.is_active and user.is_superuser,
    login_url="login",
)


@staff_required
@require_GET
def dashboard(request):
    today = timezone.localdate()
    open_work = (
        WorkItem.objects.filter(is_completed=False)
        .select_related("contact", "matter", "assigned_to")
        .order_by("scheduled_for")[:12]
    )
    recent_matters = Matter.objects.select_related("primary_contact", "assigned_to")[:8]
    stage_rows = Contact.objects.values("stage").annotate(total=Count("id"))
    stage_counts = {row["stage"]: row["total"] for row in stage_rows}
    pipeline = [
        {"key": key, "label": label, "count": stage_counts.get(key, 0)}
        for key, label in Contact.Stage.choices
    ]
    context = {
        "today": today,
        "open_matters": Matter.objects.exclude(status=Matter.Status.CLOSED).count(),
        "active_contacts": Contact.objects.exclude(
            stage__in=[Contact.Stage.CLOSED, Contact.Stage.LOST]
        ).count(),
        "overdue_count": WorkItem.objects.filter(
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
    contacts = Contact.objects.select_related("assigned_to")
    if query:
        contacts = contacts.filter(
            Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(email__icontains=query)
            | Q(phone__icontains=query)
            | Q(a_number__icontains=query)
        )
    if stage in Contact.Stage.values:
        contacts = contacts.filter(stage=stage)
    return render(
        request,
        "crm/contact_list.html",
        {"contacts": contacts[:250], "query": query, "selected_stage": stage},
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
    form = ContactForm(request.POST or None, initial={"assigned_to": request.user})
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
        {"form": form, "title": "New contact", "submit_label": "Create contact"},
    )


@staff_required
@require_GET
def contact_detail(request, pk):
    contact = get_object_or_404(Contact.objects.select_related("assigned_to"), pk=pk)
    context = {
        "contact": contact,
        "matters": contact.matters.select_related("assigned_to"),
        "activities": contact.activities.select_related("created_by")[:30],
        "work_items": contact.work_items.select_related("matter", "assigned_to")[:20],
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
        {"form": form, "title": "Edit contact", "submit_label": "Save changes"},
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
        matters = matters.filter(
            Q(matter_number__icontains=query)
            | Q(title__icontains=query)
            | Q(primary_contact__first_name__icontains=query)
            | Q(primary_contact__last_name__icontains=query)
            | Q(receipt_number__icontains=query)
        )
    if status in Matter.Status.values:
        matters = matters.filter(status=status)
    return render(
        request,
        "crm/matter_list.html",
        {"matters": matters[:250], "query": query, "selected_status": status},
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
        {"form": form, "title": "New matter", "submit_label": "Create matter"},
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
        "work_items": matter.work_items.select_related("contact", "assigned_to")[:20],
        "checklist": matter.checklist.select_related("completed_by"),
        "documents": matter.documents.select_related("contact", "uploaded_by"),
        "party_form": MatterPartyForm(),
        "activity_form": ActivityForm(
            instance=Activity(contact=matter.primary_contact, matter=matter),
            initial={"occurred_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M")},
        ),
        "checklist_form": ChecklistItemForm(),
        "document_form": DocumentUploadForm(initial={"contact": matter.primary_contact}),
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
        {"form": form, "title": f"Edit {matter.matter_number}", "submit_label": "Save changes"},
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


@superuser_required
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
    work_items = WorkItem.objects.select_related("contact", "matter", "assigned_to")
    if kind in WorkItem.Kind.values:
        work_items = work_items.filter(kind=kind)
    if owner == "mine":
        work_items = work_items.filter(assigned_to=request.user)
    if request.GET.get("completed") != "1":
        work_items = work_items.filter(is_completed=False)
    return render(
        request,
        "crm/work_list.html",
        {"work_items": work_items[:300], "selected_kind": kind, "owner": owner},
    )


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
        {"form": form, "title": "New work item", "submit_label": "Create item"},
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
def checklist_add(request, matter_pk):
    matter = get_object_or_404(Matter, pk=matter_pk)
    form = ChecklistItemForm(request.POST)
    if form.is_valid():
        item = form.save(commit=False)
        item.matter = matter
        item.save()
        audit(request, "checklist_item.created", item)
        messages.success(request, "Checklist item added.")
    else:
        messages.error(request, "Review the checklist item and try again.")
    return redirect("matter_detail", pk=matter.pk)


@staff_required
@require_POST
def checklist_toggle(request, pk):
    item = get_object_or_404(ChecklistItem, pk=pk)
    item.is_completed = not item.is_completed
    item.completed_at = timezone.now() if item.is_completed else None
    item.completed_by = request.user if item.is_completed else None
    item.save(update_fields=["is_completed", "completed_at", "completed_by"])
    audit(request, "checklist_item.toggled", item, detail=f"completed={item.is_completed}")
    return redirect("matter_detail", pk=item.matter_id)


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


@superuser_required
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
        {"form": form, "title": "New intake form", "submit_label": "Create form"},
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
        {"form": form, "title": "Edit intake form", "submit_label": "Save changes"},
    )


@staff_required
@require_http_methods(["GET", "POST"])
def intake_invite_create(request):
    form = IntakeInviteForm(request.POST or None)
    if form.is_valid():
        invite, raw_token = form.issue(request.user)
        audit(request, "intake_invite.created", invite)
        url = request.build_absolute_uri(reverse("public_intake", kwargs={"token": raw_token}))
        return render(
            request, "crm/intake_invite_created.html", {"invite": invite, "intake_url": url}
        )
    return render(
        request,
        "crm/form_page.html",
        {"form": form, "title": "Create secure intake link", "submit_label": "Create link"},
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


@superuser_required
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
        {"form": form, "title": "Firm settings", "submit_label": "Save settings"},
    )


@staff_required
@require_GET
def search(request):
    query = request.GET.get("q", "").strip()
    contacts = Contact.objects.none()
    matters = Matter.objects.none()
    if query:
        contacts = Contact.objects.filter(
            Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(email__icontains=query)
            | Q(phone__icontains=query)
            | Q(a_number__icontains=query)
        )[:30]
        matters = Matter.objects.select_related("primary_contact").filter(
            Q(matter_number__icontains=query)
            | Q(title__icontains=query)
            | Q(receipt_number__icontains=query)
        )[:30]
    return render(
        request, "crm/search.html", {"query": query, "contacts": contacts, "matters": matters}
    )


@superuser_required
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
