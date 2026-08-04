"""The per-sport fitness chart must not show a sport DECLINING on a day it was trained.

Jamie, 4 Aug 2026: he swam at 07:03 and his swim series still ended
['2026-08-04', 13.1] on a falling line, when a 49-load swim takes swim CTL from 13.4
UP to ~14.2. Cause was cache granularity, not staleness — the current season was
recomputed at most ONCE A DAY, so the 06:20 run built the series before the swim
existed and every later refresh reused it. Moving the refresh cron to */10 the same
morning could not have fixed it, and the app showed the swim in `recent` on the same
page as the declining line.
"""
import importlib.util
import unittest
from datetime import date
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "refresh-site-data.py"


def _load():
    spec = importlib.util.spec_from_file_location("refresh_site_data", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()
TODAY = date(2026, 8, 4)


class TodayFingerprint(unittest.TestCase):
    def test_a_new_activity_today_changes_the_fingerprint(self):
        before = [{"id": "i1", "icu_training_load": 144,
                   "start_date_local": "2026-08-04T05:25:00"}]
        after = before + [{"id": "i2", "icu_training_load": 49,
                           "start_date_local": "2026-08-04T07:03:00"}]
        self.assertNotEqual(mod._today_fingerprint(before, TODAY),
                            mod._today_fingerprint(after, TODAY))

    def test_a_revised_load_on_the_same_activity_changes_it(self):
        # ICU re-analyses an activity and the load moves; the chart must follow.
        a = [{"id": "i1", "icu_training_load": 40, "start_date_local": "2026-08-04T07:03"}]
        b = [{"id": "i1", "icu_training_load": 49, "start_date_local": "2026-08-04T07:03"}]
        self.assertNotEqual(mod._today_fingerprint(a, TODAY),
                            mod._today_fingerprint(b, TODAY))

    def test_yesterdays_activities_are_ignored(self):
        y = [{"id": "i0", "icu_training_load": 99, "start_date_local": "2026-08-03T09:00"}]
        self.assertEqual(mod._today_fingerprint(y, TODAY), "")

    def test_order_does_not_matter(self):
        a = [{"id": "i1", "icu_training_load": 1, "start_date_local": "2026-08-04T07:00"},
             {"id": "i2", "icu_training_load": 2, "start_date_local": "2026-08-04T08:00"}]
        self.assertEqual(mod._today_fingerprint(a, TODAY),
                         mod._today_fingerprint(list(reversed(a)), TODAY))

    def test_no_activities_is_stable_not_an_error(self):
        self.assertEqual(mod._today_fingerprint([], TODAY), "")
        self.assertEqual(mod._today_fingerprint(None, TODAY), "")


class RefreshCadenceWording(unittest.TestCase):
    """The app states its own refresh cadence, read from cron so it cannot drift."""

    def _line(self, spec):
        return [f"{spec} /root/.claude/cc-run python3 /path/scripts/refresh-site-data.py >> /log 2>&1"]

    def test_step_minute_schedule_is_described(self):
        # Returned None the moment the job went to */10, so the app said nothing.
        self.assertEqual(mod._refresh_cadence(self._line("*/10 * * * *")),
                         "every 10 minutes")

    def test_every_minute_reads_naturally(self):
        self.assertEqual(mod._refresh_cadence(self._line("*/1 * * * *")), "every minute")

    def test_the_old_two_hourly_wording_still_works(self):
        self.assertEqual(mod._refresh_cadence(self._line("20 6,8,10,12 * * *")),
                         "every 2 hours, 06:20–12:20")

    def test_hourly_at_past(self):
        self.assertEqual(mod._refresh_cadence(self._line("9 * * * *")),
                         "hourly, at 09 past")

    def test_a_line_for_another_job_is_not_claimed(self):
        self.assertIsNone(mod._refresh_cadence(["*/10 * * * * python3 /path/scripts/watchdog.py"]))

    def test_unparseable_schedule_says_nothing_rather_than_something_wrong(self):
        self.assertIsNone(mod._refresh_cadence(self._line("H/10 * * * *")))


if __name__ == "__main__":
    unittest.main()
