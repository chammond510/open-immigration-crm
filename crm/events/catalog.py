"""The CRM events an automation connection can subscribe to. See docs/WEBHOOKS.md."""

EVENT_GROUPS = {
    "Contacts and matters": {
        "contact.created": "A contact is created",
        "contact.updated": "A contact is changed",
        "matter.created": "A matter is opened",
        "matter.updated": "A matter is changed",
        "matter.status_changed": "A matter's status changes",
        "matter.phase_started": "A matter's workflow moves to a new phase",
    },
    "Work and notes": {
        "work_item.created": "A task, deadline, or appointment is added",
        "work_item.completed": "A task, deadline, or appointment is completed",
        "note.created": "A note is added",
        "communication.logged": "A call, text, email, or meeting is logged",
    },
    "USCIS": {
        "notice.logged": "A USCIS notice is logged",
        "case_status.changed": "A receipt's USCIS status is first recorded or changes",
    },
    "Intake": {
        "intake.submitted": "A client submits an intake form",
    },
    "Billing": {
        "invoice.issued": "An invoice is issued",
        "payment.recorded": "A payment is recorded",
    },
}

EVENT_TYPES = {name: label for group in EVENT_GROUPS.values() for name, label in group.items()}

# Each event's payload carries IDs and links; "full" adds the record itself.
DETAIL_IDS = "ids"
DETAIL_FULL = "full"
DETAIL_CHOICES = [
    (DETAIL_IDS, "Record IDs and links only. The receiver uses the API for details."),
    (DETAIL_FULL, "The full record, as the API returns it, including client details."),
]

TEST_EVENT = "webhook.test"
