"""Refresh public Twitch metadata only. Never download video or bypass pause/window."""
import fcntl
import time
import archive as a

with (a.STATE / 'enrichment.lock').open('w') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(0)
    a.atomic_json(a.STATE / 'enrichment.json', {'state': 'running', 'at': time.time(), 'completed': 0})
    db = a.connect()
    count = 0
    try:
        a.guard()
        a.discover(db)
        for row in db.execute('SELECT * FROM vod ORDER BY CAST(id AS INTEGER) DESC').fetchall():
            a.guard()
            try:
                info = a.extract(row['id'])
                a.store_details(a.folder(row), info)
                count += 1
                a.atomic_json(a.STATE / 'enrichment.json', {'state': 'running', 'at': time.time(), 'completed': count})
            except a.Paused:
                raise
            except Exception as e:
                a.atomic_json(a.STATE / 'enrichment.json', {'state': 'running', 'at': time.time(), 'completed': count, 'last_error': type(e).__name__})
        a.atomic_json(a.STATE / 'enrichment.json', {'state': 'done', 'at': time.time(), 'completed': count})
    except a.Paused:
        a.atomic_json(a.STATE / 'enrichment.json', {'state': 'paused', 'at': time.time(), 'completed': count})
    finally:
        db.close()
