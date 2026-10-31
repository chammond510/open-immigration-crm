import re
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from crm.models import Matter

CARRIERS = (
    ("usps", "USPS"),
    ("ups", "UPS"),
    ("fedex", "FedEx"),
    ("dhl", "DHL"),
    ("other", "Other"),
)


def normalize_tracking_number(value):
    return re.sub(r"[\s-]", "", str(value or "")).upper()


class Shipment(models.Model):
    """A package sent for a matter, such as a filing to USCIS, and where it stands."""

    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        IN_TRANSIT = "in_transit", "In transit"
        OUT_FOR_DELIVERY = "out_for_delivery", "Out for delivery"
        DELIVERED = "delivered", "Delivered"
        PROBLEM = "problem", "Needs attention"
        RETURNED = "returned", "Returned to sender"

    # A shipment in one of these is settled, and no longer tracked.
    SETTLED = (Status.DELIVERED, Status.RETURNED)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    matter = models.ForeignKey(Matter, on_delete=models.CASCADE, related_name="shipments")
    description = models.CharField(
        "What was sent", max_length=255, help_text="For example, I-130 and I-130A package."
    )
    carrier = models.CharField(max_length=20, choices=CARRIERS, default="usps")
    tracking_number = models.CharField(max_length=64, blank=True, db_index=True)
    sent_to = models.CharField(
        max_length=255, blank=True, help_text="For example, USCIS Chicago lockbox."
    )
    sent_on = models.DateField(default=timezone.localdate)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SENT)
    status_detail = models.CharField(max_length=255, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    expected_on = models.DateField(null=True, blank=True)
    # Whether a tracking connection checks it, when it last did, and why the carrier
    # could not say.
    track = models.BooleanField(
        "Check its tracking automatically",
        default=True,
        help_text="With a tracking connection for its carrier.",
    )
    checked_at = models.DateTimeField(null=True, blank=True)
    tracking_problem = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-sent_on", "-created_at"]

    def __str__(self):
        return self.description

    def save(self, *args, **kwargs):
        self.tracking_number = normalize_tracking_number(self.tracking_number)
        super().save(*args, **kwargs)

    @property
    def settled(self):
        return self.status in self.SETTLED
