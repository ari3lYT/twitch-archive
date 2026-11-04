"""Persistent transfer telemetry; sampling never participates in video commits."""
import datetime as dt
import json
import math
from pathlib import Path
import sqlite3
import threading
import time

ACTIVE = {'preparing', 'downloading', 'verifying'}
MOSCOW = dt.timezone(dt.timedelta(hours=3))


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.executescript('''CREATE TABLE IF NOT EXISTS transfer(vod TEXT PRIMARY KEY,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sample(seq INTEGER PRIMARY KEY,vod TEXT NOT NULL,at REAL NOT NULL,data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS sample_vod ON sample(vod,seq);
        CREATE TABLE IF NOT EXISTS event(seq INTEGER PRIMARY KEY,vod TEXT NOT NULL,at REAL NOT NULL,data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS event_vod ON event(vod,seq);''')
    db.commit()
    return db


from archive_schedule import finish_time


def project(summary, live=False, paused=False, allowed=True, now=None):
    result = dict(summary)
    now = time.time() if now is None else now
    stage = result.get('stage')
    stale = now-result.get('updated_at',0) > 15
    active = live and not paused and allowed and not stale and stage in ACTIVE
    extra = max(0,now-result.get('updated_at',now)) if active else 0
    result['active_seconds'] = result.get('active_seconds',0)+extra
    if active and stage == 'downloading':
        result['download_seconds'] = result.get('download_seconds',0)+extra
    if active and stage == 'verifying':
        result['verification_seconds'] = result.get('verification_seconds',0)+extra
    terminal = result.get('completed_at') if stage == 'done' else now
    result['wall_seconds'] = max(0,(terminal or now)-result.get('started_at',terminal or now))
    result['waiting_seconds'] = max(0,result['wall_seconds']-result.get('active_seconds',0))
    result['live'] = active
    result['stale'] = bool(live and stale)
    if not active:
        result['network_bps'] = 0
        result['committed_bps'] = 0
    offset, index, total = result.get('offset',0),result.get('segment',0),result.get('total_segments',0)
    estimate = offset/index*total if index > 0 and total >= index else None
    result['estimated_total_bytes'] = max(offset,estimate or 0) if estimate else None
    result['estimated_remaining_bytes'] = max(0,estimate-offset) if estimate else None
    result['average_bps'] = result.get('measured_bytes',0)/max(.001,result.get('download_seconds',0))
    # Recent durable throughput avoids counting cancelled/retried prefetch as progress.
    speed = result.get('smoothed_bps') or result.get('average_bps')
    result['stalled'] = bool(active and stage=='downloading' and now-result.get('last_progress_at',now)>30)
    if result['stalled'] or stage!='done' and result.get('download_seconds',0)<5:
        speed = None
    remaining = result.get('estimated_remaining_bytes')
    eta = remaining/speed if remaining is not None and speed and index >= 3 else None
    if stage == 'done': eta = 0
    if stage == 'verifying': eta = None
    result['eta_active_seconds'] = eta
    result['eta_at'] = finish_time(now,eta) if eta is not None and not paused and stage != 'done' else None
    result['eta_is_estimate'] = stage != 'done'
    return result


def read(path, vid, live=False, paused=False, allowed=True, limit=900):
    if not Path(path).exists(): return None
    db = sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=2)
    try:
        row = db.execute('SELECT data FROM transfer WHERE vod=?',(str(vid),)).fetchone()
        if not row: return None
        summary = project(json.loads(row[0]),live,paused,allowed)
        count = db.execute('SELECT count(*) FROM sample WHERE vod=?',(str(vid),)).fetchone()[0]
        stride = max(1, math.ceil(count/max(1,limit)))
        history=[]
        for i,(data,) in enumerate(db.execute('SELECT data FROM sample WHERE vod=? ORDER BY seq',(str(vid),))):
            if i%stride==0 or i==count-1: history.append(json.loads(data))
        events=[json.loads(x[0]) for x in db.execute('SELECT data FROM event WHERE vod=? ORDER BY seq DESC LIMIT 100',(str(vid),))][::-1]
        return {'summary':summary,'history':history,'events':events,'total_samples':count,'display_stride':stride}
    finally:
        db.close()


class NullTransfer:
    def phase(self,*args,**kwargs): pass
    def progress(self,*args,**kwargs): pass
    def network(self,*args): pass
    def event(self,*args,**kwargs): pass
    def close(self,*args): pass


class Transfer:
    def __init__(self, worker, row, interval=2):
        self.a, self.vid, self.interval = worker, str(row['id']), interval
        self.path = worker.STATE/'transfer.sqlite'
        self.folder = worker.folder(row)
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.monotonic, self.now = time.monotonic, time.time
        db = connect(self.path)
        previous = db.execute('SELECT data FROM transfer WHERE vod=?',(self.vid,)).fetchone()
        self.data = json.loads(previous[0]) if previous else {}
        db.close()
        initial = {'vod_id':self.vid,'title':json.loads(row['meta']).get('title'),'source':'live',
                   'started_at':self.now(),'completed_at':None,'active_seconds':0.,'download_seconds':0.,
                   'verification_seconds':0.,'network_bytes':0,'measured_bytes':0,'peak_bps':0.,
                   'fragment_retries':0,'resumptions':0,'total_segments':0,'stage':'preparing',
                   'offset':row['offset'],'segment':row['idx'],'baseline_bytes':row['offset'],'last_progress_at':self.now()}
        initial.update(self.data)
        initial.update(stage='preparing',offset=row['offset'],segment=row['idx'],updated_at=self.now(),network_bps=0,committed_bps=0)
        if previous: initial['resumptions'] = initial.get('resumptions',0)+1
        self.data = initial
        self.last_tick = self.monotonic()
        self.last_wire = self.data['network_bytes']
        self.last_bytes = self.data['measured_bytes']
        self.pending=[{'at':self.now(),'type':'resumed' if previous else 'started','data':{'offset':row['offset']}}]
        self.thread = threading.Thread(target=self._run,name='transfer-metrics',daemon=True)
        self.thread.start()

    def _account(self):
        now = self.monotonic()
        elapsed = max(0,now-self.last_tick)
        self.last_tick = now
        eligible = not self.a.STOP and self.a.window() and not self.a.control().get('paused')
        if self.data['stage'] in ACTIVE and eligible:
            self.data['active_seconds'] += elapsed
            if self.data['stage']=='downloading': self.data['download_seconds'] += elapsed
            if self.data['stage']=='verifying': self.data['verification_seconds'] += elapsed

    def phase(self,stage,**values):
        with self.lock:
            self._account()
            if stage != self.data['stage']:
                self.pending.append({'at':self.now(),'type':stage,'data':{}})
            self.data.update(stage=stage,**values)

    def progress(self,offset,segment,total=None):
        with self.lock:
            delta = max(0,offset-self.data['offset'])
            if offset < self.data['offset']:
                self.pending.append({'at':self.now(),'type':'source_reset','data':{'offset':offset}})
                self.data['smoothed_bps']=0
            self.data['measured_bytes'] += delta
            if delta: self.data['last_progress_at'] = self.now()
            self.data.update(offset=offset,segment=segment)
            if total is not None: self.data['total_segments']=total

    def network(self,size):
        with self.lock: self.data['network_bytes'] += size

    def event(self,kind,**details):
        with self.lock:
            self.pending.append({'at':self.now(),'type':kind,'data':details})
            if kind=='fragment_retry': self.data['fragment_retries'] += 1

    def _save(self,db,final=False):
        with self.lock:
            elapsed = max(.001,self.monotonic()-self.last_sample)
            self._account()
            wire = max(0,self.data['network_bytes']-self.last_wire)/elapsed
            useful = max(0,self.data['measured_bytes']-self.last_bytes)/elapsed
            self.last_wire,self.last_bytes = self.data['network_bytes'],self.data['measured_bytes']
            self.last_sample = self.monotonic()
            eligible = self.a.window() and not self.a.control().get('paused') and not self.a.STOP
            active = self.data['stage']=='downloading' and eligible
            if not active: wire=useful=0
            self.data['network_bps'],self.data['committed_bps'] = wire,useful
            if active:
                old = self.data.get('smoothed_bps',useful)
                self.data['smoothed_bps'] = .25*useful+.75*old
                self.data['peak_bps'] = max(self.data['peak_bps'],wire)
            self.data['updated_at']=self.now()
            if final and self.data['stage']=='done': self.data['completed_at']=self.now()
            summary=dict(self.data)
            point={k:summary.get(k) for k in ('updated_at','stage','offset','segment','network_bps','committed_bps','active_seconds')}
            point['at']=point.pop('updated_at')
            events,self.pending=self.pending,[]
        with db:
            db.execute('INSERT INTO transfer VALUES(?,?) ON CONFLICT(vod) DO UPDATE SET data=excluded.data',(self.vid,json.dumps(summary)))
            db.execute('INSERT INTO sample(vod,at,data) VALUES(?,?,?)',(self.vid,point['at'],json.dumps(point)))
            db.executemany('INSERT INTO event(vod,at,data) VALUES(?,?,?)',[(self.vid,e['at'],json.dumps(e)) for e in events])
        return summary

    def _run(self):
        try:
            db=connect(self.path)
            self.last_sample=self.monotonic()
            self._save(db)
            while not self.stop.wait(self.interval): self._save(db)
            self._save(db,final=True)
            self.folder.mkdir(parents=True,exist_ok=True)
            result=read(self.path,self.vid,limit=100000)
            self.a.atomic_json(self.folder/'download.metrics.json',result)
            db.close()
        except Exception as e:
            self.a.log.warning('transfer metrics unavailable vod=%s reason=%s',self.vid,type(e).__name__)

    def close(self,stage):
        self.phase(stage)
        self.stop.set()
        self.thread.join(timeout=12)
