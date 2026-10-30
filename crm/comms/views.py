from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from crm.connect.models import OutboundRequest
from crm.models import Contact, Matter
from crm.services import audit

from .emailing import email_connections, send_email
from .forms import CommunicationForm
from .models import Communication
from .services import set_texting_opt_out
from .texting import send_text, texting_connections

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
    )
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
            Q(subject__icontains=query)
            | Q(summary__icontains=query)
            | Q(counterpart__icontains=query)
            | Q(contact__first_name__icontains=query)
            | Q(contact__last_name__icontains=query)
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
            Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(email__icontains=query)
            | Q(phone__icontains=query)
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


def send_from_page(request, connections, noun, send):
    """Send a text or email to a contact from a contact or matter page, and say what happened."""
    contact = get_object_or_404(Contact, pk=request.POST.get("contact"))
    matter = Matter.objects.filter(pk=request.POST.get("matter") or None).first()
    fallback = (
        reverse("matter_detail", args=[matter.pk])
        if matter
        else reverse("contact_detail", args=[contact.pk])
    )
    connection = {str(c.pk): c for c in connections}.get(request.POST.get("connection", ""))
    if connection is None:
        raise PermissionDenied
    if (
        matter
        and contact != matter.primary_contact
        and not matter.parties.filter(contact=contact).exists()
    ):
        raise PermissionDenied
    try:
        outbound = send(connection, contact=contact, actor=request.user, matter=matter)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(safe_next(request, fallback))
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
    return redirect(safe_next(request, fallback))


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
