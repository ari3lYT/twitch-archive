"""Durable, paginated public VOD chat sidecars. Uses the worker's safety guards."""
import datetime as dt
import fcntl
import hashlib
import html
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time

import archive as a

CHAT_CLIENT = 'kd1unb4b3q4t58fwlpcbzcbnm76a8fp'
CHAT_HASH = 'b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a'
SOURCE = 'Twitch VOD chat replay; all messages returned by Twitch, not deleted/unavailable messages'


class Unavailable(Exception):
    pass


def queue(db):
    db.execute('''CREATE TABLE IF NOT EXISTS chat_job (
        id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        retry_at REAL NOT NULL DEFAULT 0, count INTEGER NOT NULL DEFAULT 0,
        updated REAL NOT NULL DEFAULT 0, error TEXT)''')
    db.execute("INSERT OR IGNORE INTO chat_job(id) SELECT id FROM vod WHERE status='done'")
    db.commit()


def page(vid, cursor=None):
    variables = {'videoID': str(vid)}
    variables.update({'cursor': cursor} if cursor else {'contentOffsetSeconds': 0})
    payload = {'operationName': 'VideoCommentsByOffsetOrCursor', 'variables': variables,
               'extensions': {'persistedQuery': {'version': 1, 'sha256Hash': CHAT_HASH}}}
    value = json.loads(a.fetch('https://gql.twitch.tv/gql', json.dumps(payload).encode(),
        {'Client-ID': CHAT_CLIENT, 'Content-Type': 'application/json'}, 8 * 1024**2))
    if value.get('errors'):
        raise RuntimeError('Twitch chat query failed: ' + ','.join(str(x.get('message', 'error'))[:150] for x in value['errors']))
    if not isinstance(value.get('data'), dict) or 'video' not in value['data']:
        raise ValueError('Twitch chat response has no video field')
    video = value['data']['video']
    if video is None:
        raise Unavailable('VOD unavailable on Twitch; chat replay cannot be recovered')
    if str(video.get('id')) != str(vid):
        raise ValueError('chat belongs to a different VOD')
    comments = video.get('comments')
    if comments is None:
        raise Unavailable('Twitch does not provide chat replay for this VOD')
    edges, info = comments.get('edges'), comments.get('pageInfo')
    if not isinstance(edges, list) or not isinstance(info, dict) or type(info.get('hasNextPage')) is not bool:
        raise ValueError('incomplete chat pagination response')
    return edges, info['hasNextPage']


def local_db(dest):
    path = dest / 'chat.sqlite'
    if path.is_symlink():
        raise ValueError('unsafe chat database path')
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.executescript('''CREATE TABLE IF NOT EXISTS message (
        id TEXT PRIMARY KEY, offset REAL NOT NULL, created TEXT NOT NULL, raw TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS message_order ON message(offset,created,id);
        CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY CHECK(id=1), cursor TEXT,
            source_done INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO checkpoint(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS visited (cursor TEXT PRIMARY KEY);''')
    db.commit()
    a.sync_dir(dest)
    return db


def stamp(seconds):
    ms = max(0, int(float(seconds) * 1000))
    whole, ms = divmod(ms, 1000)
    hours, rest = divmod(whole, 3600)
    minutes, seconds = divmod(rest, 60)
    return '%02d:%02d:%02d.%03d' % (hours, minutes, seconds, ms)


def text_message(node):
    return ''.join(str(fragment.get('text', '')) for fragment in node['message']['fragments'])


def checked_file(path, writer):
    """Publish a whole export atomically; old valid exports survive interruption."""
    a.guard()
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.' + path.name + '.', suffix='.tmp', delete=False) as f:
        temp = Path(f.name)
        try:
            os.fchmod(f.fileno(), 0o644)
            writer(f)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        a.guard()
        os.replace(temp, path)
        a.sync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)
    return {'bytes': path.stat().st_size, 'sha256': a.checksum(path)}


def export(db, row, dest):
    count = db.execute('SELECT count(*) FROM message').fetchone()[0]
    meta = json.loads(row['meta'])
    info = {'schema': 'twitch-chat-v1', 'vod_id': row['id'], 'channel': a.CHANNEL,
            'title': meta.get('title'), 'video_created_at': meta.get('createdAt'),
            'video_duration_seconds': row['duration'], 'message_count': count,
            'source': SOURCE, 'offset_unit': 'seconds', 'created_at_timezone': 'UTC',
            'exported_at': dt.datetime.now(dt.timezone.utc).isoformat()}

    def messages():
        for (raw,) in db.execute('SELECT raw FROM message ORDER BY offset,created,id'):
            a.guard()
            a.enough_space()
            yield json.loads(raw)

    def json_export(f):
        f.write(json.dumps(info, ensure_ascii=False)[:-1] + ',"messages":[')
        for i, node in enumerate(messages()):
            if i:
                f.write(',')
            json.dump(node, f, ensure_ascii=False, separators=(',', ':'))
        f.write(']}\n')

    def txt_export(f):
        f.write('# ' + a.CHANNEL + ' / VOD ' + row['id'] + '\n# ' + SOURCE + '\n')
        f.write('# [HH:MM:SS.mmm] [UTC] author: message\n')
        for node in messages():
            author = node.get('commenter') or {}
            name = author.get('displayName') or author.get('login') or '[deleted user]'
            body = text_message(node).replace('\r', '\\r').replace('\n', '\\n')
            f.write('[%s] [%s] %s: %s\n' % (stamp(node['contentOffsetSeconds']), node['createdAt'], name, body))

    def html_export(f):
        f.write('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Чат VOD ' + row['id'] + '</title>')
        f.write('<style>body{margin:24px auto;max-width:960px;padding:0 18px;background:#11121a;color:#ddd;font:15px system-ui}h1{font-size:24px}p{color:#aaa}ol{list-style:none;padding:0}li{padding:9px 0;border-bottom:1px solid #292a36;line-height:1.6;overflow-wrap:anywhere}a{color:#bc9dff;font:13px monospace;margin-right:12px}time{color:#9093a5;font-size:11px}b{margin-right:8px}span{white-space:pre-wrap}</style>')
        f.write('<h1>' + html.escape(str(meta.get('title', row['id']))) + ' · чат</h1><p>VOD ' + row['id'] + ' · Сообщений: ' + str(count) + '. Таймкод ведёт на Twitch; полный текст доступен без интернета. Часовой пояс времени отправки: UTC.</p><ol>')
        for node in messages():
            author = node.get('commenter') or {}
            name = author.get('displayName') or author.get('login') or '[deleted user]'
            offset = node['contentOffsetSeconds']
            f.write('<li><a href="https://www.twitch.tv/videos/' + row['id'] + '?t=' + str(max(0, int(offset))) + 's" target="_blank" rel="noopener noreferrer">' + stamp(offset) + '</a><b>' + html.escape(str(name)) + '</b><time>' + html.escape(node['createdAt']) + '</time><br><span>' + html.escape(text_message(node)) + '</span></li>')
        f.write('</ol></html>\n')

    files = {}
    for name, writer in [('chat.json', json_export), ('chat.txt', txt_export), ('chat.html', html_export)]:
        files[name] = checked_file(dest / name, writer)
    marker = dict(info, state='done', files=files,
                  pages=db.execute('SELECT pages FROM checkpoint WHERE id=1').fetchone()[0])
    a.atomic_json(dest / 'chat.complete.json', marker)
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    return marker


def archive(row, progress=None):
    a.guard()
    dest = a.folder(row)
    dest.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink():
        raise ValueError('unsafe archive folder')
    with (dest / 'chat.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _archive(row, progress)


def _archive(row, progress=None):
    a.guard()
    a.enough_space()
    dest = a.folder(row)
    dest.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink():
        raise ValueError('unsafe archive folder')
    marker = dest / 'chat.complete.json'
    if marker.exists():
        complete = json.loads(marker.read_text())
        if str(complete['vod_id']) != str(row['id']):
            raise ValueError('chat completion marker has wrong VOD id')
        if all((dest / name).is_file() and not (dest / name).is_symlink() and (dest / name).stat().st_size == complete['files'][name]['bytes'] and a.checksum(dest / name) == complete['files'][name]['sha256']
               for name in ('chat.json', 'chat.txt', 'chat.html')):
            return complete
    db = local_db(dest)
    cursor_reset = False
    try:
        while True:
            a.guard()
            cursor, source_done, pages = db.execute('SELECT cursor,source_done,pages FROM checkpoint WHERE id=1').fetchone()
            if source_done:
                break
            try:
                edges, more = page(row['id'], cursor)
            except RuntimeError as e:
                if cursor and not cursor_reset and 'cursor' in str(e).lower():
                    # A changed/expired cursor can restart pagination safely;
                    # committed message IDs remain in the deduplicated store.
                    with db:
                        db.execute('UPDATE checkpoint SET cursor=NULL WHERE id=1')
                        db.execute('DELETE FROM visited')
                    cursor_reset = True
                    continue
                raise
            if more and (not edges or not isinstance(edges[-1].get('cursor'), str) or not edges[-1]['cursor']):
                raise ValueError('chat page has no next cursor')
            next_cursor = edges[-1]['cursor'] if more else None
            if more and (next_cursor == cursor or db.execute('SELECT 1 FROM visited WHERE cursor=?', (next_cursor,)).fetchone()):
                raise ValueError('chat pagination stopped making progress')
            # The page, deduplication and cursor are committed in one transaction.
            with db:
                for edge in edges:
                    node = edge.get('node')
                    if not isinstance(node, dict) or not isinstance(node.get('id'), str) or not node['id']:
                        raise ValueError('chat message has no unique id')
                    offset = node.get('contentOffsetSeconds')
                    if type(offset) not in (int, float) or not math.isfinite(offset) or not isinstance(node.get('createdAt'), str):
                        raise ValueError('chat message has invalid timestamp')
                    message = node.get('message')
                    if not isinstance(message, dict) or not isinstance(message.get('fragments'), list):
                        raise ValueError('chat message has no fragments')
                    db.execute('INSERT OR IGNORE INTO message VALUES(?,?,?,?)',
                               (node['id'], offset, node['createdAt'], json.dumps(node, ensure_ascii=False)))
                if cursor:
                    db.execute('INSERT INTO visited VALUES(?)', (cursor,))
                db.execute('UPDATE checkpoint SET cursor=?,source_done=?,pages=pages+1 WHERE id=1', (next_cursor, int(not more)))
            if progress:
                progress(db.execute('SELECT count(*) FROM message').fetchone()[0])
            a.enough_space()
        return export(db, row, dest)
    finally:
        db.close()


def tick(db, vid=None):
    queue(db)
    clause, params = ('AND v.id=?', (time.time(), str(vid))) if vid else ('', (time.time(),))
    rows = db.execute("SELECT v.*,j.attempts AS chat_attempts FROM vod v JOIN chat_job j ON j.id=v.id WHERE v.status='done' AND j.status!='done' AND j.retry_at<=? " + clause + ' ORDER BY CAST(v.id AS INTEGER) DESC', params).fetchall()
    for row in rows:
        a.guard()
        db.execute("UPDATE chat_job SET status='downloading',updated=? WHERE id=?", (time.time(), row['id']))
        db.commit()
        a.atomic_json(a.STATE / 'activity.json', {'state': 'chat', 'vod_id': row['id'], 'at': time.time()})
        def progress(count):
            db.execute('UPDATE chat_job SET count=?,updated=? WHERE id=?', (count, time.time(), row['id']))
            db.commit()
        try:
            result = archive(row, progress)
            db.execute("UPDATE chat_job SET status='done',count=?,error=NULL,attempts=0,retry_at=0,updated=? WHERE id=?", (result['message_count'], time.time(), row['id']))
            a.log.info('CHAT COMPLETE vod=%s messages=%s', row['id'], result['message_count'])
        except a.Paused:
            db.execute("UPDATE chat_job SET status='paused',updated=? WHERE id=?", (time.time(), row['id']))
            db.commit()
            raise
        except Exception as e:
            unavailable = isinstance(e, Unavailable)
            attempts = row['chat_attempts'] + 1
            delay = 21600 if unavailable else min(a.settings()['retry_max_seconds'], a.settings()['retry_base_seconds'] * 2**min(attempts, 10))
            reason = str(e) if unavailable else type(e).__name__
            db.execute('UPDATE chat_job SET status=?,attempts=?,retry_at=?,error=?,updated=? WHERE id=?',
                       ('unavailable' if unavailable else 'retry', attempts, time.time()+delay, reason, time.time(), row['id']))
            a.log.warning('chat vod=%s state=%s reason=%s', row['id'], 'unavailable' if unavailable else 'retry', reason)
        db.commit()
