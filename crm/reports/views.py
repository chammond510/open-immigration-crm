from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from crm.customfields.models import CustomField

from . import periods
from .metrics import build_report

staff_required = user_passes_test(lambda user: user.is_active and user.is_staff, login_url="login")


@staff_required
@require_GET
def operating_dashboard(request):
    period = periods.resolve(
        request.GET.get("period", ""), request.GET.get("start", ""), request.GET.get("end", "")
    )
    report = build_report(
        period,
        include_billing=request.user.has_perm("billing.access_billing"),
        source_key=request.GET.get("by", ""),
    )
    return render(
        request,
        "reports/dashboard.html",
        {
            "report": report,
            "today": timezone.localdate(),
            "period_choices": periods.CHOICES,
            "source_fields": CustomField.objects.filter(
                applies_to=CustomField.AppliesTo.CONTACT,
                field_type=CustomField.FieldType.CHOICE,
                is_active=True,
            ),
        },
    )
