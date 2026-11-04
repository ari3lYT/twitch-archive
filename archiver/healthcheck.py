import json
from pathlib import Path
import sys
import time
import sqlite3
from archive import window, control, settings

if not window() or control().get('paused'):
    sys.exit(0)
p = Path('/state/discovery.json')
if not p.exists() or time.time() - p.stat().st_mtime > max(1800, settings()['poll_seconds'] * 2 + 120):
    sys.exit(1)
db = sqlite3.connect('file:/state/queue.sqlite?mode=ro', uri=True)
if db.execute("SELECT count(*) FROM vod WHERE status='retry' AND attempts>=3").fetchone()[0]:
    sys.exit(1)
sys.exit(0)
