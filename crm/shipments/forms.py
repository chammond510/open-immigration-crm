from datetime import datetime, time

from django import forms
from django.utils import timezone

from .models import Shipment


class NewShipmentForm(forms.ModelForm):
    class Meta:
        model = Shipment
        fields = ["description", "carrier", "tracking_number", "sent_to", "sent_on"]
        widgets = {"sent_on": forms.DateInput(attrs={"type": "date"})}


class ShipmentForm(NewShipmentForm):
    """Changes a shipment, including marking it delivered or returned by hand."""

    delivered_on = forms.DateField(
        label="Delivered on",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="For a shipment marked delivered by hand. Left blank, a new delivery is today.",
    )

    class Meta(NewShipmentForm.Meta):
        fields = [*NewShipmentForm.Meta.fields, "status", "track"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.delivered_at:
            self.initial["delivered_on"] = timezone.localdate(self.instance.delivered_at)

    def save(self, commit=True):
        shipment = super().save(commit=False)
        if shipment.status == Shipment.Status.DELIVERED:
            day = self.cleaned_data.get("delivered_on")
            # Left blank, a known delivery keeps its time, and a new one is today.
            if day is None and not shipment.delivered_at:
                day = timezone.localdate()
            if day and (
                not shipment.delivered_at or timezone.localdate(shipment.delivered_at) != day
            ):
                shipment.delivered_at = timezone.make_aware(datetime.combine(day, time(12)))
        else:
            shipment.delivered_at = None
        if commit:
            shipment.save()
        return shipment
