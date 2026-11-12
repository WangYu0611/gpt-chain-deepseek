"""Deterministic Beijing-time work windows backed by an optional local calendar.

The Windows host does not need an IANA timezone database. Holiday dates are never
guessed or fetched at runtime: ``calendar_covered`` reports whether the supplied
calendar explicitly claims coverage of the current year.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any


BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
LOOKAHEAD_DAYS = 370
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")
_MODES = {"all_day", "weekday", "closed"}


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate calendar key: {key}")
        result[key] = value
    return result


def _day(value: Any) -> date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError("Calendar dates must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Invalid calendar date: {value}") from error


def _minutes(value: Any) -> int:
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        raise ValueError("Work window times must use HH:MM (00:00 through 23:59)")
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _date_set(values: Any, name: str) -> frozenset[date]:
    if not isinstance(values, list):
        raise ValueError(f"Calendar {name} must be a list")
    days = [_day(value) for value in values]
    if len(set(days)) != len(days):
        raise ValueError(f"Duplicate dates in calendar {name}")
    return frozenset(days)


class WorkCalendar:
    """Combine weekday windows, weekends, holidays, and explicit date overrides.

    The current local date determines its windows. Thus the default weekday
    schedule consists of [00:00, 08:00) and [18:00, midnight); a weekend or holiday
    can join those pieces into one uninterrupted multi-day interval.

    Inputs must be timezone-aware. ``next_open`` returns the input instant when
    already allowed and raises ``ValueError`` if no opening exists in 370 days.
    A ``None`` closing time means no closure was found within that horizon.
    """

    def __init__(self, schedule: dict, *, base_dir: Path | None = None):
        if not isinstance(schedule, dict):
            raise ValueError("schedule must be an object")
        if schedule.get("timezone", "Asia/Shanghai") != "Asia/Shanghai":
            raise ValueError("Only Asia/Shanghai is supported")
        self.always_on = schedule.get("always_on", False)
        if type(self.always_on) is not bool:
            raise ValueError("always_on must be true or false")
        self.start = _minutes(schedule.get("weekday_start", "18:00"))
        self.end = _minutes(schedule.get("weekday_end", "08:00"))
        if self.start == self.end:
            raise ValueError("Weekday start and end must differ")
        self.weekends_all_day = schedule.get("weekends_all_day", True)
        if type(self.weekends_all_day) is not bool:
            raise ValueError("weekends_all_day must be true or false")
        self.years: frozenset[int] = frozenset()
        self.holidays: frozenset[date] = frozenset()
        self.workdays: frozenset[date] = frozenset()
        self.overrides: dict[date, str] = {}
        self.holidays_file: Path | None = None
        raw_path = schedule.get("holidays_file")
        if not self.always_on and raw_path is not None and raw_path != "":
            if not isinstance(raw_path, str):
                raise ValueError("holidays_file must be a path string or null")
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = (Path(base_dir) if base_dir is not None else Path.cwd()) / path
            self.holidays_file = path.resolve()
            self._load(self.holidays_file)

    def _load(self, path: Path) -> None:
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("Holiday calendar exceeds 1 MiB")
            data = json.loads(path.read_text(encoding="utf-8-sig"),
                              object_pairs_hook=_strict_object)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"Cannot read holiday calendar: {path}") from error
        if not isinstance(data, dict):
            raise ValueError("Holiday calendar must be an object")
        unknown = set(data) - {"years", "holidays", "workdays", "overrides", "source", "notes", "updated_at"}
        if unknown:
            raise ValueError(f"Unknown holiday calendar fields: {', '.join(sorted(unknown))}")
        years = data.get("years", [])
        if (not isinstance(years, list) or
                any(type(year) is not int or not 1 <= year <= 9999 for year in years) or
                len(set(years)) != len(years)):
            raise ValueError("Calendar years must be a list of unique years")
        self.years = frozenset(years)
        self.holidays = _date_set(data.get("holidays", []), "holidays")
        self.workdays = _date_set(data.get("workdays", []), "workdays")
        if self.holidays & self.workdays:
            raise ValueError("A date cannot be both a holiday and a workday")
        overrides = data.get("overrides", {})
        if not isinstance(overrides, dict):
            raise ValueError("Calendar overrides must be an object")
        for raw_day, mode in overrides.items():
            if not isinstance(mode, str) or mode not in _MODES:
                raise ValueError("Override mode must be all_day, weekday, or closed")
            self.overrides[_day(raw_day)] = mode

    @staticmethod
    def _local(now: datetime) -> datetime:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("WorkCalendar requires a timezone-aware datetime")
        return now.astimezone(BEIJING)

    def _mode(self, day: date) -> str:
        if self.always_on:
            return "all_day"
        if day in self.overrides:
            return self.overrides[day]
        if day in self.holidays:
            return "all_day"
        if day in self.workdays:
            return "weekday"
        if self.weekends_all_day and day.weekday() >= 5:
            return "all_day"
        return "weekday"

    def _segments(self, day: date) -> list[tuple[datetime, datetime]]:
        midnight = datetime.combine(day, time.min, BEIJING)
        following = midnight + timedelta(days=1)
        mode = self._mode(day)
        if mode == "closed":
            return []
        if mode == "all_day":
            return [(midnight, following)]
        start, end = midnight + timedelta(minutes=self.start), midnight + timedelta(minutes=self.end)
        if self.start < self.end:
            return [(start, end)]
        result = []
        if self.end:
            result.append((midnight, end))
        result.append((start, following))
        return result

    def allowed(self, now: datetime) -> bool:
        current = self._local(now)
        return any(start <= current < end for start, end in self._segments(current.date()))

    def next_open(self, now: datetime) -> datetime:
        current = self._local(now)
        for offset in range(LOOKAHEAD_DAYS + 1):
            day = current.date() + timedelta(days=offset)
            for start, end in self._segments(day):
                if current < end:
                    return max(current, start)
        raise ValueError(f"No work window opens within {LOOKAHEAD_DAYS} days")

    def _closes_at(self, current: datetime) -> datetime | None:
        if self.always_on:
            return None
        segment = next(((start, end) for start, end in self._segments(current.date())
                        if start <= current < end), None)
        if segment is None:
            return current
        end = segment[1]
        for _ in range(LOOKAHEAD_DAYS):
            if end.time() != time.min:
                return end
            next_segments = self._segments(end.date())
            if not next_segments or next_segments[0][0] != end:
                return end
            end = next_segments[0][1]
        return None

    def seconds_until_close(self, now: datetime) -> float | None:
        current = self._local(now)
        closing = self._closes_at(current)
        return None if closing is None else (closing - current).total_seconds()

    def describe(self, now: datetime) -> dict:
        current = self._local(now)
        is_allowed = self.allowed(current)
        closing = self._closes_at(current) if is_allowed else None
        return {
            "timezone": "Asia/Shanghai",
            "now": current.isoformat(),
            "allowed": is_allowed,
            "next_open": self.next_open(current).isoformat(),
            "closes_at": closing.isoformat() if closing is not None else None,
            "calendar_covered": self.always_on or current.year in self.years,
            "always_on": self.always_on,
            "holidays_file": str(self.holidays_file) if self.holidays_file is not None else None,
        }
