"""Self-hosted Twitch VOD archiver with durable segment checkpoints."""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

import m3u8

CHANNEL = os.environ.get('CHANNEL', 'example_channel')
ROOT = Path(os.environ.get('ARCHIVE_ROOT', '/archive'))
STATE = Path(os.environ.get('STATE_ROOT', '/state'))
MIN_FREE = int(os.environ.get('MIN_FREE_BYTES', str(100 * 1024**3)))
STOP = False
CLIENT = 'kimne78kx3ncx6brgo4mv6wki5h1ko'
log = logging.getLogger('archive')
DEFAULTS = {'poll_seconds': 300, 'settle_seconds': 600, 'socket_timeout': 20,
            'min_free_gib': 100, 'rate_limit_mbps': 0, 'retry_base_seconds': 60,
            'retry_max_seconds': 3600, 'download_workers': 32}
MAX_WORKERS = 256
PREFETCH_BYTES = 256 * 1024**2
SETTINGS_CACHE = (0, None, DEFAULTS)
RATE_LOCK = threading.Lock()
RATE_NEXT = 0
RATE_VALUE = 0
BENCHMARKING = False
LAST_BENCHMARK = 0
BENCHMARK_REQUESTS_ENABLED = False
CURRENT_TRANSFER = None


class Paused(Exception):
    pass


class BenchmarkRequested(Paused):
    pass


from archive_schedule import allowed as window


def control():
    try:
        value = json.loads((STATE / 'control.json').read_text())
        return value if isinstance(value, dict) else {'paused': True}
    except FileNotFoundError:
        return {}
    except (ValueError, OSError):
        return {'paused': True}  # Fail closed if the control file is damaged.


def settings():
    global SETTINGS_CACHE
    stamp, path, cfg = SETTINGS_CACHE
    if path == STATE and time.monotonic() - stamp < 1:
        return cfg
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads((STATE / 'settings.json').read_text()))
    except (OSError, ValueError, TypeError):
        pass
    SETTINGS_CACHE = (time.monotonic(), STATE, cfg)
    return cfg


def guard():
    cfg = control()
    if STOP or not window() or cfg.get('paused'):
        raise Paused('outside download window, manually paused, or stopping')
    if BENCHMARK_REQUESTS_ENABLED and not BENCHMARKING and cfg.get('benchmark_requested', 0) > LAST_BENCHMARK:
        raise BenchmarkRequested('speed test requested at a durable checkpoint')


def stop(*_):
    global STOP
    STOP = True


def atomic_json(path, obj):
    # Discovery and metadata enrichment may publish the same JSON concurrently.
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.' + path.name + '.', suffix='.tmp', delete=False) as f:
        tmp = Path(f.name)
        os.fchmod(f.fileno(), 0o644)
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    sync_dir(path.parent)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def throttle(size, cancel=None):
    global RATE_NEXT, RATE_VALUE
    rate = settings()['rate_limit_mbps'] * 1000000 / 8
    if not rate:
        return 0
    started = time.monotonic()
    with RATE_LOCK:
        if rate != RATE_VALUE:
            RATE_NEXT = started
            RATE_VALUE = rate
        RATE_NEXT = max(started, RATE_NEXT) + size / rate
        target = RATE_NEXT
    while time.monotonic() < target:
        guard()
        if cancel is not None and cancel.is_set():
            raise Paused('prefetch cancelled')
        time.sleep(max(0, min(.2, target - time.monotonic())))
    return time.monotonic() - started


class ByteBudget:
    """Global backpressure, with priority for the next durable fragment.

    Later fragments may fill the budget while the first request is slow. The
    next fragment can exceed it by at most its 64 MiB response limit, avoiding
    a deadlock without letting 256 responses accumulate unbounded in memory.
    """
    def __init__(self, expected, cancel, limit=PREFETCH_BYTES):
        self.expected, self.cancel, self.limit = expected, cancel, limit
        self.condition = threading.Condition()
        self.held, self.used, self.peak, self.retries = {}, 0, 0, 0

    def acquire(self, index, size):
        started = time.monotonic()
        with self.condition:
            while self.used + size > self.limit and index != self.expected:
                guard()
                if self.cancel.is_set():
                    raise Paused('prefetch cancelled')
                self.condition.wait(.1)
            guard()
            if self.cancel.is_set():
                raise Paused('prefetch cancelled')
            self.held[index] = self.held.get(index, 0) + size
            self.used += size
            self.peak = max(self.peak, self.used)
        return time.monotonic() - started

    def release(self, index, committed=False):
        with self.condition:
            self.used -= self.held.pop(index, 0)
            if committed:
                self.expected = index + 1
            self.condition.notify_all()


def fetch(url, data=None, headers=None, limit=64 * 1024**2, cancel=None, budget=None, index=None):
    guard()
    req = urllib.request.Request(url, data=data, headers=headers or {})
    started = time.monotonic()
    throttled = 0
    with urllib.request.urlopen(req, timeout=settings()['socket_timeout']) as response:
        chunks, size = [], 0
        while True:
            guard()
            if cancel is not None and cancel.is_set():
                raise Paused('prefetch cancelled')
            if time.monotonic() - started - throttled > 60:
                raise TimeoutError('request exceeded 60 seconds')
            b = response.read(64 * 1024)
            if not b:
                break
            size += len(b)
            if size > limit:
                raise ValueError('response exceeds bounded memory limit')
            if budget is not None and CURRENT_TRANSFER is not None:
                CURRENT_TRANSFER.network(len(b))
            if budget is not None:
                throttled += budget.acquire(index, len(b))
            chunks.append(b)
            throttled += throttle(len(b), cancel)
        length = response.headers.get('Content-Length')
        if length is not None and size != int(length):
            raise IOError('truncated HTTP response')
    guard()
    return b''.join(chunks)


def ordered_segments(segments, start, headers, workers=None, metrics=None):
    """Bounded parallel prefetch, single ordered durable writer in the caller."""
    count = max(1, min(MAX_WORKERS, int(workers or settings()['download_workers'])))
    cancel = threading.Event()
    budget = ByteBudget(start, cancel)
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix='twitch-segment')

    def get(index):
        for attempt in range(3):
            guard()
            if cancel.is_set():
                raise Paused('prefetch cancelled')
            try:
                data = fetch(segments[index].absolute_uri, headers=headers, cancel=cancel, budget=budget, index=index)
                if not data or data[0] != 0x47 or len(data) % 188:
                    raise ValueError('invalid MPEG-TS segment %s' % index)
                return data
            except Paused:
                budget.release(index)
                raise
            except (OSError, TimeoutError) as e:
                budget.release(index)
                with budget.condition:
                    budget.retries += 1
                if CURRENT_TRANSFER is not None:
                    CURRENT_TRANSFER.event('fragment_retry', fragment=index, attempt=attempt+1, reason=type(e).__name__)
                if attempt == 2:
                    raise
                log.warning('retry fragment=%s attempt=%s reason=%s', index, attempt + 1, type(e).__name__)
                if cancel.wait(.5 * 2**attempt):
                    raise Paused('prefetch cancelled')
            except BaseException:
                budget.release(index)
                raise
    pending = {}
    following = start
    try:
        while following < min(len(segments), start + count):
            pending[following] = executor.submit(get, following)
            following += 1
        for index in range(start, len(segments)):
            guard()
            data = pending.pop(index).result()
            guard()
            yield index, data
            budget.release(index, committed=True)
            del data
            # Schedule only after the consumer committed this segment.
            desired = max(1, min(MAX_WORKERS, int(workers or settings()['download_workers'])))
            while following < len(segments) and len(pending) < desired:
                pending[following] = executor.submit(get, following)
                following += 1
    finally:
        cancel.set()
        for future in pending.values():
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        if metrics is not None:
            metrics.update(buffer_peak_bytes=budget.peak, fragment_retries=budget.retries)


def recommend_performance(report):
    # Recovery is normal on the internet. Reject modes that need >1% fragment
    # retries, and require two complete, checksum-identical passes.
    retry_limit = max(1, int(report['sample_segments'] * .01))
    first = {r['workers']: r for r in report['results'] if r.get('ok')
             and r.get('fragment_retries', 0) <= retry_limit}
    stable = []
    for repeat in report['confirmations']:
        initial = first.get(repeat['workers'])
        if initial and repeat.get('ok') and repeat.get('fragment_retries', 0) <= retry_limit and initial['sha256'] == repeat['sha256']:
            stable.append((min(initial['bps'], repeat['bps']), repeat['workers']))
    if not stable:
        raise RuntimeError('no mode passed repeat stability measurement')
    fastest = max(score for score, _ in stable)
    # The owner prefers higher concurrency; within measurement noise (3%),
    # choose the highest successfully repeated mode, never the largest input.
    return max(workers for score, workers in stable if score >= fastest * .97)


def run_performance(db):
    global BENCHMARKING, LAST_BENCHMARK
    request = control().get('benchmark_requested', 0)
    BENCHMARKING = True
    modes = [1, 4, 8, 16, 32, 64, 128, 256]
    report = {'request_id': request, 'state': 'running', 'at': time.time(),
              'modes': modes, 'results': [], 'confirmations': [], 'buffer_limit_bytes': PREFETCH_BYTES}
    atomic_json(STATE / 'activity.json', {'state': 'benchmark', 'at': time.time()})
    atomic_json(STATE / 'performance.json', report)
    try:
        guard()
        # Same 256 real fragments for every mode; no benchmark video on disk.
        vid = db.execute('SELECT id FROM vod WHERE duration>2700 ORDER BY duration LIMIT 1').fetchone()[0]
        info = extract(vid)
        headers = info.get('http_headers', {})
        manifest, _ = playlist(info['url'], headers)
        sample = list(manifest.segments)[:256]
        report.update(vod_id=vid, sample_segments=len(sample))
        reference = None

        def trial(workers, phase):
            digest, transferred, metrics = hashlib.sha256(), 0, {}
            started, cpu_started = time.monotonic(), time.process_time()
            peak_rss, last_publish = 0, 0
            stream = ordered_segments(sample, 0, headers, workers=workers, metrics=metrics)
            try:
                for index, blob in stream:
                    digest.update(blob)
                    transferred += len(blob)
                    try:
                        for line in Path('/proc/self/status').read_text().splitlines():
                            if line.startswith('VmRSS:'):
                                peak_rss = max(peak_rss, int(line.split()[1]) * 1024)
                                break
                    except OSError:
                        pass
                    if time.monotonic() - last_publish > 2:
                        report.update(progress={'workers': workers, 'phase': phase,
                            'segments': index + 1, 'total': len(sample)}, at=time.time())
                        atomic_json(STATE / 'performance.json', report)
                        last_publish = time.monotonic()
            finally:
                stream.close()
            elapsed = time.monotonic() - started
            checksum = digest.hexdigest()
            if reference is not None and checksum != reference:
                raise ValueError('parallel fragment checksum mismatch')
            result = {'workers': workers, 'bytes': transferred, 'seconds': round(elapsed, 3),
                'bps': transferred / elapsed, 'sha256': checksum, 'peak_rss_bytes': peak_rss,
                'cpu_seconds': round(time.process_time() - cpu_started, 3), 'ok': True, **metrics}
            log.info('speed test threads=%s bytes=%s seconds=%.3f rss=%s retries=%s',
                     workers, transferred, elapsed, peak_rss, metrics.get('fragment_retries', 0))
            return result

        for workers in modes:
            guard()
            try:
                result = trial(workers, 'comparison')
                if reference is None:
                    reference = result['sha256']
            except Paused:
                raise
            except Exception as e:
                result = {'workers': workers, 'ok': False, 'error': type(e).__name__, 'bps': 0}
            report['results'].append(result)
            report['at'] = time.time()
            atomic_json(STATE / 'performance.json', report)
        retry_limit = max(1, int(len(sample) * .01))
        candidates = sorted((r for r in report['results'] if r.get('ok') and r['fragment_retries'] <= retry_limit),
                            key=lambda r: r['bps'], reverse=True)[:3]
        for candidate in candidates:
            try:
                confirmation = trial(candidate['workers'], 'confirmation')
                report['confirmations'].append(confirmation)
            except Paused:
                raise
            except Exception as e:
                report['confirmations'].append({'workers': candidate['workers'], 'ok': False,
                                                 'error': type(e).__name__, 'bps': 0})
        report.update(state='done', recommended_workers=recommend_performance(report),
                      selection_policy='two_matching_sha256_passes; retries<=1%; min_bps; highest_within_3%',
                      rate_limit_mbps=settings()['rate_limit_mbps'])
    except Paused:
        report['state'] = 'paused'
    except Exception as e:
        report.update(state='error', error=type(e).__name__)
    finally:
        BENCHMARKING = False
        LAST_BENCHMARK = request
        report['at'] = time.time()
        atomic_json(STATE / 'performance.json', report)


def gql(query):
    result = json.loads(fetch('https://gql.twitch.tv/gql',
                             json.dumps({'query': query}).encode(),
                             {'Client-ID': CLIENT, 'Content-Type': 'application/json'}, 8 * 1024**2))
    if result.get('errors'):
        raise RuntimeError(str(result['errors']))
    return result['data']


def connect():
    STATE.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(STATE / 'queue.sqlite')
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('''CREATE TABLE IF NOT EXISTS vod (
        id TEXT PRIMARY KEY, meta TEXT NOT NULL, duration INTEGER NOT NULL,
        stable_since REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        idx INTEGER NOT NULL DEFAULT 0, offset INTEGER NOT NULL DEFAULT 0,
        fingerprint TEXT, format TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        retry_at REAL NOT NULL DEFAULT 0, error TEXT, updated REAL NOT NULL)''')
    if 'priority' not in {r[1] for r in db.execute('PRAGMA table_info(vod)')}:
        db.execute('ALTER TABLE vod ADD COLUMN priority INTEGER NOT NULL DEFAULT 0')
    db.commit()
    return db


def discover(db):
    cursor = None
    count = 0
    offline = False
    while True:
        after = ',after:' + json.dumps(cursor) if cursor else ''
        q = '{user(login:' + json.dumps(CHANNEL) + '){id login displayName description createdAt followers{totalCount} roles{isAffiliate isPartner} profileImageURL(width:150) broadcastSettings{title} stream{id viewersCount createdAt game{displayName} previewImageURL(width:640,height:360)} videos(first:100,type:ARCHIVE,sort:TIME' + after + '){edges{cursor node{id title createdAt publishedAt lengthSeconds description language broadcastType previewThumbnailURL(width:640,height:360) viewCount game{displayName}}} pageInfo{hasNextPage}}}}'
        user = gql(q)['user']
        if not user or user['login'].lower() != CHANNEL.lower():
            raise RuntimeError('Twitch channel unavailable or mismatched')
        offline = user['stream'] is None
        now = time.time()
        for edge in user['videos']['edges']:
            v = edge['node']
            duration = v['lengthSeconds']
            db.execute('''INSERT INTO vod(id,meta,duration,stable_since,updated) VALUES(?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET meta=excluded.meta,
                stable_since=CASE WHEN vod.duration != excluded.duration THEN excluded.stable_since ELSE vod.stable_since END,
                duration=excluded.duration, updated=excluded.updated''',
                       (v['id'], json.dumps(v, ensure_ascii=False), duration, now, now))
            count += 1
            import archive_insights
            archive_insights.observe(db, v, user)
            try:
                cache_preview(v)
            except Paused:
                raise
            except Exception as e:
                log.warning('preview unavailable vod=%s reason=%s', v['id'], type(e).__name__)
        db.commit()
        if not user['videos']['pageInfo']['hasNextPage']:
            break
        edges = user['videos']['edges']
        if not edges:
            raise RuntimeError('Twitch pagination returned empty page')
        cursor = edges[-1]['cursor']
    atomic_json(STATE / 'discovery.json', {'at': dt.datetime.now(dt.timezone.utc).isoformat(),
                'channel': CHANNEL, 'offline': offline, 'available_vods': count,
                'profile': {k: user[k] for k in ('id', 'login', 'displayName', 'description', 'createdAt', 'followers', 'roles', 'profileImageURL', 'broadcastSettings', 'stream')}})
    log.info('discovery channel=%s offline=%s available_vods=%s', CHANNEL, offline, count)
    return offline


def cache_preview(meta):
    url = meta.get('previewThumbnailURL')
    if not url or urllib.parse.urlsplit(url).hostname != 'static-cdn.jtvnw.net':
        return
    dest = ROOT / (meta['createdAt'][:10] + '_' + meta['id'])
    target = dest / 'preview.jpg'
    if target.exists():
        return
    guard()
    image = fetch(url, limit=2 * 1024**2)
    if not image.startswith(b'\xff\xd8'):
        raise ValueError('not a JPEG preview')
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='wb', dir=dest, prefix='.preview.', suffix='.tmp', delete=False) as f:
        tmp = Path(f.name)
        os.fchmod(f.fileno(), 0o644)
        f.write(image)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    sync_dir(dest)


def checked_process(args, timeout=120):
    guard()
    # Pipes go to files on NAS, with no media or unbounded logs on a container layer.
    with tempfile.TemporaryFile(dir=STATE) as out, tempfile.TemporaryFile(dir=STATE) as err:
        p = subprocess.Popen(args, stdout=out, stderr=err, start_new_session=True)
        start = time.monotonic()
        try:
            while p.poll() is None:
                guard()
                if time.monotonic() - start > timeout:
                    raise TimeoutError('subprocess timed out')
                time.sleep(.2)
            if p.returncode:
                err.seek(0)
                raise RuntimeError(err.read(3000).decode(errors='replace'))
            out.seek(0)
            return out.read(16 * 1024**2)
        finally:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait()


def extract(vid):
    data = json.loads(checked_process([sys.executable, '-m', 'yt_dlp', '--ignore-config',
        '--no-cache-dir', '--socket-timeout', str(settings()['socket_timeout']), '--extractor-retries', '2',
        '--no-playlist', '-f', 'best', '-J', 'https://www.twitch.tv/videos/' + vid]))
    if str(data['id']).lstrip('v') != vid or data.get('uploader_id', '').lower() != CHANNEL:
        raise RuntimeError('VOD identity mismatch')
    if data.get('is_live'):
        raise RuntimeError('VOD is still live')
    return data


def playlist(url, headers):
    pl = m3u8.loads(fetch(url, headers=headers, limit=8 * 1024**2).decode(), uri=url)
    if pl.is_variant or not pl.is_endlist or not pl.segments:
        raise RuntimeError('media playlist is incomplete or not final')
    if any(s.key and s.key.method != 'NONE' or s.byterange or s.init_section for s in pl.segments):
        raise RuntimeError('unsupported encrypted, byte-range or fMP4 playlist')
    fingerprint = hashlib.sha256(json.dumps([(urllib.parse.urlsplit(s.absolute_uri).path,
                      s.duration) for s in pl.segments]).encode()).hexdigest()
    return pl, fingerprint


def store_details(dest, info):
    keys = ('id', 'title', 'description', 'duration', 'uploader', 'uploader_id', 'uploader_url',
            'language', 'view_count', 'timestamp', 'upload_date', 'availability',
            'format', 'format_id', 'vcodec', 'acodec', 'tbr', 'vbr', 'abr',
            'filesize_approx', 'resolution', 'width', 'height', 'fps', 'protocol', 'ext',
            'chapters', 'categories', 'tags', 'thumbnail')
    data = {key: info[key] for key in keys if info.get(key) is not None}
    data.update(schema_version=2, fetched_at=dt.datetime.now(dt.timezone.utc).isoformat())
    dest.mkdir(parents=True, exist_ok=True)
    atomic_json(dest / 'details.json', data)


def enough_space():
    stat = os.statvfs(ROOT)
    reserve = max(MIN_FREE, settings()['min_free_gib'] * 1024**3)
    if stat.f_bavail * stat.f_frsize < reserve:
        raise RuntimeError('NAS free space below configured reserve')


def folder(row):
    meta = json.loads(row['meta'])
    return ROOT / (meta['createdAt'][:10] + '_' + row['id'])


def validate(path, expected):
    result = json.loads(checked_process(['ffprobe', '-v', 'error', '-show_entries',
        'format=duration:stream=codec_type,width,height,r_frame_rate,codec_name',
        '-of', 'json', str(path)], timeout=300))
    duration = float(result.get('format', {}).get('duration', 0))
    types = {s['codec_type'] for s in result.get('streams', [])}
    if not {'video', 'audio'} <= types or abs(duration - expected) > max(15, expected * .002):
        raise RuntimeError('media validation failed: duration=%s expected=%s streams=%s' % (duration, expected, types))
    return result


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        while True:
            guard()
            chunk = f.read(4 * 1024**2)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def finish(db, row, dest, final, expected, fmt, segments):
    probe = validate(final, expected)
    sha256 = checksum(final)
    atomic_json(dest / 'complete.json', {'vod_id': row['id'], 'bytes': final.stat().st_size, 'sha256': sha256,
        'completed_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'format_id': fmt,
        'segments': segments, 'probe': probe})
    db.execute("UPDATE vod SET status='done',error=NULL,retry_at=0,attempts=0 WHERE id=?", (row['id'],))
    db.commit()
    log.info('COMPLETE vod=%s bytes=%s sha256=%s', row['id'], final.stat().st_size, sha256)


def download(db, row):
    global CURRENT_TRANSFER
    from transfer_stats import Transfer, NullTransfer
    if row['status'] == 'done':
        return _download(db, row, NullTransfer())
    try:
        tracker = Transfer(sys.modules[__name__], row)
    except Exception as e:
        log.warning('metrics init failed: %s', type(e).__name__)
        tracker = NullTransfer()
    CURRENT_TRANSFER = tracker
    try:
        result = _download(db, row, tracker)
    except Paused:
        tracker.close('paused')
        raise
    except BaseException:
        tracker.close('retry')
        raise
    else:
        tracker.close('done')
        return result
    finally:
        CURRENT_TRANSFER = None


def _download(db, row, tracker):
    guard()
    enough_space()
    dest = folder(row)
    dest.mkdir(parents=True, exist_ok=True)
    sync_dir(ROOT)
    sync_dir(dest)
    part, final, marker = dest / 'recording.ts.part', dest / 'recording.ts', dest / 'complete.json'
    if marker.exists() and final.exists():
        completed = json.loads(marker.read_text())
        if completed['vod_id'] != row['id'] or final.stat().st_size != completed['bytes']:
            raise RuntimeError('completed archive differs from completion marker')
        db.execute("UPDATE vod SET status='done', error=NULL WHERE id=?", (row['id'],))
        db.commit()
        return
    if final.exists():
        saved = json.loads((dest / 'metadata.json').read_text())
        if row['idx'] != saved['segments'] or final.stat().st_size != row['offset']:
            raise RuntimeError('uncommitted final file size/checkpoint mismatch')
        tracker.phase('verifying')
        finish(db, row, dest, final, saved['playlist_seconds'], saved['format_id'], saved['segments'])
        return
    info = extract(row['id'])
    store_details(dest, info)
    headers = info.get('http_headers', {})
    pl, fingerprint = playlist(info['url'], headers)
    expected = sum(s.duration for s in pl.segments)
    if abs(expected - row['duration']) > max(15, row['duration'] * .002):
        raise RuntimeError('playlist duration differs from discovered VOD')
    fmt = str(info['format_id'])
    if row['fingerprint'] and (row['fingerprint'] != fingerprint or row['format'] != fmt):
        if final.exists():
            raise RuntimeError('source changed after final rename; validation needs investigation')
        log.warning('source changed vod=%s; restarting partial safely without mixing formats', row['id'])
        db.execute('UPDATE vod SET idx=0,offset=0 WHERE id=?', (row['id'],))
        db.commit()
        row = db.execute('SELECT * FROM vod WHERE id=?', (row['id'],)).fetchone()
    db.execute("UPDATE vod SET fingerprint=?,format=?,status='downloading' WHERE id=?", (fingerprint, fmt, row['id']))
    db.commit()
    atomic_json(dest / 'metadata.json', {'vod': json.loads(row['meta']), 'format_id': fmt,
        'width': info.get('width'), 'height': info.get('height'), 'fps': info.get('fps'),
        'playlist_seconds': expected, 'segments': len(pl.segments), 'source': 'Twitch VOD; best/source; no transcoding'})
    tracker.progress(row['offset'], row['idx'], len(pl.segments))
    tracker.phase('downloading')
    atomic_json(STATE / 'activity.json', {'vod_id': row['id'], 'at': time.time(), 'state': 'downloading'})
    if final.exists():
        if row['idx'] != len(pl.segments) or final.stat().st_size != row['offset']:
            raise RuntimeError('uncommitted final file size/checkpoint mismatch')
    else:
        if not part.exists() and row['offset']:
            raise RuntimeError('partial file missing; preserving checkpoint for investigation')
        with part.open('r+b' if part.exists() else 'w+b') as f:
            sync_dir(dest)
            if os.fstat(f.fileno()).st_size < row['offset']:
                raise RuntimeError('partial file is shorter than durable checkpoint')
            # Drop bytes written before a crash but not committed to SQLite.
            f.truncate(row['offset'])
            f.seek(row['offset'])
            log.info('download vod=%s format=%s resume_segment=%s/%s bytes=%s', row['id'], fmt, row['idx'], len(pl.segments), row['offset'])
            scanned = time.monotonic()
            wake = control().get('wake', 0)
            stream = ordered_segments(pl.segments, row['idx'], headers)
            log.info('parallel prefetch workers=%s; ordered durable writer', settings()['download_workers'])
            try:
                for index, segment in stream:
                    guard()
                    current_wake = control().get('wake', 0)
                    if time.monotonic() - scanned >= settings()['poll_seconds'] or current_wake != wake:
                        discover(db)
                        scanned = time.monotonic()
                        wake = current_wake
                    enough_space()
                    f.write(segment)
                    f.flush()
                    os.fsync(f.fileno())
                    db.execute('UPDATE vod SET idx=?,offset=?,error=NULL WHERE id=?', (index + 1, f.tell(), row['id']))
                    db.commit()
                    tracker.progress(f.tell(), index+1, len(pl.segments))
                    if index % 100 == 0:
                        log.info('progress vod=%s segment=%s/%s', row['id'], index + 1, len(pl.segments))
            finally:
                stream.close()
        guard()
        os.replace(part, final)
        sync_dir(dest)
    tracker.phase('verifying')
    atomic_json(STATE / 'activity.json', {'vod_id': row['id'], 'at': time.time(), 'state': 'verifying'})
    finish(db, row, dest, final, expected, fmt, len(pl.segments))


def tick(db):
    guard()
    offline = discover(db)
    import chat_archive
    chat_archive.tick(db)
    import archive_insights
    archive_insights.tick(db)
    if not offline:
        log.info('channel live; waiting for broadcast completion')
        return
    while True:
        now = time.time()
        row = db.execute("SELECT * FROM vod WHERE status!='done' AND retry_at<=? AND stable_since<=? ORDER BY priority DESC, CAST(id AS INTEGER) LIMIT 1", (now, now - settings()['settle_seconds'])).fetchone()
        if row is None:
            break
        try:
            download(db, row)
        except Paused:
            raise
        except Exception as e:
            attempts = row['attempts'] + 1
            cfg = settings()
            delay = min(cfg['retry_max_seconds'], cfg['retry_base_seconds'] * 2**min(attempts, 10))
            # Do not persist signed URLs or credentials in status/logs.
            error = type(e).__name__ + ': ' + str(e).split('https://')[0][:500]
            log.error('vod=%s attempt=%s retry_in=%s error=%s', row['id'], attempts, delay, error)
            db.execute("UPDATE vod SET status='retry',attempts=?,retry_at=?,error=? WHERE id=?", (attempts, time.time() + delay, error, row['id']))
            db.commit()
        chat_archive.tick(db, vid=row['id'])
        archive_insights.tick(db)


def main():
    global LAST_BENCHMARK, BENCHMARK_REQUESTS_ENABLED
    BENCHMARK_REQUESTS_ENABLED = True
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'worker.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not ROOT.is_dir() or not os.path.ismount(ROOT):
            raise RuntimeError('archive bind mount is absent; refusing local media writes')
        db = connect()
        try:
            previous = json.loads((STATE / 'performance.json').read_text())
            LAST_BENCHMARK = previous.get('request_id', 0)
            if previous.get('state') == 'running':
                # A reboot must resume the archive rather than leave a fake
                # running benchmark in the panel or start another large test.
                previous.update(state='error', error='InterruptedByRestart', at=time.time())
                atomic_json(STATE / 'performance.json', previous)
        except (OSError, ValueError):
            LAST_BENCHMARK = 0
        log.info('started channel=%s; schedule is configured by DOWNLOAD_START/DOWNLOAD_END', CHANNEL)
        while not STOP:
            wake = control().get('wake', 0)
            try:
                if window() and not control().get('paused'):
                    tick(db)
                    atomic_json(STATE / 'health.json', {'at': time.time(), 'status': 'ok', 'counts': dict(db.execute('SELECT status,count(*) FROM vod GROUP BY status').fetchall())})
                elif not window():
                    log.info('night pause; no network requests')
                else:
                    log.info('manual pause; no network requests')
            except BenchmarkRequested:
                log.info('paused at checkpoint for speed measurement')
                run_performance(db)
                wake = None  # Resume the queue immediately after the measurement.
            except Paused:
                log.info('paused safely at durable segment checkpoint')
            except Exception as e:
                log.error('scan failed: %s: %s', type(e).__name__, str(e).split('https://')[0][:500])
                atomic_json(STATE / 'health.json', {'at': time.time(), 'status': 'error', 'type': type(e).__name__})
            atomic_json(STATE / 'activity.json', {'at': time.time(), 'state': 'waiting'})
            for _ in range(settings()['poll_seconds']):
                if STOP or control().get('wake', 0) != wake:
                    break
                time.sleep(1)
        db.close()
        log.info('stopped safely')


if __name__ == '__main__':
    # Sidecar modules must share this worker's stop/benchmark flags.
    sys.modules['archive'] = sys.modules[__name__]
    main()
