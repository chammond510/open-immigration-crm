"""JSON shapes for API records. Linked records appear as their IDs, the same
values the API accepts when writing; money is a string with two decimals."""

from django.urls import reverse


def iso(value):
    return value.isoformat() if value else None


def ref(value):
    return str(value) if value is not None else None


def money(value):
    return f"{value:.2f}"


def minutes(duration):
    return int(duration.total_seconds() // 60) if duration else None


def user_name(user):
    return (user.get_full_name() or user.username) if user else None


def web_url(request, name, pk):
    return request.build_absolute_uri(reverse(name, args=[pk]))


def user(person):
    return {"id": person.pk, "username": person.username, "name": user_name(person)}


def contact(record, request):
    return {
        "id": str(record.pk),
        "full_name": record.full_name,
        "first_name": record.first_name,
        "last_name": record.last_name,
        "email": record.email,
        "phone": record.phone,
        "date_of_birth": iso(record.date_of_birth),
        "preferred_language": record.preferred_language,
        "address_line1": record.address_line1,
        "address_line2": record.address_line2,
        "city": record.city,
        "region": record.region,
        "postal_code": record.postal_code,
        "country": record.country,
        "country_of_birth": record.country_of_birth,
        "nationality": record.nationality,
        "a_number": record.a_number,
        "current_immigration_status": record.current_immigration_status,
        "status": record.status,
        "stage": record.stage,
        "assigned_to": record.assigned_to_id,
        "notes": record.notes,
        "texting_opted_out_at": iso(record.texting_opted_out_at),
        "created_at": iso(record.created_at),
        "updated_at": iso(record.updated_at),
        "web_url": web_url(request, "contact_detail", record.pk),
    }


def matter(record, request):
    return {
        "id": str(record.pk),
        "matter_number": record.matter_number,
        "title": record.title,
        "case_type": record.case_type,
        "status": record.status,
        "primary_contact": str(record.primary_contact_id),
        "primary_contact_name": record.primary_contact.full_name,
        "assigned_to": record.assigned_to_id,
        "receipt_number": record.receipt_number,
        "priority_date": iso(record.priority_date),
        "filing_date": iso(record.filing_date),
        "description": record.description,
        "notes": record.notes,
        "parties": [
            {"contact": str(party.contact_id), "role": party.role, "notes": party.notes}
            for party in record.parties.all()
        ],
        "closed_at": iso(record.closed_at),
        "created_at": iso(record.created_at),
        "updated_at": iso(record.updated_at),
        "web_url": web_url(request, "matter_detail", record.pk),
    }


def work_item(record, request):
    return {
        "id": record.pk,
        "kind": record.kind,
        "title": record.title,
        "description": record.description,
        "priority": record.priority,
        "contact": ref(record.contact_id),
        "matter": ref(record.matter_id),
        "assigned_to": record.assigned_to_id,
        "scheduled_for": iso(record.scheduled_for),
        "is_completed": record.is_completed,
        "completed_at": iso(record.completed_at),
        "created_at": iso(record.created_at),
        "updated_at": iso(record.updated_at),
    }


def note(record, request):
    return {
        "id": record.pk,
        "kind": record.kind,
        "subject": record.subject,
        "body": record.body,
        "occurred_at": iso(record.occurred_at),
        "contact": ref(record.contact_id),
        "matter": ref(record.matter_id),
        "created_by": record.created_by_id,
    }


def communication(record, request):
    return {
        "id": str(record.pk),
        "channel": record.channel,
        "direction": record.direction,
        "occurred_at": iso(record.occurred_at),
        "duration_minutes": minutes(record.duration),
        "counterpart": record.counterpart,
        "subject": record.subject,
        "summary": record.summary,
        "contact": ref(record.contact_id),
        "matter": ref(record.matter_id),
        "logged_by": record.logged_by_id,
        "source_connection": ref(record.source_connection_id),
        "external_id": record.external_id,
        "created_at": iso(record.created_at),
        "updated_at": iso(record.updated_at),
    }


def time_entry(record, request):
    return {
        "id": record.pk,
        "user": record.user_id,
        "contact": ref(record.contact_id),
        "matter": ref(record.matter_id),
        "started_at": iso(record.started_at),
        "stopped_at": iso(record.stopped_at),
        "duration_minutes": None if record.is_running else minutes(record.duration),
        "note": record.note,
    }


def notice(record, request):
    return {
        "id": str(record.pk),
        "matter": str(record.matter_id),
        "contact": ref(record.contact_id),
        "kind": record.kind,
        "form_type": record.form_type,
        "receipt_number": record.receipt_number,
        "notice_date": iso(record.notice_date),
        "uscis_received_date": iso(record.uscis_received_date),
        "priority_date": iso(record.priority_date),
        "response_due_date": iso(record.response_due_date),
        "appointment_at": iso(record.appointment_at),
        "appointment_location": record.appointment_location,
        "document": ref(record.document_id),
        "notes": record.notes,
        "deadline": record.deadline_id,
        "appointment": record.appointment_id,
        "created_by": record.created_by_id,
        "created_at": iso(record.created_at),
        "updated_at": iso(record.updated_at),
    }


def status_check(record, request):
    return {
        "id": record.pk,
        "matter": str(record.matter_id),
        "receipt_number": record.receipt_number,
        "status_title": record.status_title,
        "status_text": record.status_text,
        "checked_at": iso(record.checked_at),
        "changed": record.changed,
        "recorded_by": record.recorded_by_id,
        "source_connection": ref(record.source_connection_id),
    }


def invoice(record, request):
    lines = list(record.lines.all())
    payments = list(record.payments.all())
    return {
        "id": str(record.pk),
        "number": record.number,
        "status": record.status,
        "contact": str(record.contact_id),
        "matter": ref(record.matter_id),
        "issue_date": iso(record.issue_date),
        "due_date": iso(record.due_date),
        "memo": record.memo,
        "notes": record.notes,
        "subtotal": money(record.subtotal),
        "earned_subtotal": money(record.subtotal_for("earned")),
        "advance_subtotal": money(record.subtotal_for("advance")),
        "amount_paid": money(record.amount_paid),
        "balance": money(record.balance),
        "is_overdue": record.is_overdue,
        "void_reason": record.void_reason,
        "lines": [
            {
                "kind": line.kind,
                "funds": line.funds,
                "description": line.description,
                "quantity": str(line.quantity),
                "unit_price": money(line.unit_price),
                "amount": money(line.amount),
                "time_entry": line.time_entry_id,
            }
            for line in lines
        ],
        "payments": [
            {
                "id": str(payment.pk),
                "received_on": iso(payment.received_on),
                "amount": money(payment.amount),
                "method": payment.method,
                "deposit_account": payment.deposit_account,
                "reference": payment.reference,
                "note": payment.note,
                "voided_at": iso(payment.voided_at),
                "void_reason": payment.void_reason,
            }
            for payment in payments
        ],
        "created_at": iso(record.created_at),
        "updated_at": iso(record.updated_at),
        "web_url": web_url(request, "invoice_detail", record.pk),
    }
