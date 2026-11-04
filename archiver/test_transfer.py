import datetime as dt
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import transfer_stats as t


class TransferTests(unittest.TestCase):
    def test_eta_respects_night_and_is_not_claimed_during_verification(self):
        now=dt.datetime(2026,10,27,23,29,tzinfo=t.MOSCOW).timestamp()
        finish=t.finish_time(now,120)
        self.assertEqual(dt.datetime.fromtimestamp(finish,t.MOSCOW).strftime('%H:%M'),'06:31')
        summary={'stage':'downloading','updated_at':now,'started_at':now-100,'active_seconds':100,
                 'download_seconds':100,'measured_bytes':1000,'offset':1000,'segment':10,'total_segments':20,'smoothed_bps':100}
        live=t.project(summary,True,now=now)
        self.assertEqual(live['eta_active_seconds'],10)
        self.assertIsNone(t.project(dict(summary,stage='verifying'),True,now=now)['eta_active_seconds'])
        self.assertIsNone(t.project(summary,True,paused=True,now=now)['eta_at'])
        self.assertEqual(t.project(summary,False,now=now)['network_bps'],0)

    def test_stale_telemetry_never_reports_live_speed(self):
        sample={'stage':'downloading','updated_at':10,'started_at':0,'network_bps':200,'active_seconds':10,'download_seconds':10}
        view=t.project(sample,True,now=40)
        self.assertTrue(view['stale'])
        self.assertEqual(view['network_bps'],0)
        self.assertEqual(view['active_seconds'],10)

    def test_pause_restart_preserves_counters_without_charging_downtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state=root/'state';state.mkdir();folder=root/'vod'
            worker=SimpleNamespace(STATE=state,STOP=False,window=lambda:True,control=lambda:{},
                folder=lambda row:folder,atomic_json=lambda p,data:p.write_text(json.dumps(data)),
                log=SimpleNamespace(warning=lambda *args:None))
            row={'id':'123','meta':'{"title":"test"}','offset':0,'idx':0}
            first=t.Transfer(worker,row,interval=.03)
            first.phase('downloading');first.network(2000);first.progress(1000,1,10)
            time.sleep(.08);first.close('paused')
            old=t.read(state/'transfer.sqlite','123')['summary']
            self.assertEqual(old['offset'],1000)
            time.sleep(.15)
            row.update(offset=1000,idx=1)
            second=t.Transfer(worker,row,interval=.03)
            second.phase('downloading');second.network(2000);second.progress(2000,2,10)
            time.sleep(.08);second.close('done')
            result=t.read(state/'transfer.sqlite','123')
            self.assertEqual(result['summary']['resumptions'],1)
            self.assertEqual(result['summary']['network_bytes'],4000)
            self.assertEqual(result['summary']['measured_bytes'],2000)
            self.assertGreater(result['summary']['wall_seconds']-result['summary']['active_seconds'],.1)
            self.assertGreater(len(result['history']),3)
            self.assertTrue((folder/'download.metrics.json').exists())


if __name__=='__main__':unittest.main(verbosity=2)
