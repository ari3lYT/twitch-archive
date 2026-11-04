import json
import sqlite3
import time
from archive import STATE, ROOT, window

db = sqlite3.connect('file:' + str(STATE / 'queue.sqlite') + '?mode=ro', uri=True)
db.row_factory = sqlite3.Row
rows = db.execute('SELECT id,status,idx,offset,format,attempts,error,retry_at FROM vod ORDER BY CAST(id AS INTEGER)').fetchall()
print(json.dumps({'downloads_allowed_now': window(), 'archive_path_on_nas': str(ROOT),
    'counts': dict(db.execute('SELECT status,count(*) FROM vod GROUP BY status').fetchall()),
    'vods': [dict(r) for r in rows]}, ensure_ascii=False, indent=2))
