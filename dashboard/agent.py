"""Privileged NAS controller. Unix socket only; no HTTP, shells, or arbitrary targets."""
import datetime as dt
import json
import logging
import os
from pathlib import Path
import re
import socket
import socketserver
import sqlite3
import struct
import subprocess
import threading
import time
from transfer_stats import read as read_transfer, project as project_transfer

BASE = Path(os.environ.get('PANEL_BASE', './panel'))
STATE = Path(os.environ.get('STATE_ROOT', './state'))
ARCHIVE = Path(os.environ.get('ARCHIVE_ROOT', './archive'))
DOCKER = os.environ.get('DOCKER_BINARY', 'docker')
CONTAINER = os.environ.get('ARCHIVE_CONTAINER', 'twitch-archive')
SOCKET = BASE / 'control' / 'agent.sock'
UID = int(os.environ.get('CONTROL_UID', str(os.getuid())))
GID = int(os.environ.get('CONTROL_GID', str(os.getgid())))
CHANNEL = os.environ.get('CHANNEL', 'example_channel')
LOCK = threading.Lock()
MSK = dt.timezone(dt.timedelta(hours=3))
SPECS = {'poll_seconds': (300, 60, 3600), 'settle_seconds': (600, 300, 3600),
         'socket_timeout': (20, 5, 60), 'min_free_gib': (100, 100, 5000),
         'rate_limit_mbps': (0, 0, 1000), 'retry_base_seconds': (60, 15, 300),
         'retry_max_seconds': (3600, 60, 7200), 'download_workers': (32, 1, 256)}


def settings():
    cfg = {k: v[0] for k, v in SPECS.items()}
    cfg.update(read_json(STATE / 'settings.json', {}))
    return cfg


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def command(args, timeout=15):
    r = subprocess.run([DOCKER] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if r.returncode:
        raise RuntimeError(r.stderr.decode(errors='replace')[:400])
    return r.stdout.decode(errors='replace')


def database(write=False):
    db = sqlite3.connect('file:' + str(STATE / 'queue.sqlite') + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


from archive_schedule import snapshot as schedule


def chat_info(vid, folder, job=None):
    info = dict(job or {'status': 'pending', 'count': 0})
    complete = read_json(ARCHIVE / folder / 'chat.complete.json', {})
    if complete.get('state') == 'done' and str(complete.get('vod_id')) == str(vid):
        info.update(status='done', count=complete['message_count'], files=complete['files'],
                    source=complete.get('source'), pages=complete.get('pages'),
                    completed_at=complete.get('exported_at'), error=None)
    return info


def status():
    info = json.loads(command(['inspect', CONTAINER]))[0]
    running = info['State']['Running']
    control = read_json(STATE / 'control.json', {})
    activity = read_json(STATE / 'activity.json', {})
    discovery = read_json(STATE / 'discovery.json', {})
    db = database()
    rows = db.execute('SELECT * FROM vod ORDER BY CAST(id AS INTEGER) DESC LIMIT 2000').fetchall()
    counts = dict(db.execute('SELECT status,count(*) FROM vod GROUP BY status').fetchall())
    try:
        chat_jobs = {r['id']: dict(r) for r in db.execute('SELECT * FROM chat_job')}
    except sqlite3.OperationalError:
        chat_jobs = {}
    totals = dict(db.execute('SELECT status,coalesce(sum(offset),0) FROM vod GROUP BY status').fetchall())
    total_duration = db.execute("SELECT coalesce(sum(duration),0) FROM vod WHERE status='done'").fetchone()[0]
    db.close()
    metric_summaries = {}
    metric_path = STATE / 'transfer.sqlite'
    if metric_path.exists():
        try:
            with sqlite3.connect('file:'+str(metric_path)+'?mode=ro',uri=True,timeout=2) as metrics_db:
                metric_summaries = {vid:json.loads(data) for vid,data in metrics_db.execute('SELECT vod,data FROM transfer')}
        except (sqlite3.Error,ValueError):
            pass
    vods = []
    active = None
    for r in rows:
        v = dict(r)
        meta = json.loads(v.pop('meta'))
        folder = meta['createdAt'][:10] + '_' + v['id']
        saved = read_json(ARCHIVE / folder / 'metadata.json', {})
        is_active = running and not control.get('paused') and activity.get('state') in ('downloading','verifying') and activity.get('vod_id') == v['id'] and v['status'] == 'downloading'
        v.update({'title': meta['title'], 'created_at': meta['createdAt'], 'folder': folder,
                  'total_segments': saved.get('segments'), 'height': saved.get('height'), 'fps': saved.get('fps'),
                  'has_preview': (ARCHIVE / folder / 'preview.jpg').is_file(),
                  'chat': chat_info(v['id'], folder, chat_jobs.get(v['id'])),
                  'insights': read_json(ARCHIVE / folder / 'insights.complete.json', {}),
                  'transfer': project_transfer(metric_summaries[v['id']], is_active, bool(control.get('paused')), schedule()['allowed']) if v['id'] in metric_summaries else None,
                  'thumbnail_url': meta.get('previewThumbnailURL'), 'views': meta.get('viewCount'),
                  'game': (meta.get('game') or {}).get('displayName'), 'language': meta.get('language'),
                  'description': meta.get('description'), 'broadcast_type': meta.get('broadcastType'),
                  'active': is_active})
        if v['status'] == 'downloading' and not is_active:
            v['status'] = 'partial'
        if is_active:
            active = v['id']
        vods.append(v)
    selected = active or next((v['id'] for v in vods if v.get('transfer')), None)
    telemetry = read_transfer(metric_path, selected, live=bool(active), paused=bool(control.get('paused')), allowed=schedule()['allowed']) if selected else None
    speed = (telemetry or {}).get('summary', {}).get('network_bps') or 0
    disk = os.statvfs(ARCHIVE)
    cfg = settings()
    return {'at': time.time(), 'channel': CHANNEL, 'schedule': schedule(), 'settings': cfg,
            'twitch': discovery.get('profile', {}),
            'enrichment': read_json(STATE / 'enrichment.json', {}),
            'performance': read_json(STATE / 'performance.json', {}),
            'telemetry': telemetry,
            'container': {'name': CONTAINER, 'running': running, 'health': info['State'].get('Health', {}).get('Status', 'unknown'),
                          'started_at': info['State'].get('StartedAt'), 'restart_count': info['RestartCount'],
                          'image': info['Config']['Image']},
            'worker': {'paused': bool(control.get('paused')), 'active_vod_id': active, 'speed_bps': speed,
                       'activity': activity, 'discovery': discovery},
            'counts': counts, 'totals': {'saved_bytes': totals.get('done', 0), 'saved_seconds': total_duration,
                'partial_bytes': sum(size for state, size in totals.items() if state != 'done'), 'total_vods': sum(counts.values())},
            'disk': {'total': disk.f_blocks * disk.f_frsize, 'free': disk.f_bavail * disk.f_frsize,
                     'reserve': cfg['min_free_gib'] * 1024**3},
            'archive_path': str(ARCHIVE), 'vods': vods}


def write_control(paused=None, benchmark=False):
    cfg = read_json(STATE / 'control.json', {})
    if paused is not None:
        cfg['paused'] = paused
    if benchmark:
        cfg['benchmark_requested'] = time.time_ns()
    cfg.update(wake=time.time_ns(), updated_at=time.time())
    tmp = STATE / 'control.json.tmp'
    with tmp.open('w') as f:
        json.dump(cfg, f)
        f.flush()
        os.fsync(f.fileno())
    os.chown(tmp, UID, GID)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE / 'control.json')
    fd = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY)
    os.fsync(fd)
    os.close(fd)


def detail(vid):
    if not re.fullmatch(r'[0-9]{1,20}', vid):
        raise ValueError('Некорректный ID записи')
    db = database()
    row = db.execute('SELECT * FROM vod WHERE id=?', (vid,)).fetchone()
    try:
        chat_job = db.execute('SELECT * FROM chat_job WHERE id=?', (vid,)).fetchone()
    except sqlite3.OperationalError:
        chat_job = None
    db.close()
    if row is None:
        raise ValueError('Запись не найдена')
    meta = json.loads(row['meta'])
    folder = meta['createdAt'][:10] + '_' + vid
    return {'id': vid, 'status': row['status'], 'folder': folder, 'metadata': meta,
            'media': read_json(ARCHIVE / folder / 'metadata.json'),
            'chat': chat_info(vid, folder, dict(chat_job) if chat_job else None),
            'insights': read_json(ARCHIVE / folder / 'insights.complete.json', {}),
            'transfer_available': (ARCHIVE / folder / 'download.metrics.json').is_file(),
            'details': read_json(ARCHIVE / folder / 'details.json', {}), 'complete': read_json(ARCHIVE / folder / 'complete.json'), 'path': str(ARCHIVE / folder)}


def audit(action, vid=None):
    path = BASE / 'audit.jsonl'
    if path.exists() and path.stat().st_size > 5 * 1024**2:
        # Rotate our own operational log; do not touch archive files.
        os.replace(path, BASE / 'audit.previous.jsonl')
    with path.open('a') as f:
        f.write(json.dumps({'at': dt.datetime.now(MSK).isoformat(), 'action': action, 'vod_id': vid}) + '\n')


def dispatch(req):
    action = req.get('action')
    if action == 'status':
        return status()
    if action == 'transfer':
        vid = str(req.get('id',''))
        detail(vid)
        activity=read_json(STATE / 'activity.json',{})
        return read_transfer(STATE / 'transfer.sqlite',vid,live=activity.get('vod_id')==vid and activity.get('state') in ('downloading','verifying'), paused=bool(read_json(STATE / 'control.json',{}).get('paused')), allowed=schedule()['allowed'])
    if action == 'logs':
        # docker logs writes worker logging to stderr too.
        r = subprocess.run([DOCKER, 'logs', '--tail', '150', '--timestamps', CONTAINER], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
        return {'logs': re.sub(r'https?://\S+', '[URL скрыт]', r.stdout.decode(errors='replace')[-64000:])}
    if action == 'detail':
        return detail(str(req.get('id', '')))
    if action in ('pause', 'resume', 'scan'):
        write_control(True if action == 'pause' else False if action == 'resume' else None)
        if action == 'resume':
            command(['start', CONTAINER])
    elif action == 'benchmark':
        if not schedule()['allowed'] or read_json(STATE / 'control.json', {}).get('paused'):
            raise ValueError('Замер доступен в рабочее окно после снятия паузы')
        write_control(benchmark=True)
    elif action == 'metadata':
        if not schedule()['allowed'] or read_json(STATE / 'control.json', {}).get('paused'):
            raise ValueError('Обновление метаданных доступно в рабочее окно после снятия паузы')
        command(['exec', '-d', CONTAINER, 'python', '/app/enrich.py'])
    elif action == 'restart':
        command(['restart', '-t', '30', CONTAINER], timeout=45)
    elif action == 'settings':
        value = req.get('settings')
        if not isinstance(value, dict) or set(value) != set(SPECS):
            raise ValueError('Передайте все параметры настроек')
        for key, number in value.items():
            _, low, high = SPECS[key]
            if type(number) is not int or not low <= number <= high:
                raise ValueError('Значение %s вне допустимого диапазона' % key)
        if value['retry_max_seconds'] < value['retry_base_seconds'] * 2:
            raise ValueError('Максимальная задержка должна быть не меньше двух начальных')
        tmp = STATE / 'settings.json.tmp'
        with tmp.open('w') as f:
            json.dump(value, f)
            f.flush()
            os.fsync(f.fileno())
        os.chown(tmp, UID, GID)
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE / 'settings.json')
        write_control()
    elif action in ('retry', 'prioritize'):
        vid = str(req.get('id', ''))
        if vid != 'all' and not re.fullmatch(r'[0-9]{1,20}', vid):
            raise ValueError('Некорректный ID записи')
        db = database(write=True)
        with db:
            if action == 'retry':
                if vid == 'all':
                    changed = db.execute("UPDATE vod SET status='pending',retry_at=0,attempts=0,error=NULL WHERE status='retry'").rowcount
                else:
                    changed = db.execute("UPDATE vod SET status='pending',retry_at=0,attempts=0,error=NULL WHERE status='retry' AND id=?", (vid,)).rowcount
            else:
                if vid == 'all':
                    raise ValueError('Выберите одну запись')
                priority = db.execute('SELECT coalesce(max(priority),0)+1 FROM vod').fetchone()[0]
                changed = db.execute("UPDATE vod SET priority=? WHERE id=? AND status!='done'", (priority, vid)).rowcount
        db.close()
        write_control()
        if not changed:
            return {'ok': True, 'message': 'Изменений нет: состояние записи уже изменилось'}
    else:
        raise ValueError('Команда не разрешена')
    audit(action, req.get('id'))
    return {'ok': True, 'message': {'pause': 'Пауза включена. Позиция сохраняется.',
            'resume': 'Архивирование продолжится в разрешённое время.', 'scan': 'Проверка канала запрошена.',
            'restart': 'Архиватор перезапущен. Позиции сохранены.', 'retry': 'Повтор добавлен в очередь.',
            'prioritize': 'Запись будет следующей после текущей.',
            'metadata': 'Обновление метаданных и превью запущено на NAS.',
            'benchmark': 'Замер 1–256 потоков и повторная проверка стабильности запрошены. Очередь продолжится автоматически.',
            'settings': 'Настройки сохранены и применяются без сброса очереди.'}[action]}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(50)
        _, uid, _ = struct.unpack('3i', self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid not in (0, UID):
            return
        try:
            data = self.rfile.readline(4097)
            if len(data) > 4096:
                raise ValueError('Request too large')
            req = json.loads(data)
            with LOCK:
                result = dispatch(req)
            out = {'ok': True, 'data': result}
        except Exception as e:
            logging.exception('controller request failed')
            out = {'ok': False, 'error': str(e)[:500]}
        self.wfile.write(json.dumps(out, ensure_ascii=False).encode() + b'\n')


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET.exists():
        SOCKET.unlink()
    with Server(str(SOCKET), Handler) as server:
        os.chown(SOCKET, os.getuid(), GID)
        os.chmod(SOCKET, 0o660)
        server.serve_forever()
