import os
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from crm.connect.base import ConnectorError
from crm.connect.models import OutboundRequest
from crm.models import Contact, Document, Matter
from crm.search import words_match
from crm.services import audit

from .emailing import email_connections, send_email
from .forms import AttachmentSaveForm, CommunicationForm
from .models import Communication, CommunicationAttachment
from .services import contact_matters, save_to_matter, set_texting_opt_out
from .texting import (
    approved_templates,
    can_text_freely,
    reply_window,
    reply_window_ends,
    send_template,
    send_text,
    template_connections,
    texting_connections,
)

staff_required = user_passes_test(
    lambda user: user.is_active and user.is_staff,
    login_url="login",
)

LIST_LIMIT = 200


def safe_next(request, fallback):
    target = request.POST.get("next") or request.GET.get("next") or ""
    if url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return fallback


@staff_required
@require_GET
def communication_list(request):
    query = request.GET.get("q", "").strip()
    channel = request.GET.get("channel", "")
    direction = request.GET.get("direction", "")
    communications = Communication.objects.select_related(
        "contact", "matter", "logged_by", "source_connection"
    ).prefetch_related("attachments__saved_document__matter")
    contact = Contact.objects.filter(pk=request.GET.get("contact") or None).first()
    matter = Matter.objects.filter(pk=request.GET.get("matter") or None).first()
    if contact:
        communications = communications.filter(contact=contact)
    if matter:
        communications = communications.filter(matter=matter)
    if channel in Communication.Channel.values:
        communications = communications.filter(channel=channel)
    if direction in Communication.Direction.values:
        communications = communications.filter(direction=direction)
    unmatched = Communication.objects.filter(contact__isnull=True, matter__isnull=True)
    matching_only = request.GET.get("matching") == "needed"
    if matching_only:
        communications = communications.filter(contact__isnull=True, matter__isnull=True)
    if query:
        communications = communications.filter(
            words_match(
                query,
                ("subject", "summary", "counterpart", "contact__first_name", "contact__last_name"),
            )
        )
    return render(
        request,
        "comms/communication_list.html",
        {
            "communications": communications[:LIST_LIMIT],
            "limit": LIST_LIMIT,
            "query": query,
            "channels": Communication.Channel.choices,
            "directions": Communication.Direction.choices,
            "selected_channel": channel,
            "selected_direction": direction,
            "filter_contact": contact,
            "filter_matter": matter,
            "needs_matching": unmatched.count(),
            "matching_only": matching_only,
        },
    )


@staff_required
@require_http_methods(["GET", "POST"])
def communication_match(request, pk):
    """Link a communication that matched no contact, and others from the same number or address."""
    communication = get_object_or_404(
        Communication.objects.select_related("source_connection"),
        pk=pk,
        contact__isnull=True,
        matter__isnull=True,
    )
    same = Communication.objects.filter(
        contact__isnull=True,
        matter__isnull=True,
        counterpart_key=communication.counterpart_key,
    ).exclude(pk=communication.pk)
    if not communication.counterpart_key:
        same = same.none()
    if request.method == "POST":
        contact = get_object_or_404(Contact, pk=request.POST.get("contact"))
        linked = [communication.pk]
        if request.POST.get("include_same") == "on":
            linked += list(same.values_list("pk", flat=True))
        Communication.objects.filter(pk__in=linked).update(contact=contact)
        audit(request, "communication.matched", communication, f"{len(linked)} linked")
        messages.success(
            request,
            f"Linked {len(linked)} communication{'s' if len(linked) != 1 else ''} to {contact}.",
        )
        return redirect(reverse("communication_list") + "?matching=needed")
    query = request.GET.get("q", "").strip()
    candidates = Contact.objects.none()
    if query:
        candidates = Contact.objects.filter(
            words_match(query, ("first_name", "last_name", "email", "phone"))
        )[:20]
    remote = communication.counterpart
    create_params = {"email": remote} if "@" in remote else {"phone": remote}
    return render(
        request,
        "comms/communication_match.html",
        {
            "communication": communication,
            "same_count": same.count(),
            "query": query,
            "candidates": candidates,
            "create_query": urlencode(create_params),
        },
    )


@staff_required
@require_http_methods(["GET", "POST"])
def communication_create(request):
    initial = {
        "occurred_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
        "channel": request.GET.get("channel") or Communication.Channel.CALL,
    }
    for name, model in (("contact", Contact), ("matter", Matter)):
        record = model.objects.filter(pk=request.GET.get(name) or None).first()
        if record:
            initial[name] = record
    form = CommunicationForm(request.POST or None, initial=initial, show_links=True)
    if request.method == "POST" and form.is_valid():
        communication = form.save(commit=False)
        communication.logged_by = request.user
        communication.save()
        audit(request, "communication.logged", communication)
        messages.success(request, f"{communication.get_channel_display()} logged.")
        contact = communication.contact
        if (
            communication.channel == Communication.Channel.TEXT
            and communication.direction == Communication.Direction.OUTBOUND
            and contact
            and contact.texting_opted_out_at
        ):
            messages.info(request, f"{contact} has opted out of texts.")
        fallback = (
            reverse("matter_detail", args=[communication.matter.pk])
            if communication.matter
            else reverse("contact_detail", args=[contact.pk])
        )
        return redirect(safe_next(request, fallback))
    return render(request, "comms/communication_form.html", {"form": form})


def recipient(params, connections):
    """The contact, matter, and chosen connection a message is for, and the page to return to."""
    contact = get_object_or_404(Contact, pk=params.get("contact"))
    matter = Matter.objects.filter(pk=params.get("matter") or None).first()
    fallback = (
        reverse("matter_detail", args=[matter.pk])
        if matter
        else reverse("contact_detail", args=[contact.pk])
    )
    connection = {str(c.pk): c for c in connections}.get(params.get("connection", ""))
    if connection is None:
        raise PermissionDenied
    if (
        matter
        and contact != matter.primary_contact
        and not matter.parties.filter(contact=contact).exists()
    ):
        raise PermissionDenied
    return contact, matter, connection, fallback


def send_from_page(request, connections, noun, send):
    """Send a text or email to a contact from a contact or matter page, and say what happened."""
    contact, matter, connection, fallback = recipient(request.POST, connections)
    try:
        outbound = send(connection, contact=contact, actor=request.user, matter=matter)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(safe_next(request, fallback))
    report(request, outbound, connection, noun)
    return redirect(safe_next(request, fallback))


def report(request, outbound, connection, noun):
    """Say what happened to a message sent through the outbound policy."""
    if outbound.status == OutboundRequest.Status.SENT:
        messages.success(request, f"{noun.capitalize()} sent through {connection.label}.")
    elif outbound.status == OutboundRequest.Status.PREVIEWED:
        messages.info(
            request,
            f"{connection.label} is in preview mode, so the {noun} was recorded but not sent. "
            "A superuser can turn preview mode off on the connection's page.",
        )
    elif outbound.status == OutboundRequest.Status.FAILED:
        messages.error(request, f"The {noun} was not sent: {outbound.error}")
    else:
        messages.error(request, f"The {noun} was not sent. {outbound.error}")


@staff_required
@require_POST
def communication_send_text(request):
    body = request.POST.get("body", "")
    return send_from_page(
        request,
        texting_connections(request.user),
        "text",
        lambda connection, **kwargs: send_text(connection, body=body, **kwargs),
    )


def template_choices(templates, chosen, values):
    """Each template with its text as a preview, and the values entered for the chosen one."""
    choices = []
    for template in templates:
        kept = values if template == chosen else {}
        choices.append(
            {
                "template": template,
                "preview": template.render(
                    {item.key: f"[{item.label}]" for item in template.fields}
                ),
                "chosen": template == chosen,
                "fields": [
                    {"field": item, "value": kept.get(item.key, "")} for item in template.fields
                ],
            }
        )
    return choices


@staff_required
@require_http_methods(["GET", "POST"])
def communication_send_template(request):
    """Choose one of a messaging service's approved templates for a contact, and send it."""
    params = request.POST if request.method == "POST" else request.GET
    contact, matter, connection, fallback = recipient(params, template_connections(request.user))
    try:
        templates = approved_templates(connection)
        error = ""
    except ConnectorError as exc:
        templates, error = [], str(exc)
    chosen, values = None, {}
    if request.method == "POST":
        chosen = next((t for t in templates if t.id == request.POST.get("template")), None)
        if chosen is None:
            messages.error(request, "That template is no longer approved. Choose another.")
        else:
            values = {item.key: request.POST.get(f"value-{item.key}", "") for item in chosen.fields}
            try:
                outbound = send_template(
                    connection,
                    contact=contact,
                    template=chosen,
                    values=values,
                    actor=request.user,
                    matter=matter,
                )
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
            else:
                report(request, outbound, connection, "message")
                return redirect(fallback + "#communications")
    window = reply_window(connection)
    return render(
        request,
        "comms/send_template.html",
        {
            "contact": contact,
            "matter": matter,
            "connection": connection,
            "back": fallback,
            "error": error,
            "window_hours": int(window.total_seconds() // 3600) if window else None,
            "window_ends": reply_window_ends(connection, contact),
            "window_open": can_text_freely(connection, contact),
            "templates": template_choices(templates, chosen, values),
        },
    )


@staff_required
@require_POST
def communication_send_email(request):
    subject, body = request.POST.get("subject", ""), request.POST.get("body", "")
    return send_from_page(
        request,
        email_connections(request.user),
        "email",
        lambda connection, **kwargs: send_email(connection, subject=subject, body=body, **kwargs),
    )


@staff_required
@require_POST
def communication_delete(request, pk):
    if not request.user.is_superuser:
        raise PermissionDenied
    communication = get_object_or_404(Communication, pk=pk)
    audit(request, "communication.deleted", communication, communication.get_channel_display())
    communication.delete()
    messages.success(request, "Communication deleted.")
    return redirect(safe_next(request, "communication_list"))


@staff_required
@require_POST
def contact_texting_opt_out(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    opted_out = request.POST.get("opted_out") == "yes"
    if set_texting_opt_out(contact, opted_out, actor=request.user):
        messages.success(
            request,
            "Recorded that this contact opted out of texts."
            if opted_out
            else "Recorded that this contact agreed to receive texts again.",
        )
    return redirect("contact_detail", pk=contact.pk)


@staff_required
@require_GET
def communication_attachment_download(request, pk):
    attachment = get_object_or_404(CommunicationAttachment, pk=pk)
    try:
        file_handle = attachment.file.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404("The file is unavailable.") from None
    audit(request, "communication_attachment.downloaded", attachment)
    response = FileResponse(
        file_handle,
        as_attachment=True,
        filename=attachment.original_filename,
        content_type=attachment.mime_type,
    )
    response["Cache-Control"] = "private, no-store"
    return response


@staff_required
@require_http_methods(["GET", "POST"])
def communication_attachment_save(request, pk):
    """Save a copy of a file a contact sent to one of their matters, as a document."""
    attachment = get_object_or_404(
        CommunicationAttachment.objects.select_related(
            "communication__contact", "saved_document__matter"
        ),
        pk=pk,
    )
    contact = attachment.communication.contact
    matters = contact_matters(contact) if contact else Matter.objects.none()
    chosen = matters.filter(pk=request.GET.get("matter") or None).first()
    form = AttachmentSaveForm(
        request.POST or None,
        matters=matters,
        initial={
            "matter": chosen,
            "title": os.path.splitext(attachment.original_filename)[0],
            "category": Document.Category.OTHER,
        },
    )
    if request.method == "POST" and form.is_valid():
        matter = form.cleaned_data["matter"]
        document = save_to_matter(
            attachment,
            matter,
            title=form.cleaned_data["title"],
            category=form.cleaned_data["category"],
            actor=request.user,
        )
        audit(request, "document.saved_from_message", document)
        messages.success(request, f"Saved to {matter} as a document.")
        return redirect("matter_detail", pk=matter.pk)
    return render(
        request,
        "comms/attachment_save.html",
        {
            "attachment": attachment,
            "contact": contact,
            "form": form,
            "has_matters": matters.exists(),
        },
    )
