"""Pages for the AI features: the assistant, consult memos, and task suggestions."""

from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from crm.comms.emailing import email_connections
from crm.comms.texting import MAX_LENGTH, texting_connections
from crm.connect.scheduler import worker_is_running
from crm.forms import WorkItemForm
from crm.mcp import tools as crm_tools
from crm.models import Contact, Matter, WorkItem
from crm.services import audit

from . import assistant, jobs, memos, suggestions
from .client import ai_connections
from .forms import AskForm, ChooseConnectionForm
from .models import AISettings, ConsultMemo, Conversation, ConversationMessage, TaskSuggestion
from .records import clip
from .views import staff_required


def next_url(request, fallback):
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()}):
        return candidate
    return fallback


def feature_off(request, feature):
    if AISettings.load().feature_on(feature.key):
        return False
    messages.error(
        request,
        f"{feature.label} is off. Someone who manages connections and AI can turn it on in "
        "AI features.",
    )
    return True


def start_answer(conversation):
    conversation.status = Conversation.Status.WORKING
    conversation.error = ""
    conversation.save(update_fields=["status", "error", "updated_at"])
    transaction.on_commit(lambda: jobs.answer.enqueue(str(conversation.pk)))


# The assistant


@staff_required
@require_GET
def assistant_home(request):
    return render(
        request,
        "ai/assistant.html",
        {
            "on": AISettings.load().feature_on(assistant.FEATURE.key),
            "conversations": Conversation.objects.filter(owner=request.user).select_related(
                "matter"
            )[:50],
            "form": AskForm(user=request.user),
            "connections": ai_connections(request.user),
        },
    )


@staff_required
@require_POST
def assistant_start(request):
    matter = Matter.objects.filter(pk=request.POST.get("matter") or None).first()
    fallback = reverse("matter_detail", args=[matter.pk]) if matter else reverse("ai_assistant")
    if feature_off(request, assistant.FEATURE):
        return redirect(fallback)
    form = AskForm(request.POST, user=request.user)
    if not form.is_valid():
        messages.error(request, "Write a message and choose an AI connection you may use.")
        return redirect(next_url(request, fallback))
    with transaction.atomic():
        conversation = Conversation.objects.create(
            owner=request.user,
            matter=matter,
            title=clip(" ".join(form.cleaned_data["message"].split()), 80),
            connection=form.cleaned_data["connection"],
        )
        ConversationMessage.objects.create(
            conversation=conversation,
            role=ConversationMessage.Role.USER,
            content=form.cleaned_data["message"],
        )
        start_answer(conversation)
    return redirect("ai_conversation", pk=conversation.pk)


def entries_for(conversation):
    """The conversation as shown: messages, lookups, proposed changes, and drafts."""
    entries = []
    for message in conversation.messages.all():
        if message.role == ConversationMessage.Role.USER:
            entries.append({"kind": "user", "message": message})
        elif message.role == ConversationMessage.Role.ASSISTANT:
            if message.content.strip():
                entries.append({"kind": "assistant", "message": message})
        elif message.tool_name in assistant.WRITE_TOOLS:
            title, rows = assistant.describe(message)
            result = assistant.result_of(message)
            entries.append(
                {
                    "kind": "change",
                    "message": message,
                    "title": title,
                    "rows": rows,
                    "error": result.get("error", "") if isinstance(result, dict) else "",
                }
            )
        elif message.tool_name == assistant.DRAFT:
            entries.append(draft_entry(conversation, message))
        else:
            tool = crm_tools.BY_NAME.get(message.tool_name)
            entries.append({"kind": "lookup", "title": tool.title if tool else message.tool_name})
    return entries


def draft_entry(conversation, message):
    result = assistant.result_of(message)
    arguments = message.arguments or {}
    entry = {"kind": "draft", "message": message, "arguments": arguments}
    if not isinstance(result, dict) or result.get("error"):
        entry["error"] = result.get("error", "") if isinstance(result, dict) else ""
        return entry
    contact = Contact.objects.filter(pk=arguments.get("contact_id")).first()
    matter = conversation.matter
    if contact and matter and contact != matter.primary_contact:
        if not matter.parties.filter(contact=contact).exists():
            matter = None
    entry.update(contact=contact, matter=matter)
    return entry


@staff_required
@require_GET
def conversation_detail(request, pk):
    conversation = get_object_or_404(
        Conversation.objects.select_related("matter__primary_contact", "connection"),
        pk=pk,
        owner=request.user,
    )
    working = assistant.is_working(conversation)
    return render(
        request,
        "ai/conversation.html",
        {
            "conversation": conversation,
            "entries": entries_for(conversation),
            "working": working,
            "worker_running": worker_is_running() if working else True,
            "on": AISettings.load().feature_on(assistant.FEATURE.key),
            "form": AskForm(user=request.user, initial_connection=conversation.connection_id),
            "too_long": conversation.messages.count() >= assistant.MAX_MESSAGES,
            "text_connections": texting_connections(request.user),
            "text_max_length": MAX_LENGTH,
            "email_connections": email_connections(request.user),
        },
    )


@staff_required
@require_POST
def conversation_send(request, pk):
    conversation = get_object_or_404(Conversation, pk=pk, owner=request.user)
    if feature_off(request, assistant.FEATURE):
        return redirect("ai_conversation", pk=pk)
    if assistant.is_working(conversation):
        messages.error(request, "The assistant is still answering.")
        return redirect("ai_conversation", pk=pk)
    if conversation.messages.filter(action=ConversationMessage.Action.WAITING).exists():
        messages.error(request, "Confirm or decline the proposed changes first.")
        return redirect("ai_conversation", pk=pk)
    if conversation.messages.count() >= assistant.MAX_MESSAGES:
        messages.error(request, "This conversation is full. Start a new one.")
        return redirect("ai_conversation", pk=pk)
    form = AskForm(request.POST, user=request.user)
    if not form.is_valid():
        messages.error(request, "Write a message and choose an AI connection you may use.")
        return redirect("ai_conversation", pk=pk)
    with transaction.atomic():
        conversation.connection = form.cleaned_data["connection"]
        conversation.save(update_fields=["connection", "updated_at"])
        ConversationMessage.objects.create(
            conversation=conversation,
            role=ConversationMessage.Role.USER,
            content=form.cleaned_data["message"],
        )
        start_answer(conversation)
    return redirect("ai_conversation", pk=pk)


@staff_required
@require_POST
def conversation_decide(request, pk):
    """Confirm or decline one change the assistant proposed."""
    message = get_object_or_404(
        ConversationMessage.objects.select_related("conversation"),
        pk=pk,
        conversation__owner=request.user,
        action=ConversationMessage.Action.WAITING,
    )
    conversation = message.conversation
    if feature_off(request, assistant.FEATURE):
        return redirect("ai_conversation", pk=conversation.pk)
    confirm = request.POST.get("decision") == "confirm"
    with transaction.atomic():
        if assistant.decide(message, confirm=confirm, request=request):
            start_answer(conversation)
    result = assistant.result_of(message)
    if not confirm:
        messages.info(request, "Declined. Nothing was changed.")
    elif isinstance(result, dict) and result.get("error"):
        messages.error(request, f"The change was not made: {result['error']}")
    else:
        messages.success(request, "Done.")
    return redirect("ai_conversation", pk=conversation.pk)


@staff_required
@require_GET
def conversation_status(request, pk):
    conversation = get_object_or_404(Conversation, pk=pk, owner=request.user)
    return JsonResponse({"done": not assistant.is_working(conversation)})


@staff_required
@require_POST
def conversation_delete(request, pk):
    conversation = get_object_or_404(Conversation, pk=pk, owner=request.user)
    audit(request, "ai_conversation.deleted", conversation)
    conversation.delete()
    messages.success(request, "Conversation deleted.")
    return redirect("ai_assistant")


# Consult memos


@staff_required
@require_POST
def consult_memo_prepare(request, pk):
    appointment = get_object_or_404(
        WorkItem.objects.select_related("contact"),
        pk=pk,
        kind=WorkItem.Kind.APPOINTMENT,
        contact__isnull=False,
    )
    fallback = f"{reverse('contact_detail', args=[appointment.contact_id])}#consult-memos"
    if feature_off(request, memos.FEATURE):
        return redirect(fallback)
    form = ChooseConnectionForm(request.POST, user=request.user)
    if not form.is_valid():
        messages.error(request, "Choose an AI connection you may use.")
        return redirect(next_url(request, fallback))
    memo, created = ConsultMemo.objects.get_or_create(
        appointment=appointment, defaults={"contact": appointment.contact}
    )
    if not created and memos.is_preparing(memo):
        messages.info(request, "The memo is already being prepared.")
        return redirect(next_url(request, fallback))
    with transaction.atomic():
        memo.status = ConsultMemo.Status.PREPARING
        memo.error = ""
        memo.save(update_fields=["status", "error", "updated_at"])
        audit(request, "consult_memo.requested", appointment)
        connection = form.cleaned_data["connection"]
        transaction.on_commit(
            lambda: jobs.prepare_memo.enqueue(appointment.pk, request.user.pk, str(connection.pk))
        )
    messages.success(request, "Preparing the memo. It appears here when it is ready.")
    return redirect(next_url(request, fallback))


@staff_required
@require_GET
def consult_memo_status(request, pk):
    memo = get_object_or_404(ConsultMemo, appointment_id=pk)
    return JsonResponse({"done": not memos.is_preparing(memo)})


# Task suggestions


def suggestion_page(suggestion):
    if suggestion.matter_id:
        return reverse("matter_detail", args=[suggestion.matter_id])
    if suggestion.contact_id:
        return reverse("contact_detail", args=[suggestion.contact_id])
    return reverse("dashboard")


def pending_suggestion(request, pk):
    suggestion = get_object_or_404(TaskSuggestion, pk=pk)
    if suggestion.status != TaskSuggestion.Status.PENDING:
        messages.info(request, "That suggestion was already handled.")
        return suggestion, False
    return suggestion, True


@staff_required
@require_http_methods(["GET", "POST"])
def suggestion_accept(request, pk):
    """Create the suggested task, with any changes the staff member makes first."""
    suggestion, pending = pending_suggestion(request, pk)
    destination = next_url(request, suggestion_page(suggestion))
    if not pending:
        return redirect(destination)
    form = WorkItemForm(request.POST or None, initial=suggestions.task_values(suggestion))
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            item = form.save()
            suggestion.status = TaskSuggestion.Status.ACCEPTED
            suggestion.decided_at = timezone.now()
            suggestion.decided_by = request.user
            suggestion.work_item = item
            suggestion.save(update_fields=["status", "decided_at", "decided_by", "work_item"])
            audit(request, "work_item.created", item, "From an AI task suggestion")
        messages.success(request, f"{item.get_kind_display()} created.")
        return redirect(destination)
    return render(
        request,
        "ai/suggestion_accept.html",
        {"suggestion": suggestion, "form": form, "next": destination},
    )


@staff_required
@require_POST
def suggestion_dismiss(request, pk):
    suggestion, pending = pending_suggestion(request, pk)
    if pending:
        suggestion.status = TaskSuggestion.Status.DISMISSED
        suggestion.decided_at = timezone.now()
        suggestion.decided_by = request.user
        suggestion.save(update_fields=["status", "decided_at", "decided_by"])
        audit(request, "task_suggestion.dismissed", suggestion)
        messages.info(request, "Suggestion dismissed.")
    return redirect(next_url(request, suggestion_page(suggestion)))
