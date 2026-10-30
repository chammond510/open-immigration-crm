"""The date ranges reports cover. Each period also names the equal-length
period just before it, for comparison."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.utils import timezone
from django.utils.dateparse import parse_date

CHOICES = [
    ("week", "This week"),
    ("month", "This month"),
    ("last_month", "Last month"),
    ("quarter", "This quarter"),
    ("year", "This year"),
    ("30d", "Last 30 days"),
    ("90d", "Last 90 days"),
    ("custom", "Custom dates"),
]
DEFAULT = "month"
MAX_DAYS = 3 * 366


@dataclass(frozen=True)
class Period:
    key: str
    label: str
    start: date
    end: date

    @property
    def days(self):
        return (self.end - self.start).days + 1

    @property
    def start_at(self):
        return timezone.make_aware(datetime.combine(self.start, time.min))

    @property
    def end_at(self):
        """The first moment after the period, for half-open time filters."""
        return timezone.make_aware(datetime.combine(self.end + timedelta(days=1), time.min))

    def previous(self):
        end = self.start - timedelta(days=1)
        return Period("previous", "Previous period", end - timedelta(days=self.days - 1), end)

    def __str__(self):
        return f"{self.start:%b %-d, %Y} – {self.end:%b %-d, %Y}"


def date_or_none(value):
    try:
        return parse_date(value or "")
    except ValueError:
        return None


def resolve(key, start="", end="", today=None):
    """Return a Period for a choice key; unknown keys and bad dates fall back to the default."""
    today = today or timezone.localdate()
    labels = dict(CHOICES)
    if key == "custom":
        first, last = date_or_none(start), date_or_none(end)
        if first and last and first <= last and (last - first).days < MAX_DAYS:
            return Period("custom", labels["custom"], first, last)
        key = DEFAULT
    if key not in labels:
        key = DEFAULT
    if key == "week":
        first = today - timedelta(days=today.weekday())
    elif key == "month":
        first = today.replace(day=1)
    elif key == "last_month":
        last = today.replace(day=1) - timedelta(days=1)
        return Period(key, labels[key], last.replace(day=1), last)
    elif key == "quarter":
        first = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    elif key == "year":
        first = date(today.year, 1, 1)
    else:
        first = today - timedelta(days=int(key.removesuffix("d")) - 1)
    return Period(key, labels[key], first, today)
