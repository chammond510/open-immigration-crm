from datetime import timedelta

from django import template
from django.utils import timezone

from crm.connect.scheduler import worker_is_running

from .. import assistant, memos, suggestions
from ..client import ai_connections
from ..forms import AskForm
from ..models import AISettings, ConsultMemo, Conversation

register = template.Library()

# Consults that started this recently still show their memo.
RECENT = timedelta(hours=3)


@register.inclusion_tag("ai/includes/consult_memos.html", takes_context=True)
def consult_memos_panel(context, contact):
    """Memos for a contact's upcoming consults, and a way to ask for one."""
    user = context["user"]
    on = AISettings.load().feature_on(memos.FEATURE.key)
    appointments = [
        item
        for item in contact.work_items.filter(
            kind="appointment",
            is_completed=False,
            scheduled_for__gte=timezone.now() - RECENT,
        ).select_related("contact", "assigned_to")
        if memos.is_consult(item)
    ]
    found = {
        memo.appointment_id: memo
        for memo in ConsultMemo.objects.filter(appointment__in=appointments).select_related(
            "requested_by", "connection"
        )
    }
    consults = [
        {
            "appointment": item,
            "memo": found.get(item.pk),
            "preparing": found.get(item.pk) is not None and memos.is_preparing(found[item.pk]),
        }
        for item in appointments
    ]
    preparing = any(consult["preparing"] for consult in consults)
    return {
        "request": context["request"],
        "user": user,
        "show": bool(consults) and (on or bool(found)),
        "on": on,
        "consults": consults,
        "connections": ai_connections(user) if on else [],
        "worker_running": worker_is_running() if preparing else True,
    }


@register.inclusion_tag("ai/includes/task_suggestions.html", takes_context=True)
def task_suggestions_panel(context, contact=None, matter=None, mine=False):
    """Suggested tasks waiting for review on a contact, a matter, or the dashboard."""
    user = context["user"]
    pending = suggestions.pending_for(user=user if mine else None, contact=contact, matter=matter)[
        :8
    ]
    return {
        "request": context["request"],
        "user": user,
        "suggestions": pending,
        "mine": mine,
    }


@register.inclusion_tag("ai/includes/assistant_panel.html", takes_context=True)
def assistant_panel(context, matter):
    """Ask the assistant about a matter, and the staff member's conversations about it."""
    user = context["user"]
    on = AISettings.load().feature_on(assistant.FEATURE.key)
    return {
        "request": context["request"],
        "user": user,
        "on": on,
        "matter": matter,
        "connections": ai_connections(user) if on else [],
        "form": AskForm(user=user, auto_id="id_ask_%s") if on else None,
        "conversations": Conversation.objects.filter(owner=user, matter=matter)[:5] if on else [],
    }
