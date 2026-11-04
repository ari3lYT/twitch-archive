import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import archive as a


class ArchiveTests(unittest.TestCase):
    def test_schedule_boundaries(self):
        for t, expected in [('00:00', False), ('06:00', False), ('06:29', False),
                            ('06:30', True), ('12:00', True), ('23:29', True), ('23:30', False)]:
            with self.subTest(t=t):
                self.assertEqual(a.window(dt.datetime.fromisoformat('2026-10-26T' + t)), expected)

    def test_night_has_no_network(self):
        with patch.object(a, 'window', return_value=False), patch.object(a.urllib.request, 'urlopen') as net:
            with self.assertRaises(a.Paused):
                a.fetch('http://127.0.0.1/test')
            net.assert_not_called()

    def test_manual_pause_and_night_cannot_be_overridden(self):
        with patch.object(a, 'window', return_value=True), patch.object(a, 'control', return_value={'paused': True}):
            with self.assertRaises(a.Paused):
                a.guard()
        with patch.object(a, 'window', return_value=False), patch.object(a, 'control', return_value={'paused': False}):
            with self.assertRaises(a.Paused):
                a.guard()

    def test_priority_is_selected_between_downloads(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(a, 'STATE', Path(tmp)), patch.object(a, 'guard'), patch.object(a, 'discover', return_value=True):
            db = a.connect()
            for vid, priority in [('10', 0), ('20', 3)]:
                db.execute('INSERT INTO vod(id,meta,duration,stable_since,updated,priority) VALUES(?,?,?,?,?,?)', (vid, '{}', 10, 0, 0, priority))
            db.commit()
            order = []
            def download(conn, row):
                order.append(row['id'])
                conn.execute("UPDATE vod SET status='done' WHERE id=?", (row['id'],))
                conn.commit()
            with patch.object(a, 'download', side_effect=download):
                a.tick(db)
            self.assertEqual(order, ['20', '10'])
            db.close()

    def test_parallel_prefetch_keeps_order(self):
        completed = []
        segments = [SimpleNamespace(absolute_uri=str(i)) for i in range(5)]
        def fetch(url, **kwargs):
            index = int(url)
            time.sleep((4 - index) * .01)
            completed.append(index)
            return b'G' + bytes([index]) * 187
        with patch.object(a, 'guard'), patch.object(a, 'settings', return_value=dict(a.DEFAULTS, download_workers=5)), patch.object(a, 'fetch', side_effect=fetch):
            result = list(a.ordered_segments(segments, 0, {}))
        self.assertNotEqual(completed, list(range(5)))
        self.assertEqual([index for index, data in result], list(range(5)))
        self.assertEqual([data[1] for index, data in result], list(range(5)))

    def test_restart_truncates_uncommitted_tail_and_deduplicates(self):
        self._resume(crash=False)

    def test_256_streams_with_full_buffer_do_not_deadlock(self):
        # Later responses fill a deliberately tiny buffer before fragment zero.
        # The priority allowance must let the ordered writer make progress.
        segments = [SimpleNamespace(absolute_uri=str(i)) for i in range(256)]
        budgets = []
        real_budget = a.ByteBudget
        def budget(start, cancel):
            result = real_budget(start, cancel, limit=188 * 8)
            budgets.append(result)
            return result
        def fetch(url, budget, index, **kwargs):
            if index == 0:
                time.sleep(.1)
            for _ in range(4):
                budget.acquire(index, 188)
            return (b'G' + bytes([index]) * 187) * 4
        with patch.object(a, 'guard'), patch.object(a, 'ByteBudget', side_effect=budget), patch.object(a, 'fetch', side_effect=fetch):
            result = list(a.ordered_segments(segments, 0, {}, workers=256))
        self.assertEqual([blob[1] for _, blob in result], list(range(256)))
        self.assertEqual(budgets[0].used, 0)
        self.assertLessEqual(budgets[0].peak, budgets[0].limit + 188 * 4)

    def test_blocked_buffer_respects_cancellation(self):
        cancelled = threading.Event()
        budget = a.ByteBudget(0, cancelled, limit=1)
        with patch.object(a, 'guard'):
            budget.acquire(1, 1)
            cancelled.set()
            with self.assertRaises(a.Paused):
                budget.acquire(2, 1)

    def test_recommendation_rejects_excessive_recovery_and_uses_both_passes(self):
        def result(workers, bps, retries):
            return dict(workers=workers, bps=bps, fragment_retries=retries, ok=True, sha256='same')
        report = {'sample_segments': 256,
                  'results': [result(8, 29, 0), result(32, 37, 0), result(64, 44, 0), result(256, 90, 16)],
                  'confirmations': [result(8, 35, 0), result(32, 28.8, 1), result(64, 20, 2), result(256, 90, 16)]}
        self.assertEqual(a.recommend_performance(report), 32)

    def test_crash_between_media_fsync_and_sql_commit(self):
        self._resume(crash=True)

    def _resume(self, crash):
        with tempfile.TemporaryDirectory() as tmp:
            root, state = Path(tmp) / 'archive', Path(tmp) / 'state'
            root.mkdir()
            with patch.object(a, 'ROOT', root), patch.object(a, 'STATE', state), patch.object(a, 'guard'), patch.object(a, 'enough_space'):
                db = a.connect()
                meta = {'id': '1', 'createdAt': '2026-10-24T00:00:00Z', 'lengthSeconds': 30}
                db.execute('INSERT INTO vod(id,meta,duration,stable_since,updated,idx,offset,fingerprint,format) VALUES(?,?,?,?,?,?,?,?,?)',
                           ('1', json.dumps(meta), 30, 0, 0, 1, 188, 'abc', 'source'))
                db.commit()
                row = db.execute('SELECT * FROM vod').fetchone()
                dest = a.folder(row)
                dest.mkdir()
                one, two, three = [b'G' + bytes([x]) * 187 for x in (1, 2, 3)]
                (dest / 'recording.ts.part').write_bytes(one + b'uncommitted tail')
                pl = SimpleNamespace(segments=[SimpleNamespace(duration=10, absolute_uri=str(x)) for x in (1, 2, 3)])
                info = {'format_id': 'source', 'url': 'unused'}
                fake_fetch = lambda url, **kwargs: {'2': two, '3': three}[url]
                with patch.object(a, 'extract', return_value=info) as extract, patch.object(a, 'playlist', return_value=(pl, 'abc')), patch.object(a, 'validate', return_value={'ok': True}), patch.object(a, 'fetch', side_effect=fake_fetch) as fetch:
                    if crash:
                        class CrashDB:
                            def execute(self, *args):
                                return db.execute(*args)
                            def commit(self):
                                if db.execute('SELECT offset FROM vod').fetchone()[0] > 188:
                                    raise SystemExit('simulated power loss')
                                return db.commit()
                        with self.assertRaises(SystemExit):
                            a.download(CrashDB(), row)
                        db.close()
                        db = a.connect()
                        row = db.execute('SELECT * FROM vod').fetchone()
                        self.assertEqual(row['offset'], 188)
                        fetch.side_effect = fake_fetch
                    a.download(db, row)
                    self.assertEqual((dest / 'recording.ts').read_bytes(), one + two + three)
                    self.assertEqual(db.execute('SELECT status FROM vod').fetchone()[0], 'done')
                    # A crash after final rename can finish without Twitch access.
                    (dest / 'complete.json').unlink()
                    db.execute("UPDATE vod SET status='downloading'")
                    db.commit()
                    fetch.reset_mock()
                    extract.reset_mock()
                    a.download(db, db.execute('SELECT * FROM vod').fetchone())
                    fetch.assert_not_called()
                    extract.assert_not_called()
                    a.download(db, db.execute('SELECT * FROM vod').fetchone())
                    fetch.assert_not_called()
                    extract.assert_not_called()
                    self.assertEqual(len(list(dest.glob('recording.ts'))), 1)
                db.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
