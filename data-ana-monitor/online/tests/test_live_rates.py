import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.request import build_opener, ProxyHandler, Request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from acquisition import DatParser, SourceChanged, discover
from analysis import Analyzer,Config
from engine import Engine
from fleet import FleetMonitor
from live_rates import CounterStore, query, status
from rates import sqlite_rates
from storage import Store
from server import ThreadingHTTPServer,make_handler
from test_online import trigger, waveform, gaussian


class LiveRateTest(unittest.TestCase):
    def request(self, store, **kwargs):
        q=dict(run_id='a'*32,max_seq=0,segment=0,min_s=0,max_s=1000,time_bin_s=100,population='all',live=True)
        q.update(kwargs)
        return query(store.directory.parent.parent,store.source_key,q,dict(run_id=q['run_id'],seq=0,segment=0))

    def test_incremental_partial_volume_restart_clock_reset_and_exact_bins(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);data=root/'data';data.mkdir();name='data_fee_3_run000001_idx001_20261110000000.dat'
            p=data/name;values=gaussian();origin=10**18
            # Empty-trigger time is unrelated and must not create a clock segment.
            payload=trigger(0,origin)+waveform(values,origin+250)+waveform(values,origin+300,channel=1)
            payload+=trigger(1,1)+trigger(2,origin+100*10**9)+waveform(values,origin+100*10**9+250)
            payload+=trigger(3,origin+200*10**9)+waveform(values,origin+200*10**9+250)
            payload+=trigger(4,origin+300*10**9)+waveform(values,origin+300*10**9+250)
            p.write_bytes(payload[:-20]);source=discover(data)[0];key=source['key'];s=CounterStore(data,root/'state',key)
            while s.step(source):pass
            s.publish(source);a=self.request(s)
            self.assertEqual(a['counter_waves'],4);self.assertEqual(a['counter_events'],3)
            self.assertEqual(a['relative_s'],200);self.assertEqual(a['segment'],0)
            self.assertGreater(a['pending_bytes'],0)
            s.close()
            s=CounterStore(data,root/'state',key)
            # Finish a record across two volume files, then omit volume 3.
            second=data/name.replace('idx001','idx002');second.write_bytes(payload[-20:])
            fourth=data/name.replace('idx001','idx004');fourth.write_bytes(trigger(5,origin+400*10**9)+waveform(values,origin+400*10**9))
            source=discover(data)[0]
            while s.step(source):pass
            s.publish(source);a=self.request(s)
            self.assertEqual(a['counter_waves'],5);self.assertEqual(a['counter_events'],4)
            self.assertEqual(a['waiting_for_volume'],3)
            ch=a['series'][0]['data'];self.assertEqual([v[2] for v in ch],[1,1,2])
            self.assertEqual([v[2] for v in a['series'][-1]['data']],[1,1,2])
            # Arbitrary widths and partial edges agree with exact trigger timestamps.
            custom=self.request(s,min_s=50,max_s=250,time_bin_s=75)
            self.assertEqual(sum(v[2] for v in custom['series'][0]['data']),2)
            self.assertEqual(sum(v[2] for v in custom['series'][-1]['data']),2)
            for lo,hi in ((0,100),(100,200),(50,250),(100,300),(0,300)):
                value=self.request(s,min_s=lo,max_s=hi)
                expected=sum(lo<=v and (v<hi or hi==300 and v==hi) for v in (0,100,200,300))
                self.assertEqual(sum(v[2] for v in value['series'][0]['data']),expected,(lo,hi))
            (data/name.replace('idx001','idx003')).write_bytes(trigger(6,origin+10*10**9)+waveform(values,origin+10*10**9))
            source=discover(data)[0]
            while s.step(source):pass
            s.publish(source);a=self.request(s,segment=1)
            self.assertEqual(a['counter_waves'],7);self.assertEqual(a['segment'],1)
            self.assertEqual(a['counter_events'],6)
            s.close()
            s=CounterStore(data,root/'state',key)
            self.assertFalse(s.step(source));s.publish(source)
            self.assertEqual(self.request(s)['counter_waves'],7)
            # Source replacement must stop, rather than silently duplicate counts.
            second.write_bytes(b'x'*20)
            with self.assertRaises(SourceChanged):s.step(source)
            s.close()
            full=list(DatParser().feed(payload));headers=list(DatParser().feed(payload,headers_only=True))
            self.assertEqual([{k:v for k,v in r.items() if k!='samples'} for r in full],headers)

    def test_three_string_counting_continues_when_full_analysis_paused(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);data=root/'data';data.mkdir();paths=[]
            for board in (3,4,5):
                path=data/f'data_fee_{board}_run000001_idx001_20261110000000.dat'
                path.write_bytes(trigger(0,10**18)+waveform(gaussian(),10**18+250,board=board));paths.append(path)
            fleet=FleetMonitor(data,root/'state',poll=.1,follow=True);fleet.start()
            def wait(predicate):
                deadline=time.monotonic()+30
                while time.monotonic()<deadline:
                    if predicate():return
                    time.sleep(.05)
                self.fail('live counter did not update')
            try:
                wait(lambda:len(fleet.entries)==3 and all(e['snapshot'].get('waves')==1 for e in fleet.entries.values()))
                for entry in list(fleet.entries.values()):
                    fleet.active=entry['source']['key'];fleet.put(dict(action='pause'))
                wait(lambda:all(e['snapshot'].get('paused') for e in fleet.entries.values()))
                for path,board in zip(paths,(3,4,5)):
                    with path.open('ab') as stream:stream.write(trigger(1,10**18+10**9)+waveform(gaussian(),10**18+10**9+250,board=board))
                wait(lambda:all((status(e['directory'],e['source']['key']) or {}).get('waves')==2 for e in fleet.entries.values()))
                for entry in fleet.entries.values():
                    snap=entry['snapshot'];start=time.perf_counter()
                    result=fleet.rates(dict(run_id=snap['run_id'],max_seq=snap['seq'],min_s=0,max_s=2,time_bin_s=100,population='all',live=True,scope='all'))
                    self.assertLess(time.perf_counter()-start,.5)
                    self.assertEqual(snap['waves'],1);self.assertEqual(result['counter_waves'],2)
                    self.assertEqual(sum(v[2] for v in result['series'][0]['data']),2)
                with ThreadingHTTPServer(('127.0.0.1',0),make_handler(fleet)) as web:
                    thread=threading.Thread(target=web.serve_forever,daemon=True);thread.start()
                    try:
                        opener=build_opener(ProxyHandler({}))
                        for entry in fleet.entries.values():
                            snap=entry['snapshot']
                            payload=dict(run_id=snap['run_id'],max_seq=snap['seq'],min_s=0,max_s=2,time_bin_s=100,population='all',live=True,scope='all')
                            request=Request(f'http://127.0.0.1:{web.server_port}/api/rates',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
                            with opener.open(request,timeout=2) as response:actual=json.load(response)
                            self.assertEqual(actual['counter_waves'],2);self.assertTrue(actual['live'])
                    finally:web.shutdown();thread.join(timeout=2)
                # New Run counting advances even though waveform analysis remains paused in Run 1.
                newer=data/'data_fee_3_run000002_idx001_20261110010000.dat'
                newer.write_bytes(trigger(0,2*10**18)+waveform(gaussian(),2*10**18+250)+
                                  trigger(1,2*10**18+10**9)+waveform(gaussian(),2*10**18+10**9+250))
                entry=next(e for e in fleet.entries.values() if e['source']['board_id']==3)
                request=dict(run_id=entry['snapshot']['run_id'],max_seq=0,min_s=0,max_s=2,time_bin_s=100,population='all',live=True,scope='all')
                def followed():
                    try:return fleet.rates(request).get('counter_source','').endswith('20261110010000')
                    except ValueError:return False
                wait(followed)
                actual=fleet.rates(request)
                self.assertEqual(actual['counter_waves'],2);self.assertEqual(actual['counter_events'],2)
                self.assertNotEqual(actual['counter_source'],actual['analysis_source'])
                self.assertEqual(entry['snapshot']['waves'],1)
                fleet.active=entry['source']['key'];fleet.put(dict(action='follow',follow=False))
                wait(lambda:not entry['snapshot'].get('follow'))
                self.assertEqual(fleet.rates(request)['counter_source'],actual['counter_source'])
            finally:fleet.close()

    def test_real_three_string_rates_match_full_waveform_analysis(self):
        samples=Path(__file__).resolve().parents[3]/'runtime/debug-20261101_1102/samples'
        if not samples.is_dir():self.skipTest('optional real DAT fixtures absent')
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);config=Config();analyzer=Analyzer(config,root/'build')
            for board in (3,4,5):
                data=samples/f'G{board}';source=discover(data)[0]
                records=list(DatParser().feed((data/source['files'][0]['name']).read_bytes()))
                engine=Engine(config,analyzer);pulses=iter(analyzer.batch([r['samples'] for r in records if r['type']=='wave']));metrics=[]
                for record in records:
                    value=engine.consume(record,next(pulses) if record['type']=='wave' else None)
                    if value is not None:metrics.append(value)
                db=Store(root/f'full-{board}.db');run=db.create(source['key'],config,'test');db.save(run,{},metrics)
                snapshot=engine.snapshot();snapshot['run_id']=run
                counter=CounterStore(data,root/f'counter-{board}',source['key'])
                try:
                    while counter.step(source):pass
                    counter.publish(source)
                    for width in (100,.1):
                        request=dict(run_id=run,max_seq=engine.seq,min_s=0,max_s=snapshot['relative_s']+.001,time_bin_s=width,population='all')
                        expected=sqlite_rates(db.path,request,snapshot)
                        actual=query(root/f'counter-{board}',source['key'],request,snapshot)
                        self.assertEqual(actual['counter_waves'],engine.waves)
                        self.assertEqual(actual['counter_events'],engine.wave_events)
                        self.assertEqual(actual['series'],expected['series'])
                finally:counter.close();db.close()


if __name__=='__main__':unittest.main()
