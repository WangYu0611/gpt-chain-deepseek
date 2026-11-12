from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from unattended.calendar import BEIJING, WorkCalendar


def local(text):
    return datetime.fromisoformat(text).replace(tzinfo=BEIJING)


class WorkCalendarTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def calendar(self, data, **schedule):
        (self.root / "calendar.json").write_text(json.dumps(data), encoding="utf-8")
        return WorkCalendar({"holidays_file": "calendar.json", **schedule}, base_dir=self.root)

    def test_weekday_boundaries_and_timezone_conversion(self):
        calendar = WorkCalendar({})
        for moment, expected in [("2026-11-02T07:59:59", True),
                                 ("2026-11-02T08:00:00", False),
                                 ("2026-11-02T17:59:59", False),
                                 ("2026-11-02T18:00:00", True)]:
            self.assertEqual(calendar.allowed(local(moment)), expected)
        self.assertTrue(calendar.allowed(datetime(2026, 11, 2, 10, tzinfo=timezone.utc)))
        self.assertFalse(calendar.allowed(datetime(2026, 11, 2, 9, 59, tzinfo=timezone.utc)))

    def test_weekends_join_friday_evening_to_monday_morning(self):
        calendar = WorkCalendar({})
        now = local("2026-11-06T18:00:00")
        self.assertEqual(calendar.seconds_until_close(now), 62 * 3600)
        self.assertEqual(calendar.describe(now)["closes_at"], local("2026-11-09T08:00:00").isoformat())
        self.assertTrue(calendar.allowed(local("2026-11-08T12:00:00")))

    def test_next_open_on_weekday_and_already_open(self):
        calendar = WorkCalendar({})
        self.assertEqual(calendar.next_open(local("2026-11-02T09:00:00")), local("2026-11-02T18:00:00"))
        now = local("2026-11-02T03:45:00")
        self.assertEqual(calendar.next_open(now), now)
        self.assertEqual(calendar.seconds_until_close(local("2026-11-02T09:00:00")), 0)

    def test_holiday_and_makeup_workday_and_overrides(self):
        calendar = self.calendar({"years": [2026], "holidays": ["2026-11-02"],
                                  "workdays": ["2026-11-07"],
                                  "overrides": {"2026-11-02": "closed", "2026-11-07": "all_day"}})
        self.assertFalse(calendar.allowed(local("2026-11-02T19:00:00")))
        self.assertEqual(calendar.next_open(local("2026-11-02T19:00:00")), local("2026-11-03T00:00:00"))
        self.assertTrue(calendar.allowed(local("2026-11-07T12:00:00")))
        self.assertTrue(calendar.describe(local("2026-11-07T12:00:00"))["calendar_covered"])

    def test_makeup_workday_supersedes_weekend(self):
        calendar = self.calendar({"years": [2026], "workdays": ["2026-11-07"]})
        self.assertFalse(calendar.allowed(local("2026-11-07T12:00:00")))
        self.assertTrue(calendar.allowed(local("2026-11-07T20:00:00")))
        self.assertEqual(calendar.seconds_until_close(local("2026-11-06T20:00:00")), 12 * 3600)

    def test_holiday_joins_night_windows_across_year(self):
        calendar = self.calendar({"years": [2026, 2027], "holidays": ["2027-01-01"]})
        now = local("2026-12-31T18:00:00")
        self.assertEqual(calendar.describe(now)["closes_at"], local("2027-01-04T08:00:00").isoformat())

    def test_missing_and_expired_calendar_is_visible(self):
        self.assertFalse(WorkCalendar({}).describe(local("2026-11-02T12:00:00"))["calendar_covered"])
        calendar = self.calendar({"years": [2026], "holidays": ["2026-11-02"]})
        self.assertTrue(calendar.allowed(local("2026-11-02T12:00:00")))
        self.assertFalse(calendar.describe(local("2027-11-02T12:00:00"))["calendar_covered"])

    def test_daytime_window_and_weekends_option(self):
        calendar = WorkCalendar({"weekday_start": "09:30", "weekday_end": "17:15", "weekends_all_day": False})
        self.assertFalse(calendar.allowed(local("2026-11-07T09:29:59")))
        self.assertTrue(calendar.allowed(local("2026-11-07T09:30:00")))
        self.assertFalse(calendar.allowed(local("2026-11-07T17:15:00")))
        self.assertEqual(calendar.next_open(local("2026-11-07T18:00:00")), local("2026-11-08T09:30:00"))

    def test_window_ending_at_midnight_does_not_open_early(self):
        calendar = WorkCalendar({"weekday_start": "18:00", "weekday_end": "00:00"})
        self.assertEqual(calendar.seconds_until_close(local("2026-11-02T23:00:00")), 3600)
        self.assertFalse(calendar.allowed(local("2026-11-03T00:00:00")))

    def test_all_day_for_horizon_has_no_known_close(self):
        first = local("2026-11-02T12:00:00")
        holidays = [(first.date() + timedelta(days=n)).isoformat() for n in range(371)]
        calendar = self.calendar({"holidays": holidays})
        self.assertIsNone(calendar.seconds_until_close(first))
        self.assertIsNone(calendar.describe(first)["closes_at"])

    def test_no_opening_within_horizon_is_explicit(self):
        first = local("2026-11-02T12:00:00")
        overrides = {(first.date() + timedelta(days=n)).isoformat(): "closed" for n in range(371)}
        calendar = self.calendar({"overrides": overrides})
        with self.assertRaisesRegex(ValueError, "No work window"):
            calendar.next_open(first)

    def test_reject_naive_times_and_invalid_schedule(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            WorkCalendar({}).allowed(datetime(2026, 11, 2, 18))
        for schedule in [{"weekday_start": "24:00"}, {"weekday_start": "8:00"},
                         {"weekday_start": "08:00", "weekday_end": "08:00"},
                         {"weekends_all_day": 1}, {"timezone": "UTC"}, {"holidays_file": 3}]:
            with self.subTest(schedule=schedule), self.assertRaises(ValueError):
                WorkCalendar(schedule)

    def test_reject_calendar_conflicts_invalid_dates_and_modes(self):
        for data in [{"years": [True]}, {"years": [2026, 2026]},
                     {"holidays": ["2026-02-30"]}, {"holidays": ["2026-1-01"]},
                     {"holidays": ["2026-01-01", "2026-01-01"]},
                     {"holidays": ["2026-01-01"], "workdays": ["2026-01-01"]},
                     {"overrides": {"2026-11-02": "holiday"}}, {"overrides": []},
                     {"holidays": "2026-01-01"}, {"unexpected": True}]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.calendar(data)

    def test_reject_duplicate_json_keys_and_missing_file(self):
        (self.root / "calendar.json").write_text('{"years": [], "years": [2026]}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate calendar key"):
            WorkCalendar({"holidays_file": "calendar.json"}, base_dir=self.root)
        with self.assertRaisesRegex(ValueError, "Cannot read holiday calendar"):
            WorkCalendar({"holidays_file": "absent.json"}, base_dir=self.root)


if __name__ == "__main__":
    unittest.main()
