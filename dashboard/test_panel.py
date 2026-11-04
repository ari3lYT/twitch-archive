import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import agent
import server


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        salt = 'ab' * 16
        cfg = {'username': 'owner', 'salt': salt, 'password_hash': hashlib.pbkdf2_hmac('sha256', b'test password 123456', bytes.fromhex(salt), 600000).hex(), 'secret': 'cd' * 32, 'version': '1'}
        (root / 'auth.json').write_text(json.dumps(cfg))
        self.patches = [patch.object(server, 'STATE', root), patch.object(server, 'ROOT', root), patch.object(server, 'ORIGIN', 'https://test.local')]
        for p in self.patches:
            p.start()
        server.ATTEMPTS.clear()
        self.http = server.Server(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ''
        self.csrf = ''

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def request(self, path, payload=None, extra=None):
        headers = {'Cookie': self.cookie, 'Origin': 'https://test.local', 'X-CSRF-Token': self.csrf}
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        headers.update(extra or {})
        connection = HTTPConnection('127.0.0.1', self.http.server_port, timeout=10)
        connection.request('POST' if payload is not None else 'GET', path, json.dumps(payload) if payload is not None else None, headers)
        result = connection.getresponse()
        status, h, body = result.status, dict(result.getheaders()), result.read()
        connection.close()
        return status, h, body

    def login(self):
        status, h, body = self.request('/api/login', {'username': 'owner', 'password': 'test password 123456'})
        self.assertEqual(status, 200)
        self.cookie = h['Set-Cookie'].split(';')[0]
        self.csrf = json.loads(body)['csrf']
        return h

    def test_authentication_and_cookie_tampering(self):
        self.assertEqual(self.request('/api/status')[0], 401)
        self.assertEqual(self.request('/api/login', {'username': 'owner', 'password': 'wrong'})[0], 401)
        h = self.login()
        for protection in ('Secure', 'HttpOnly', 'SameSite=Strict', 'Path=/twitch/'):
            self.assertIn(protection, h['Set-Cookie'])
        self.assertEqual(self.request('/api/session')[0], 200)
        self.cookie = self.cookie[:-1] + ('A' if self.cookie[-1] != 'A' else 'B')
        self.assertEqual(self.request('/api/session')[0], 401)

    def test_csrf_origin_and_command_allowlist(self):
        self.login()
        with patch.object(server, 'agent', return_value={'ok': True}) as command:
            self.assertEqual(self.request('/api/action', {'action': 'pause'}, {'X-CSRF-Token': ''})[0], 403)
            self.assertEqual(self.request('/api/action', {'action': 'pause'}, {'Origin': 'https://evil.example'})[0], 403)
            self.assertEqual(self.request('/api/action', {'action': 'reboot-nas'})[0], 400)
            command.assert_not_called()
            self.assertEqual(self.request('/api/action', {'action': 'pause'})[0], 200)
            self.assertEqual(command.call_args.args[0]['action'], 'pause')

    def test_verified_file_range_and_unfinished_denial(self):
        self.login()
        folder = server.ROOT / '2026-10-24_123'
        folder.mkdir()
        (folder / 'recording.ts').write_bytes(b'G' * 564)
        record = {'status': 'done', 'complete': {'bytes': 564}, 'folder': folder.name, 'id': '123'}
        with patch.object(server, 'agent', return_value=record):
            code, headers, body = self.request('/api/vods/123/download', extra={'Range': 'bytes=188-375'})
            self.assertEqual(code, 206)
            self.assertEqual(body, b'G' * 188)
            self.assertEqual(headers['Content-Range'], 'bytes 188-375/564')
            record['status'] = 'pending'
            self.assertEqual(self.request('/api/vods/123/download')[0], 409)

    def test_chat_exports_require_completion_and_are_attachments(self):
        self.login()
        folder = server.ROOT / '2026-10-27_123'
        folder.mkdir()
        files = {'chat.json': b'{"messages":[]}', 'chat.txt': b'[00:00:01] Test: hello',
                 'chat.html': b'<!doctype html><p>hello</p>'}
        record = {'status': 'done', 'complete': {'bytes': 1}, 'folder': folder.name,
                  'id': '123', 'chat': {'status': 'done', 'files': {}}}
        for name, body in files.items():
            (folder / name).write_bytes(body)
            record['chat']['files'][name] = {'bytes': len(body)}
        with patch.object(server, 'agent', return_value=record):
            for name, body in files.items():
                code, headers, downloaded = self.request('/api/vods/123/' + name)
                self.assertEqual(code, 200)
                self.assertEqual(downloaded, body)
                self.assertIn('attachment;', headers['Content-Disposition'])
            record['chat']['status'] = 'unavailable'
            self.assertEqual(self.request('/api/vods/123/chat.json')[0], 409)

    def test_insight_and_transfer_exports_are_authenticated_and_verified(self):
        for endpoint in ('metrics.json', 'events.jsonl', 'actions.jsonl', 'transfer'):
            self.assertEqual(self.request('/api/vods/123/' + endpoint)[0], 401)
        self.login()
        folder = server.ROOT / '2026-10-27_123'
        folder.mkdir()
        record = {'status':'done','complete':{'bytes':1},'folder':folder.name,'id':'123',
                  'insights':{'files':{}},'transfer_available':True}
        with patch.object(server, 'agent', return_value=record):
            for name in ('metrics.json','events.jsonl','actions.jsonl','download.metrics.json'):
                body = b'{}' if name.endswith('.json') else b'{"type":"chat"}\n'
                (folder / name).write_bytes(body)
                record['insights']['files'][name] = {'bytes':len(body)}
                code, headers, downloaded = self.request('/api/vods/123/' + name)
                self.assertEqual(code,200)
                self.assertEqual(downloaded,body)
                self.assertIn('attachment;',headers['Content-Disposition'])
            (folder / 'events.jsonl').write_bytes(b'bad length')
            self.assertEqual(self.request('/api/vods/123/events.jsonl')[0],409)
            record['insights'] = {}
            self.assertEqual(self.request('/api/vods/123/metrics.json')[0],409)

    def test_settings_reserve_floor_and_no_power_controls(self):
        cfg = {key: values[0] for key, values in agent.SPECS.items()}
        cfg['min_free_gib'] = 99
        with self.assertRaises(ValueError):
            agent.dispatch({'action': 'settings', 'settings': cfg})
        cfg['min_free_gib'] = 100
        cfg['nas_start'] = '00:00'
        with self.assertRaises(ValueError):
            agent.dispatch({'action': 'settings', 'settings': cfg})
        with self.assertRaises(ValueError):
            agent.dispatch({'action': 'shell', 'command': 'whoami'})
        with self.assertRaises(ValueError):
            agent.detail('../../etc/passwd')


if __name__ == '__main__':
    unittest.main(verbosity=2)
