"""Timezone boundaries for the China-calendar feedback ledger."""

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from core import feedback_archive, paths, release_windows
from core import evolution


class _UtcCrossDay(datetime):
    """2026-11-26 16:30 UTC is already the next China calendar day."""

    @classmethod
    def now(cls, tz=None):
        instant = cls(2026, 11, 26, 16, 30, tzinfo=timezone.utc)
        return instant.astimezone(tz) if tz else instant

    def astimezone(self, tz=None):
        return super().astimezone(timezone.utc if tz is None else tz)


class _UtcCalendarDay(date):
    @classmethod
    def today(cls):
        return cls(2026, 11, 26)


class EvolutionTimezoneTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='aq_evolution_timezone_')
        self.root = Path(self.temporary.name)
        self.old_root = paths.DATA_ROOT
        paths.DATA_ROOT = str(self.root)

    def tearDown(self):
        paths.DATA_ROOT = self.old_root
        self.temporary.cleanup()

    def test_cross_day_capture_uses_china_date_for_default_contract(self):
        self.assertEqual(_UtcCrossDay.now().astimezone().date(), date(2026, 11, 26))
        self.assertEqual(_UtcCrossDay.now(release_windows.ZH_TZ).date(), date(2026, 11, 27))
        feedback_archive.save(
            [{'aid': 'new', 'metrics': {'阅读': '2'}}],
            '2026-11-26T16:30:00+00:00')
        # A UTC runner still sees Nov 26, whereas the archive's business day is Nov 27.
        with mock.patch.object(evolution, 'datetime', _UtcCrossDay), \
                mock.patch.object(evolution, 'date', _UtcCalendarDay):
            report = evolution.contract()
        self.assertEqual(report['quality']['public_articles'], 1)
        self.assertEqual(evolution._date('2026-11-26T16:30:00+00:00'), date(2026, 11, 27))


if __name__ == '__main__':
    unittest.main()
