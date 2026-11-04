import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import archive as a
import chat_archive as c


def node(ident, offset, text='hello'):
    return {'id': ident, 'contentOffsetSeconds': offset, 'createdAt': '2026-10-27T10:00:00Z',
            'commenter': {'id': '42', 'login': 'tester', 'displayName': 'Tester'},
            'message': {'fragments': [{'text': text, 'emote': None}], 'userBadges': [], 'userColor': '#ffffff'}}


def edge(ident, offset, cursor, text='hello'):
    return {'cursor': cursor, 'node': node(ident, offset, text)}


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.patches = [patch.object(a, 'ROOT', self.root), patch.object(a, 'STATE', self.state),
                        patch.object(a, 'guard'), patch.object(a, 'enough_space')]
        for p in self.patches: p.start()
        self.row = {'id': '123', 'duration': 100, 'meta': json.dumps({'title': 'Test <chat>', 'createdAt': '2026-10-27T10:00:00Z'})}
        self.dest = a.folder(self.row)

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def test_pause_resume_dedup_order_exports_and_safe_html(self):
        first = [edge('2', 20.125, 'a'), edge('1', 10, 'b', '<script>alert(1)</script>')]
        with patch.object(c, 'page', side_effect=[(first, True), a.Paused()]):
            with self.assertRaises(a.Paused): c.archive(self.row)
        with sqlite3.connect(self.dest / 'chat.sqlite') as db:
            self.assertEqual(db.execute('SELECT cursor,pages FROM checkpoint').fetchone(), ('b', 1))
            self.assertEqual(db.execute('SELECT count(*) FROM message').fetchone()[0], 2)
        self.assertFalse((self.dest / 'chat.complete.json').exists())
        second = [edge('2', 20.125, 'c'), edge('3', 30, 'd', 'line1\nline2')]
        with patch.object(c, 'page', return_value=(second, False)) as fetch:
            result = c.archive(self.row)
            fetch.assert_called_once_with('123', 'b')
        self.assertEqual(result['message_count'], 3)
        payload = json.loads((self.dest / 'chat.json').read_text())
        self.assertEqual([n['id'] for n in payload['messages']], ['1','2','3'])
        self.assertIn('00:00:20.125', (self.dest / 'chat.txt').read_text())
        document = (self.dest / 'chat.html').read_text()
        self.assertIn('&lt;script&gt;', document)
        self.assertNotIn('<script>', document)
        with patch.object(c, 'page') as fetch:
            c.archive(self.row)
            fetch.assert_not_called()

    def test_incomplete_bad_page_never_advances_checkpoint(self):
        bad = [edge('1', 10, 'a'), {'node': {'id': '2'}, 'cursor': 'b'}]
        with patch.object(c, 'page', return_value=(bad, True)):
            with self.assertRaises(ValueError): c.archive(self.row)
        with sqlite3.connect(self.dest / 'chat.sqlite') as db:
            self.assertEqual(db.execute('SELECT cursor,pages FROM checkpoint').fetchone(), (None, 0))
            self.assertEqual(db.execute('SELECT count(*) FROM message').fetchone()[0], 0)
        self.assertFalse((self.dest / 'chat.complete.json').exists())

    def test_unavailable_is_not_an_empty_complete_chat(self):
        with patch.object(c, 'page', side_effect=c.Unavailable('deleted')):
            with self.assertRaises(c.Unavailable): c.archive(self.row)
        self.assertFalse((self.dest / 'chat.complete.json').exists())

    def test_genuinely_empty_replay_is_complete(self):
        with patch.object(c, 'page', return_value=([], False)):
            result = c.archive(self.row)
        self.assertEqual(result['message_count'], 0)
        self.assertEqual(json.loads((self.dest / 'chat.json').read_text())['messages'], [])

    def test_repeated_cursor_never_marks_chat_complete(self):
        first = [edge('1', 10, 'a')]
        with patch.object(c, 'page', return_value=(first, True)):
            with self.assertRaises(ValueError): c.archive(self.row)
        self.assertFalse((self.dest / 'chat.complete.json').exists())

    def test_partial_export_recovers_offline_and_repairs_checksum(self):
        original = c.checked_file
        count = [0]
        def interrupted(path, writer):
            count[0] += 1
            if count[0] == 2: raise a.Paused()
            return original(path, writer)
        with patch.object(c, 'page', return_value=([edge('1', 10, 'a')], False)), patch.object(c, 'checked_file', side_effect=interrupted):
            with self.assertRaises(a.Paused): c.archive(self.row)
        self.assertFalse((self.dest / 'chat.complete.json').exists())
        with patch.object(c, 'page') as fetch:
            c.archive(self.row)
            fetch.assert_not_called()
            text = self.dest / 'chat.txt'
            body = text.read_bytes()
            text.write_bytes(b'X' + body[1:])
            c.archive(self.row)
            self.assertEqual(text.read_bytes(), body)
            fetch.assert_not_called()

    def test_chat_failure_does_not_requeue_or_modify_verified_video(self):
        db = a.connect()
        db.execute('INSERT INTO vod(id,meta,duration,stable_since,status,updated) VALUES(?,?,?,?,?,?)',
                   ('123', self.row['meta'], 100, 0, 'done', 0))
        db.commit()
        with patch.object(c, 'archive', side_effect=OSError('temporary network failure')):
            c.tick(db)
        self.assertEqual(db.execute('SELECT status FROM vod').fetchone()[0], 'done')
        self.assertEqual(db.execute('SELECT status FROM chat_job').fetchone()[0], 'retry')
        db.close()

    def test_window_guard_is_used_before_any_chat_network(self):
        with patch.object(a, 'guard', side_effect=a.Paused()), patch.object(a, 'fetch') as network:
            with self.assertRaises(a.Paused): c.archive(self.row)
            network.assert_not_called()


if __name__ == '__main__': unittest.main(verbosity=2)
