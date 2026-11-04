"""Unprivileged dashboard; all NAS control goes through a narrow Unix socket."""
import base64
from collections import defaultdict
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit

APP = Path(__file__).parent
STATE = Path(os.environ.get('PANEL_STATE', '/panel-state'))
ROOT = Path(os.environ.get('MEDIA_ROOT', '/archive'))
SOCKET = os.environ.get('CONTROL_SOCKET', '/control/agent.sock')
ORIGIN = os.environ.get('PUBLIC_ORIGIN', 'https://archive.example.com')
PREFIX = '/twitch/'
COOKIE = '__Secure-twitch_archive_session'
AUTH_LOCK = threading.Lock()
CACHE_LOCK = threading.Lock()
CACHE = {'at': 0, 'data': None}
ATTEMPTS = defaultdict(list)
DOWNLOADS = threading.BoundedSemaphore(2)
PROBE_LOCK = threading.Lock()


def technical(detail):
    if detail['status'] != 'done' or not detail.get('complete'):
        return None
    folder = detail['folder']
    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{1,20}', folder):
        return None
    path = ROOT / folder / 'recording.ts'
    if path.is_symlink() or path.parent.is_symlink():
        return None
    with PROBE_LOCK:
        cache = STATE / 'probes'
        cache.mkdir(exist_ok=True)
        target = cache / (detail['id'] + '.json')
        if target.exists():
            return json.loads(target.read_text())
        result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
            'format=duration,size,bit_rate,format_name,start_time:stream=codec_type,codec_name,profile,width,height,r_frame_rate,avg_frame_rate,bit_rate,pix_fmt,color_space,channels,channel_layout,sample_rate',
            '-of', 'json', str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25)
        if result.returncode:
            return None
        value = json.loads(result.stdout)
        target.write_text(json.dumps(value))
        return value


def auth():
    return json.loads((STATE / 'auth.json').read_text())


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def unb64(data):
    return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))


def password_matches(password, cfg):
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(cfg['salt']), 600000).hex()
    return hmac.compare_digest(digest, cfg['password_hash'])


def sign_session(cfg):
    value = {'user': cfg['username'], 'expires': time.time() + 7 * 86400,
             'csrf': secrets.token_urlsafe(24), 'version': cfg['version']}
    payload = b64(json.dumps(value).encode())
    signature = b64(hmac.new(bytes.fromhex(cfg['secret']), payload.encode(), hashlib.sha256).digest())
    return payload + '.' + signature, value


def session(cookie):
    try:
        value = dict(x.strip().split('=', 1) for x in cookie.split(';') if '=' in x)[COOKIE]
        payload, signature = value.split('.', 1)
        cfg = auth()
        expected = b64(hmac.new(bytes.fromhex(cfg['secret']), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(unb64(payload))
        if data['expires'] < time.time() or data['version'] != cfg['version'] or data['user'] != cfg['username']:
            return None
        return data
    except (ValueError, KeyError, TypeError):
        return None


def agent(request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(50)
        sock.connect(SOCKET)
        sock.sendall(json.dumps(request).encode() + b'\n')
        with sock.makefile('rb') as f:
            raw = f.readline(4 * 1024**2)
    result = json.loads(raw)
    if not result.get('ok'):
        raise ValueError(result.get('error', 'Controller unavailable'))
    return result['data']


def snapshot():
    with CACHE_LOCK:
        if time.monotonic() - CACHE['at'] > 2:
            CACHE['data'] = agent({'action': 'status'})
            CACHE['at'] = time.monotonic()
        return CACHE['data']


def invalidate():
    with CACHE_LOCK:
        CACHE['at'] = 0


class Handler(BaseHTTPRequestHandler):
    server_version = 'Hikari'

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def end_headers(self):
        self.send_header('Cache-Control', 'private, max-age=86400' if getattr(self, 'cache_image', False) else 'no-store, private')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://static-cdn.jtvnw.net; connect-src 'self'; frame-src https://player.twitch.tv https://www.twitch.tv; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if getattr(self, 'attachment', None):
            self.send_header('Content-Disposition', 'attachment; filename="' + self.attachment + '"')
        super().end_headers()

    def reply(self, code, obj, cookie=None):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        if cookie is not None:
            self.send_header('Set-Cookie', COOKIE + '=' + cookie + '; Path=' + PREFIX + '; Secure; HttpOnly; SameSite=Strict; Max-Age=' + ('604800' if cookie else '0'))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ('/', '/app.js', '/style.css', '/favicon.svg'):
            name = {'/': 'index.html'}.get(path, path[1:])
            data = (APP / name).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', mimetypes.guess_type(name)[0] or 'application/octet-stream')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        user = session(self.headers.get('Cookie', ''))
        if not user:
            return self.reply(401, {'error': 'Требуется вход в панель'})
        try:
            if path == '/api/session':
                return self.reply(200, {'username': user['user'], 'csrf': user['csrf']})
            if path == '/api/status':
                return self.reply(200, snapshot())
            if path == '/api/logs':
                return self.reply(200, agent({'action': 'logs'}))
            match = re.fullmatch(r'/api/vods/([0-9]{1,20})(/download|/thumbnail|/metadata|/chat\.json|/chat\.txt|/chat\.html|/metrics\.json|/events\.jsonl|/actions\.jsonl|/download\.metrics\.json|/transfer)?', path)
            if match:
                if match[2] == '/transfer':
                    return self.reply(200, agent({'action':'transfer','id':match[1]}))
                detail = agent({'action': 'detail', 'id': match[1]})
                if match[2] == '/thumbnail':
                    folder = detail['folder']
                    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{1,20}', folder):
                        return self.reply(400, {'error': 'Некорректный путь'})
                    image = ROOT / folder / 'preview.jpg'
                    if not image.is_file() or image.is_symlink() or image.parent.is_symlink() or image.stat().st_size > 2 * 1024**2:
                        return self.reply(404, {'error': 'Превью пока не сохранено'})
                    blob = image.read_bytes()
                    self.cache_image = True
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', str(len(blob)))
                    self.end_headers()
                    self.wfile.write(blob)
                    return
                if match[2] in ('/metrics.json','/events.jsonl','/actions.jsonl','/download.metrics.json'):
                    return self.download(detail, artifact=match[2][1:])
                if match[2] in ('/chat.json', '/chat.txt', '/chat.html'):
                    return self.download(detail, chat_format=match[2].rsplit('.', 1)[1])
                if match[2] == '/download':
                    return self.download(detail)
                try:
                    detail['technical'] = technical(detail)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    detail['technical'] = None
                if match[2] == '/metadata':
                    self.attachment = 'twitch_' + match[1] + '_metadata.json'
                return self.reply(200, detail)
            self.reply(404, {'error': 'Не найдено'})
        except (OSError, ValueError, TimeoutError) as e:
            self.reply(503, {'error': 'Контроллер NAS временно недоступен. Очередь сохранена.'})

    def download(self, detail, chat_format=None, artifact=None):
        if detail['status'] != 'done' or not detail['complete']:
            return self.reply(409, {'error': 'Запись ещё не прошла проверку'})
        folder = detail['folder']
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{1,20}', folder):
            return self.reply(400, {'error': 'Некорректный путь'})
        filename, content_type = 'recording.ts', 'video/mp2t'
        expected = detail['complete']['bytes']
        attachment = 'twitch_' + detail['id'] + '.ts'
        if chat_format is not None:
            if chat_format not in ('json', 'txt', 'html'):
                return self.reply(400, {'error': 'Некорректный формат чата'})
            chat = detail.get('chat') or {}
            if chat.get('status') != 'done':
                return self.reply(409, {'error': 'Чат ещё не сохранён целиком или недоступен у Twitch'})
            filename = 'chat.' + chat_format
            expected = chat.get('files', {}).get(filename, {}).get('bytes')
            content_type = {'json': 'application/json', 'txt': 'text/plain', 'html': 'text/html'}[chat_format] + '; charset=utf-8'
            attachment = 'twitch_' + detail['id'] + '_' + filename
        if artifact is not None:
            if artifact not in ('metrics.json','events.jsonl','actions.jsonl','download.metrics.json'):
                return self.reply(400, {'error': 'Некорректный файл метаданных'})
            filename = artifact
            if artifact == 'download.metrics.json':
                if not detail.get('transfer_available'):
                    return self.reply(409, {'error': 'Статистика загрузки пока не записана'})
                expected = None
            else:
                item = detail.get('insights',{}).get('files',{}).get(artifact)
                if not item:
                    return self.reply(409, {'error': 'Метаданные ещё формируются'})
                expected = item['bytes']
            content_type = ('application/x-ndjson' if artifact.endswith('.jsonl') else 'application/json') + '; charset=utf-8'
            attachment = 'twitch_' + detail['id'] + '_' + artifact
        path = ROOT / folder / filename
        if artifact == 'download.metrics.json' and path.is_file():
            expected = path.stat().st_size
        if not path.is_file() or path.is_symlink() or path.parent.is_symlink() or path.stat().st_size != expected:
            return self.reply(409, {'error': 'Файл не совпадает с проверенной записью'})
        if not DOWNLOADS.acquire(blocking=False):
            return self.reply(429, {'error': 'Уже идут две загрузки файлов. Попробуйте позже.'})
        try:
            size = path.stat().st_size
            start, end, code = 0, size - 1, 200
            if self.headers.get('Range'):
                match = re.fullmatch(r'bytes=([0-9]+)-([0-9]*)', self.headers['Range'])
                if not match:
                    return self.reply(416, {'error': 'Некорректный диапазон'})
                start = int(match[1])
                end = min(size - 1, int(match[2])) if match[2] else size - 1
                if start >= size or end < start:
                    return self.reply(416, {'error': 'Диапазон за пределами файла'})
                code = 206
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Disposition', 'attachment; filename="' + attachment + '"')
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(end - start + 1))
            if code == 206:
                self.send_header('Content-Range', 'bytes %s-%s/%s' % (start, end, size))
            self.end_headers()
            with path.open('rb') as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = f.read(min(1024**2, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            DOWNLOADS.release()

    def do_POST(self):
        path = urlsplit(self.path).path
        if self.headers.get('Origin') != ORIGIN:
            return self.reply(403, {'error': 'Недопустимый источник запроса'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.reply(415, {'error': 'Ожидается JSON'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size < 2 or size > 8192:
                return self.reply(413, {'error': 'Некорректный размер запроса'})
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, TypeError):
            return self.reply(400, {'error': 'Некорректный запрос'})
        if path == '/api/login':
            ip = self.headers.get('X-Real-IP', self.client_address[0])
            with AUTH_LOCK:
                now = time.time()
                ATTEMPTS[ip] = [t for t in ATTEMPTS[ip] if now - t < 300]
                if len(ATTEMPTS[ip]) >= 8:
                    return self.reply(429, {'error': 'Слишком много попыток. Подождите пять минут.'})
                ATTEMPTS[ip].append(now)
                cfg = auth()
                password = str(data.get('password', ''))[:256]
                valid = password_matches(password, cfg) and hmac.compare_digest(str(data.get('username', '')).encode(), cfg['username'].encode())
                if not valid:
                    return self.reply(401, {'error': 'Неверное имя или пароль'})
                ATTEMPTS.pop(ip, None)
                token, user = sign_session(cfg)
            return self.reply(200, {'username': user['user'], 'csrf': user['csrf']}, cookie=token)
        user = session(self.headers.get('Cookie', ''))
        if not user:
            return self.reply(401, {'error': 'Требуется вход в панель'})
        if not hmac.compare_digest(self.headers.get('X-CSRF-Token', '').encode(), user['csrf'].encode()):
            return self.reply(403, {'error': 'Сессия изменилась. Обновите страницу.'})
        if path == '/api/logout':
            return self.reply(200, {'ok': True}, cookie='')
        if path == '/api/password':
            current, new = str(data.get('current', '')), str(data.get('new', ''))
            if len(new) < 14 or len(new) > 128:
                return self.reply(400, {'error': 'Новый пароль: от 14 до 128 символов'})
            with AUTH_LOCK:
                cfg = auth()
                if not password_matches(current[:256], cfg):
                    return self.reply(401, {'error': 'Текущий пароль неверен'})
                cfg['salt'] = secrets.token_hex(16)
                cfg['password_hash'] = hashlib.pbkdf2_hmac('sha256', new.encode(), bytes.fromhex(cfg['salt']), 600000).hex()
                cfg['version'] = secrets.token_hex(16)
                temp = STATE / 'auth.json.tmp'
                with temp.open('w') as f:
                    json.dump(cfg, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(temp, 0o600)
                os.replace(temp, STATE / 'auth.json')
            return self.reply(200, {'message': 'Пароль изменён. Войдите с новым паролем.'}, cookie='')
        if path in ('/api/action', '/api/settings'):
            action = data.get('action')
            if path == '/api/settings':
                action = 'settings'
            if action not in {'pause', 'resume', 'scan', 'retry', 'prioritize', 'restart', 'settings', 'metadata', 'benchmark'}:
                return self.reply(400, {'error': 'Команда не разрешена'})
            try:
                result = agent({'action': action, 'id': str(data.get('id', '')), 'settings': data.get('settings')})
                invalidate()
                return self.reply(200, result)
            except (OSError, ValueError, TimeoutError):
                return self.reply(503, {'error': 'Команда не выполнена. Проверьте состояние и повторите.'})
        self.reply(404, {'error': 'Не найдено'})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    slots = threading.BoundedSemaphore(24)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


if __name__ == '__main__':
    Server((os.environ.get('PANEL_HOST', '127.0.0.1'), int(os.environ.get('PANEL_PORT', '8925'))), Handler).serve_forever()
