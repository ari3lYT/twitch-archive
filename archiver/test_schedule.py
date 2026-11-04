import datetime as dt
import os
import unittest
from unittest.mock import patch
import archive_schedule as schedule


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'ARCHIVE_TIMEZONE': 'Europe/Moscow',
                                          'DOWNLOAD_START': '06:30', 'DOWNLOAD_END': '23:30'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_wakeup_and_shutdown_margins(self):
        for hour, minute, expected in [(0, 0, False), (6, 0, False), (6, 29, False),
                                        (6, 30, True), (23, 29, True), (23, 30, False)]:
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(schedule.allowed(dt.datetime(2026, 11, 4, hour, minute)), expected)

    def test_eta_carries_work_across_shutdown(self):
        start = dt.datetime(2026, 11, 4, 23, 29, tzinfo=schedule.configuration()[0])
        result = schedule.finish_time(start.timestamp(), 120)
        self.assertEqual(dt.datetime.fromtimestamp(result, start.tzinfo), start.replace(day=5, hour=6, minute=31))

    def test_aware_inputs_are_converted_to_configured_zone(self):
        self.assertTrue(schedule.allowed(dt.datetime(2026, 11, 4, 4, tzinfo=dt.timezone.utc)))

    def test_overnight_or_empty_windows_fail_closed(self):
        with patch.dict(os.environ, {'DOWNLOAD_START': '23:30', 'DOWNLOAD_END': '06:30'}):
            with self.assertRaises(ValueError):
                schedule.allowed()
