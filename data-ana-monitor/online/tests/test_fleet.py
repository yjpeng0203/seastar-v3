import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from acquisition import discover
from analysis import Analyzer,Config
from fleet import FleetMonitor,canonical_sources
from histograms import query_histogram
from replay_cache import ReplayCache,matching_analysis
from time_reference import reference
from test_online import trigger,waveform,gaussian


class FleetTest(unittest.TestCase):
    def wait(self,predicate,timeout=30):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if predicate():return
            time.sleep(.05)
        self.fail('background task timed out')

    def test_batch_matches_scalar_kernel(self):
        with tempfile.TemporaryDirectory() as t:
            a=Analyzer(Config(),Path(t));arrays=[gaussian(),gaussian(height=40)]
            self.assertEqual(a.batch(arrays),[a(v) for v in arrays])

    def test_live_sources_precede_old_boards_and_ignore_trigger_sidecars(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            names=['data_fee_7_run000001_idx001_20261103000000.dat',
                   'data_fee_8_run000001_idx001_20261103000000.dat',
                   'data_fee7_202611031101.root']
            names += [f'{prefix}_fee_{board}_run010189_idx001_20261106011339.dat'
                      for board in (3,4,5) for prefix in ('data','trigger_signal')]
            for name in names:(root/name).write_bytes(b'')
            sources=canonical_sources(discover(root),False)
            self.assertEqual({s['board_id'] for s in sources[:3]},{3,4,5})
            self.assertTrue(all(not f['name'].startswith('trigger_signal_') for s in sources for f in s['files']))

    def test_paused_workers_release_slots_for_live_strings(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);data=root/'data';data.mkdir();paths={}
            for board in (7,8):
                path=data/f'data_fee_{board}_run000001_idx001_20261103000000.dat'
                path.write_bytes(trigger(0,10**9)+waveform(gaussian(),10**9+250,board=board));paths[board]=path
            fleet=FleetMonitor(data,root/'state',follow=True,poll=.1,workers=3);fleet.start()
            try:
                self.wait(lambda:len(fleet.entries)==2 and all(e['snapshot'].get('waves')==1 for e in fleet.entries.values()))
                old=list(fleet.entries.values())
                for e in old:fleet.active=e['source']['key'];fleet.put(dict(action='pause'))
                self.wait(lambda:all(e['snapshot'].get('status')=='paused' for e in old))
                for board in (3,4,5):
                    path=data/f'data_fee_{board}_run010189_idx001_20261106011339.dat'
                    path.write_bytes(trigger(0,2*10**9)+waveform(gaussian(),2*10**9+250,board=board));paths[board]=path
                    (data/path.name.replace('data_fee','trigger_signal_fee')).write_bytes(trigger(0,2*10**9))
                self.wait(lambda:len(fleet.entries)==5 and all(e['snapshot'].get('waves')==1 for e in fleet.entries.values()))
                live=[e for e in fleet.entries.values() if e not in old]
                self.assertEqual(sum(fleet.busy(e) for e in fleet.entries.values()),3)
                fleet.active=old[0]['source']['key']
                with self.assertRaisesRegex(ValueError,'名额已满'):fleet.put(dict(action='resume'))
                fleet.active=live[0]['source']['key'];fleet.put(dict(action='pause'))
                self.wait(lambda:live[0]['snapshot'].get('status')=='paused')
                fleet.active=old[0]['source']['key'];fleet.put(dict(action='resume'))
                self.wait(lambda:old[0]['snapshot'].get('status')!='paused')
                self.assertLessEqual(sum(fleet.busy(e) for e in fleet.entries.values()),3)
                fleet.active=old[1]['source']['key']
                with self.assertRaisesRegex(ValueError,'名额已满'):fleet.put(dict(action='resume'))
                # New Run with a partial first waveform: all live workers keep following.
                fleet.active=old[0]['source']['key'];fleet.put(dict(action='pause'))
                self.wait(lambda:old[0]['snapshot'].get('status')=='paused')
                fleet.active=live[0]['source']['key'];fleet.put(dict(action='resume'))
                for e in live:
                    board=e['source']['board_id'];path=paths[board];new=data/path.name.replace('run010189','run010190').replace('20261106011339','20261106012500')
                    raw=trigger(1,3*10**9)+waveform(gaussian(),3*10**9+250,board=board)
                    new.write_bytes(raw[:100]);paths[board]=(new,raw)
                self.wait(lambda:all('run010190' in e['snapshot'].get('source',{}).get('key','') for e in live))
                for e in live:
                    path,raw=paths[e['source']['board_id']]
                    with path.open('ab') as f:f.write(raw[100:])
                self.wait(lambda:all(e['snapshot'].get('waves')==1 for e in live))
            finally:fleet.close()

    def test_queued_view_reports_selected_source_without_stale_channels(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);data=root/'data';data.mkdir()
            for board in (3,4):
                (data/f'data_fee_{board}_run010189_idx001_20261106011339.dat').write_bytes(
                    trigger(0,10**9)+waveform(gaussian(),10**9+250,board=board))
            fleet=FleetMonitor(data,root/'state',poll=.1,workers=1);fleet.start()
            try:
                self.wait(lambda:len(fleet.entries)==1 and next(iter(fleet.entries.values()))['snapshot'].get('waves')==1)
                queued=next(s for s in fleet.sources if s['key'] not in fleet.entries)
                fleet.put(dict(action='select',source=queued['key']))
                s=fleet.snapshot();self.assertEqual(s['status'],'queued');self.assertEqual(s['source']['key'],queued['key']);self.assertEqual(s['channels'],[])
                with self.assertRaisesRegex(ValueError,'等待计算资源'):fleet.put(dict(action='pause'))
            finally:fleet.close()

    def test_three_strings_keep_running_after_view_switch_and_resume_exactly_once(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);data=root/'data';state=root/'state';paths=[]
            for board in (3,4,5):
                p=data/f'G{board}/raw_data'/f'data_fee_{board}_run000001_idx001_20261101205133.dat';p.parent.mkdir(parents=True)
                p.write_bytes(trigger(0,10**9)+waveform(gaussian(),10**9+250,board=board));paths.append(p)
            fleet=FleetMonitor(data,state,follow=False,poll=.1);fleet.start()
            try:
                self.wait(lambda:len(fleet.snapshot().get('tasks',[]))==3 and all(v['waves']==1 for v in fleet.snapshot()['tasks']))
                entries={key:e['process'].pid for key,e in fleet.entries.items()}
                runs={key:e['snapshot']['run_id'] for key,e in fleet.entries.items()}
                for source in fleet.sources:fleet.put(dict(action='select',source=source['key']))
                for board,p in zip((3,4,5),paths):
                    with p.open('ab') as f:f.write(trigger(1,2*10**9)+waveform(gaussian(),2*10**9+250,board=board))
                self.wait(lambda:all(v['waves']==2 for v in fleet.snapshot()['tasks']))
                self.assertEqual(entries,{key:e['process'].pid for key,e in fleet.entries.items()})
                for e in fleet.entries.values():
                    s=e['snapshot'];q=dict(run_id=s['run_id'],metric='area',population='all',channels=[s['channels'][0]['key']],binning='native',max_seq=s['seq'])
                    result=fleet.histogram(q);self.assertEqual(result['index_source'],'persistent');self.assertEqual(sum(n for _,n in result['series'][0]['hist']),2)
            finally:fleet.close()
            # A discovery-only upgrade must restore existing waveform checkpoints.
            for entry in fleet.entries.values():
                with sqlite3.connect(entry['directory']/'analysis.sqlite3') as db:
                    db.execute('UPDATE runs SET kernel=?',('579510d638bc6a46b2cc68e6b3a1721aea8c726c521950d58fa1e7a634df0821',))
                db.close()
            again=FleetMonitor(data,state,follow=False,poll=.1);again.start()
            try:
                self.wait(lambda:len(again.entries)==3 and all(e['snapshot'].get('waves')==2 for e in again.entries.values()))
                self.assertEqual(runs,{key:e['snapshot']['run_id'] for key,e in again.entries.items()})
            finally:again.close()

    def test_clock_hint_rejects_bad_anchor_without_altering_relative_time(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);p=root/'G3/csv/readback_fee_3_test.csv';p.parent.mkdir(parents=True)
            p.write_text('timestamp_ms,fpga_timestamp_seconds,fpga_timestamp_nanoseconds,valid\n'+
              '\n'.join(f'{1700000000000+i*1000},{100+i if i<3 else 0},0,1' for i in range(4)))
            source=dict(board_id=3,files=[dict(name='G3/raw_data/sample.dat')])
            r=reference(root,source,100*10**9)
            self.assertEqual(r['anchors'],3);self.assertEqual(r['rejected'],1);self.assertEqual(r['origin_unix_ms'],1700000000000)

    def test_raw_sqlite_points_preserve_spike_time_flags_and_pages(self):
        from raw_trends import sqlite_page
        from storage import Store,FIELDS
        with tempfile.TemporaryDirectory() as t:
            store=Store(Path(t)/'raw.db');run=store.create('test',Config(),'test')
            rows=[];origin=10**18
            for i,value in enumerate((1.,10000.,3.)):
                row={k:0 for k in FIELDS};row.update(seq=2+i*2,board=3,channel=0,raw_ns=origin+i*100000000+250,
                    wave_dt_ns=250,t=i*.1,trigger_number=i,area=value,amplitude=value,baseline=197.+i,noise=1.,good=0 if i==1 else 1)
                rows.append(row)
            store.save(run,{},rows)
            snap=dict(run_id=run,seq=6,segment=0,relative_s=1)
            q=dict(run_id=run,max_seq=6,min_s=0,max_s=1,limit=1)
            actual=[]
            while True:
                page=sqlite_page(store.path,q,snap);actual.extend(page['rows'])
                if page['done']:break
                q['cursor']=page['next_cursor']
            self.assertEqual([r[0] for r in actual],[2,4,6])
            self.assertEqual([r[7] for r in actual],[1.,10000.,3.])
            self.assertEqual([r[8] for r in actual],[1.,0.,1.])
            self.assertEqual([r[9] for r in actual],[None,100000000.,100000000.])
            self.assertEqual([r[10] for r in actual],[None,100000000.,100000000.])
            self.assertAlmostEqual(actual[1][2],.10000025,places=12)
            from time_bins import sqlite_time_bins
            binned=sqlite_time_bins(store.path,dict(q,cursor=0,time_bin_s=100),snap)
            channel=[r for r in binned['rows'] if r[1]==768][0]
            self.assertAlmostEqual(channel[7],10004/3)
            point=[v for v in binned['bin_statistics'] if v[1]==768][0]
            self.assertEqual(point[8],3)
            self.assertAlmostEqual(point[9],float(np.std([1.,10000.,3.],ddof=1)/np.sqrt(3)))
            distribution=binned['distributions']['area'][0]
            self.assertEqual(distribution['n'],3);self.assertEqual(sum(n for _,n in distribution['hist']),3)
            fine=sqlite_time_bins(store.path,dict(q,cursor=0,time_bin_s=.1),snap)
            self.assertEqual([r[7] for r in fine['rows'] if r[1]==768],[1.,10000.,3.])
            self.assertEqual(fine['distributions']['area'][0]['hist'],distribution['hist'])
            store.close()

    def test_cold_closed_root_builds_native_cache_and_custom_queries(self):
        project=Path(__file__).resolve().parents[3]
        fixture=project/'runtime/debug-20261101_1102/archives/real-waveform-functional-sample/G3/rec_data'
        if not fixture.is_dir():self.skipTest('optional ROOT fixture missing')
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);data=root/'data';dest=data/'G3/rec_data';dest.mkdir(parents=True)
            src=next(fixture.glob('*.root'));shutil.copy2(src,dest/src.name)
            source=next(s for s in discover(data) if s['kind']=='root')
            m=ReplayCache(data,root/'state',Config(),source);m.start()
            try:
                self.wait(lambda:m.snapshot()['status'] in ('complete','error'),timeout=120)
                s=m.snapshot();self.assertEqual(s['status'],'complete',s.get('error'));self.assertGreater(s['waves'],0)
                q=dict(run_id=s['run_id'],metric='area',population='all',channels=[c['key'] for c in s['channels']],binning='native',max_seq=s['seq'])
                result=m.histogram(q)
                self.assertEqual(sum(c['n'] for c in result['series']),s['waves'])
                self.assertTrue(all(sum(n for _,n in c['hist'])==c['n'] for c in result['series']))
                frozen=m.histogram(dict(q,max_seq=5))
                self.assertLess(sum(c['n'] for c in frozen['series']),s['waves'])
                self.assertEqual(frozen['max_seq'],5)
                q.update(binning='custom',min=0,max=4000,bins=4000)
                custom=m.histogram(q)
                self.assertEqual(sum(c['total']+c['missing'] for c in custom['series']),s['waves'])
                self.assertTrue(all(sum(c['counts'])==c['n'] for c in custom['series']))
                q.update(metric='interval_log10_s',min=-9,max=3,bins=240)
                self.assertTrue(m.histogram(q)['series'])
                metadata=json.loads((m.analysis/'analysis.json').read_text())
                self.assertEqual(s['analysis_sha256'],metadata['backend_sha256'])
                first=list(m.export_csv(s['run_id'],max_seq=5));self.assertIn(b'run_id',first[0])
                q=dict(run_id=s['run_id'],max_seq=s['seq'],min_s=-1,max_s=s['relative_s']+1,limit=100)
                raw=[]
                while True:
                    part=m.raw_trend(q);raw.extend(part['rows'])
                    if part['done']:break
                    q['cursor']=part['next_cursor']
                self.assertEqual(len(raw),s['waves']);self.assertEqual([int(r[0]) for r in raw],list(range(1,s['waves']+1)))
                import ROOT
                f=ROOT.TFile.Open(str(m.analysis/'root/analysis.root'));tree=f.Get('metrics')
                for entry in (0,len(raw)//2,len(raw)-1):
                    tree.GetEntry(entry)
                    self.assertEqual(raw[entry][4],float(tree.baseline));self.assertEqual(raw[entry][7],float(tree.area));self.assertEqual(raw[entry][8],int(tree.good))
                f.Close()
                from time_bins import root_time_bins
                grouped=root_time_bins(m.analysis/'root/analysis.root',m.state_dir,dict(q,time_bin_s=100,population='all'),s)
                self.assertEqual(grouped['observations'],s['waves'])
                self.assertTrue(grouped['bin_statistics'])
                self.assertTrue(any(v[3] is not None and v[3]>=0 for v in grouped['bin_statistics']))
                self.assertEqual(sum(v['n'] for v in grouped['distributions']['area']),s['waves'])
                self.assertTrue(all(sum(n for _,n in v['hist'])==v['n'] for v in grouped['distributions']['area']))

            finally:m.close()


if __name__=='__main__':unittest.main()
