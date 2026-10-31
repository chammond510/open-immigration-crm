import csv
from urllib.parse import quote

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from crm.notifications.services import quietly
from crm.roles import IMPORT_RECORDS, permission_required
from crm.services import audit
from crm.views import csv_safe

from .forms import ColumnsForm, UploadForm
from .models import SpreadsheetImport
from .spreadsheet import (
    MAX_ROWS,
    columns,
    counted,
    examples,
    parse,
    plan_import,
    purge_uploads,
    run_import,
    suggest_mapping,
    template_headers,
)

PREVIEW_ROWS = 100


def accepts(column):
    if column.key == "full_name":
        return "First and last name together; the last word becomes the last name."
    if column.kind == "choice":
        return ", ".join(label for _, label in column.choices) or "One of the field's choices."
    if column.kind == "date":
        return "A full date, written as you choose when matching columns."
    if column.kind == "yes_no":
        return "Yes or no."
    if column.kind == "staff":
        return "A staff member's email or username."
    return ""


def csv_response(filename):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(filename)
    response["Cache-Control"] = "private, no-store"
    # A byte-order mark, so spreadsheet apps read the file as UTF-8.
    response.write("﻿")
    return response


def preparing(request, pk):
    """The import, if it can still be set up; otherwise where to go instead."""
    spreadsheet = get_object_or_404(SpreadsheetImport, pk=pk)
    if spreadsheet.status == SpreadsheetImport.Status.IMPORTED:
        return None, redirect("import_report", pk=spreadsheet.pk)
    if spreadsheet.content is None:
        messages.error(request, "That file was deleted after a day. Upload it again.")
        return None, redirect("import_list")
    return spreadsheet, None


@permission_required(IMPORT_RECORDS)
@require_http_methods(["GET", "POST"])
def import_list(request):
    purge_uploads()
    form = UploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        spreadsheet = SpreadsheetImport.objects.create(
            filename=form.cleaned_data["file"].name[:255],
            created_by=request.user,
            content=form.text,
            headers=form.headers,
            row_count=form.row_count,
            mapping=suggest_mapping(form.headers),
            options={"assigned_to": request.user.pk},
        )
        return redirect("import_columns", pk=spreadsheet.pk)
    guide = [
        {"label": column.label, "record": column.record, "accepts": accepts(column)}
        for column in columns()
    ]
    return render(
        request,
        "imports/import_list.html",
        {
            "form": form,
            "imports": SpreadsheetImport.objects.select_related("created_by")[:25],
            "guide": guide,
            "max_rows": f"{MAX_ROWS:,}",
        },
    )


@permission_required(IMPORT_RECORDS)
@require_GET
def import_template(request):
    response = csv_response("contacts-and-matters-template.csv")
    csv.writer(response).writerow(template_headers())
    return response


@permission_required(IMPORT_RECORDS)
@require_http_methods(["GET", "POST"])
def import_columns(request, pk):
    spreadsheet, elsewhere = preparing(request, pk)
    if elsewhere:
        return elsewhere
    form = ColumnsForm(request.POST or None, spreadsheet=spreadsheet)
    if request.method == "POST" and form.is_valid():
        spreadsheet.mapping = form.mapping()
        spreadsheet.options = form.options()
        spreadsheet.save(update_fields=["mapping", "options"])
        return redirect("import_preview", pk=spreadsheet.pk)
    headers, rows = parse(spreadsheet.content)
    return render(
        request,
        "imports/import_columns.html",
        {
            "spreadsheet": spreadsheet,
            "form": form,
            "rows": zip(form.column_fields(), examples(headers, rows), strict=False),
        },
    )


@permission_required(IMPORT_RECORDS)
@require_http_methods(["GET", "POST"])
def import_preview(request, pk):
    spreadsheet, elsewhere = preparing(request, pk)
    if elsewhere:
        return elsewhere
    if "date_order" not in spreadsheet.options:
        # Staff confirm the columns and how dates are written before any preview.
        return redirect("import_columns", pk=spreadsheet.pk)
    if request.method == "POST":
        # One import can assign many records; it should not send an alert for each.
        with quietly():
            done = run_import(spreadsheet, request)
        messages.success(
            request,
            f"Imported {counted(done.contacts_created, 'contact')} and "
            f"{counted(done.matters_created, 'matter')}.",
        )
        return redirect("import_report", pk=spreadsheet.pk)
    plan = plan_import(spreadsheet)
    keys = set(spreadsheet.mapping.values())
    return render(
        request,
        "imports/import_preview.html",
        {
            "spreadsheet": spreadsheet,
            "plan": plan,
            "accepted": [row for row in plan.rows if not row.reason][:PREVIEW_ROWS],
            "preview_rows": PREVIEW_ROWS,
            "unmatchable": not keys & {"email", "phone"},
        },
    )


@permission_required(IMPORT_RECORDS)
@require_GET
def import_report(request, pk):
    spreadsheet = get_object_or_404(SpreadsheetImport.objects.select_related("created_by"), pk=pk)
    if spreadsheet.status != SpreadsheetImport.Status.IMPORTED:
        return redirect("import_columns", pk=spreadsheet.pk)
    return render(request, "imports/import_report.html", {"spreadsheet": spreadsheet})


@permission_required(IMPORT_RECORDS)
@require_GET
def import_skipped(request, pk):
    """The skipped rows as they were in the file, with the reason for each, to fix and retry."""
    spreadsheet = get_object_or_404(SpreadsheetImport, pk=pk)
    if spreadsheet.content is None:
        messages.error(request, "That file was deleted after a day.")
        return redirect("import_list")
    if spreadsheet.status == SpreadsheetImport.Status.IMPORTED:
        reasons = {item["row"]: item["reason"] for item in spreadsheet.skipped}
    else:
        reasons = {row.number: row.reason for row in plan_import(spreadsheet).skipped}
    headers, rows = parse(spreadsheet.content)
    stem = spreadsheet.filename.rsplit(".", 1)[0] or "import"
    response = csv_response(f"{stem} - skipped rows.csv")
    writer = csv.writer(response)
    writer.writerow(["Row", "Reason", *headers])
    for number, reason in sorted(reasons.items()):
        writer.writerow([number, reason, *(csv_safe(value) for value in rows[number - 2])])
    audit(request, "data.exported", spreadsheet, detail="spreadsheet_import_skipped_rows")
    return response


@permission_required(IMPORT_RECORDS)
@require_POST
def import_discard(request, pk):
    """Delete an import that was never confirmed, or an imported one's file."""
    spreadsheet = get_object_or_404(SpreadsheetImport, pk=pk)
    if spreadsheet.status == SpreadsheetImport.Status.IMPORTED:
        spreadsheet.content = None
        spreadsheet.save(update_fields=["content"])
        messages.success(request, "The uploaded file is deleted. The report is kept.")
        return redirect("import_report", pk=spreadsheet.pk)
    spreadsheet.delete()
    messages.success(request, "Import cancelled. Nothing was imported, and the file is deleted.")
    return redirect("import_list")
