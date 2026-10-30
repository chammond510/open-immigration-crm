"""The operating dashboard as JSON, for the API."""

from decimal import Decimal


def plain(value):
    if isinstance(value, Decimal):
        return f"{value:.2f}" if value.as_tuple().exponent <= -2 else str(value)
    return value


def rows(items, *names):
    return [{name: plain(item[name]) for name in names} for item in items]


def report_data(report):
    period, previous = report["period"], report["previous"]
    data = {
        "period": {
            "key": period.key,
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
        },
        "previous_period": {"start": previous.start.isoformat(), "end": previous.end.isoformat()},
        "headline": {
            item["key"]: {"value": plain(item["value"]), "previous": plain(item["previous"])}
            for item in report["headline"]
        },
        "intake": {
            **{row["key"]: row["count"] for row in report["funnel"]},
            "retained_per_consult_percent": report["retained_per_consult"],
        },
        "lead_sources": {
            "field": report["source_field"].key if report["source_field"] else None,
            "rows": rows(report["sources"], "value", "leads", "retained"),
        },
        "matters": {
            "open": report["matters"]["open"],
            "opened": report["matters"]["opened"],
            "closed": report["matters"]["closed"],
            "by_status": rows(report["matters"]["by_status"], "key", "count"),
            "by_case_type": rows(report["matters"]["by_case_type"], "key", "count"),
            "by_phase": rows(report["matters"]["by_phase"], "workflow", "phase", "count"),
        },
        "deadlines": {
            "overdue": report["deadlines"]["overdue"],
            "due_next_7_days": report["deadlines"]["due_this_week"],
            "work_completed": report["deadlines"]["completed"],
            "open_rfe_noid_responses": report["deadlines"]["open_responses"],
        },
        "uscis": {
            "notices": rows(report["uscis"]["notices"], "key", "count"),
            "status_changes": report["uscis"]["status_changes"],
        },
        "communications": rows(report["communications"], "key", "count"),
        "time": {
            "hours": plain(report["time"]["hours"]),
            "unbilled_hours": plain(report["time"]["unbilled_hours"]),
            "by_user": rows(report["time"]["by_user"], "name", "hours"),
        },
    }
    billing = report["billing"]
    if billing:
        data["billing"] = {
            "currency": billing["currency"],
            **{
                key: plain(billing[key])
                for key in (
                    "invoiced",
                    "collected",
                    "collected_operating",
                    "collected_trust",
                    "outstanding",
                    "overdue",
                )
            },
            "aging": rows(billing["aging"], "key", "amount"),
        }
    return data
