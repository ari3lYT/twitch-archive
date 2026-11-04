#!/usr/bin/env python3
"""Real dashboard UI, synthetic read-only controller. Binds loopback and accesses no NAS/Twitch."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'archiver'));sys.path.insert(0,str(ROOT/'dashboard'))
import server
import agent
import archive


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory:
        base=Path(directory);state=base/'state';state.mkdir();media=base/'archive';media.mkdir()
        archive.STATE=agent.STATE=state;archive.ROOT=agent.ARCHIVE=server.ROOT=media
        server.STATE=base;server.ORIGIN='http://127.0.0.1:8926'
        # Demo-only cookie: the actual deployment retains Secure cookies and TLS.
        server.COOKIE='demo_twitch_session';server.PREFIX='/twitch/'
        password='demo-dashboard-only';salt=secrets.token_hex(16)
        (base/'auth.json').write_text(json.dumps({'username':'demo','salt':salt,
            'password_hash':hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),600000).hex(),
            'secret':secrets.token_hex(32),'version':'demo'}))
        database=archive.connect()
        for i,(title,duration,size) in enumerate([('Cozy building session',7200,8*1024**3),('Exploring a new world',5400,6*1024**3),('Community game night',9000,10*1024**3)],1):
            metadata={'title':title,'createdAt':f'2026-11-0{i}T15:00:00Z','game':{'displayName':'Minecraft'},
                      'viewCount':100+i*20,'description':'Fictional demo recording. No media or chat is included.'}
            database.execute('INSERT INTO vod(id,status,duration,offset,meta,stable_since,updated) VALUES(?,?,?,?,?,?,?)',
                             (str(10000+i),'done',duration,size,json.dumps(metadata),0,time.time()))
        database.commit();database.close()
        agent.command=lambda *args,**kwargs:json.dumps([{'State':{'Running':True,'Health':{'Status':'healthy'},'StartedAt':'2026-11-04T09:00:00Z'},'RestartCount':0,'Config':{'Image':'twitch-archive:demo'}}])
        original_status=agent.status
        def status():
            result=original_status();result['archive_path']='/demo/archive';result['disk']={'total':8*1024**4,'free':6*1024**4,'reserve':100*1024**3};return result
        def mock(request):
            if request.get('action')=='status':return status()
            if request.get('action')=='logs':return {'logs':'Synthetic demo: no external worker is connected.'}
            if request.get('action')=='transfer':return {'summary':None,'samples':[],'events':[]}
            raise ValueError('Read-only synthetic demo')
        server.agent=mock
        class DemoHandler(server.Handler):
            def do_GET(self):
                self.path=self.path.removeprefix('/twitch') or '/'
                super().do_GET()
            def do_POST(self):
                self.path=self.path.removeprefix('/twitch') or '/'
                super().do_POST()
            def send_header(self,keyword,value):
                if keyword.lower()=='set-cookie':value=value.replace('; Secure','')
                super().send_header(keyword,value)
        print('Read-only synthetic demo: http://127.0.0.1:8926/twitch/ (demo / '+password+')',flush=True)
        server.Server(('127.0.0.1',8926),DemoHandler).serve_forever()
