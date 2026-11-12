import json
import math
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from analysis import Config
from rates import finish,root_rates,sqlite_rates
from stat_errors import poisson_interval,wilson_interval
from storage import Store,FIELDS


class RateTest(unittest.TestCase):
    def test_counts_exposure_zero_bins_and_partial_endpoint(self):
        r=finish({(768,0):100,(768,2):2,(-1,0):80},[768],0,250,100,'a'*32,999,'test')
        points=r['series'][0]['data']
        self.assertEqual([p[1] for p in points],[1,0,.04])
        self.assertEqual([p[3] for p in points],[100,100,50])
        self.assertEqual(sum(p[1]*p[3] for p in points),102)
        self.assertGreater(points[1][5],0) # Empty count bins still have a Poisson upper limit.
        self.assertEqual(finish({},[768],0,0,100,'a'*32,0,'test')['series'],[])

    def test_statistical_intervals_match_root(self):
        self.assertEqual(poisson_interval(0),(0.,1.8410216450092634))
        self.assertEqual(wilson_interval(0,0),(None,None))
        import ROOT
        alpha=(1-.6826894921370859)/2
        for n in (0,1,10,24,25,100,1610,100000):
            low,high=poisson_interval(n)
            expected_low=ROOT.Math.gamma_quantile(alpha,n,1.) if n else 0
            expected_high=ROOT.Math.gamma_quantile_c(alpha,n+1,1.)
            self.assertAlmostEqual(low,expected_low,delta=max(.003,expected_low*.0005))
            self.assertAlmostEqual(high,expected_high,delta=max(.003,expected_high*.0005))
        for p,n in ((0,1),(1,1),(.5,100),(.8,500)):
            low,high=wilson_interval(p,n)
            self.assertAlmostEqual(low,ROOT.TEfficiency.Wilson(n,p*n,.6826894921370859,False))
            self.assertAlmostEqual(high,ROOT.TEfficiency.Wilson(n,p*n,.6826894921370859,True))

    def test_sqlite_counts_are_not_inverse_intervals_and_respect_frozen_view(self):
        with tempfile.TemporaryDirectory() as t:
            store=Store(Path(t)/'m.db');run=store.create('test',Config(),'test');rows=[]
            # Event 0 has two channels, event 1 is empty in the record stream;
            # event 2 has one rejected and one accepted waveform in the same channel.
            for seq,ch,time,number,good in ((2,0,0,0,1),(3,1,0,0,1),(6,0,1,2,0),(7,0,1,2,1),(9,0,2.5,3,1)):
                row={k:0 for k in FIELDS};row.update(seq=seq,board=3,channel=ch,t=time,raw_ns=10**15+round(time*1e9)+250,wave_dt_ns=250,trigger_number=number,good=good);rows.append(row)
            store.save(run,{},rows);s=dict(run_id=run,seq=9,segment=0,relative_s=2.5,channels=[{'key':'3:0'},{'key':'3:1'}])
            q=dict(run_id=run,min_s=0,max_s=3,time_bin_s=1,max_seq=9,population='all')
            r=sqlite_rates(store.path,q,s);wave=r['series'][0]['data'];events=r['series'][-1]['data']
            self.assertEqual([p[2] for p in wave],[1,2,1]);self.assertEqual([p[2] for p in events],[1,1,1])
            self.assertEqual(wave[-1][3],.5);self.assertEqual(wave[-1][1],2)
            good=sqlite_rates(store.path,dict(q,population='good'),s)
            self.assertEqual(good['series'][0]['data'][1][2],1)
            frozen=sqlite_rates(store.path,dict(q,max_seq=7),s)
            self.assertEqual(frozen['max_s'],1);self.assertEqual(sum(p[2] for p in frozen['series'][-1]['data']),2)
            store.close()

    def test_real_default_rates_match_saved_root_counts(self):
        project=Path(__file__).resolve().parents[3]
        directory=project/'runtime/replay-20261101_1102_/strings/G5/closed'
        candidates=[p for p in directory.glob('*/snapshot.json') if (Path(json.loads(p.read_text()).get('analysis_path',''))/'analysis.json').is_file()]
        if not candidates:self.skipTest('optional completed real-data cache missing')
        s=json.loads(candidates[0].read_text());path=Path(s['analysis_path'])/'root/analysis.root'
        r=root_rates(path,dict(run_id=s['run_id'],time_bin_s=100,max_seq=s['seq'],population='all'),s)
        self.assertEqual(r['unit'],'Hz');self.assertEqual(r['series'][0]['data'][0][2:4],[1610,100.])
        self.assertEqual(r['series'][0]['data'][0][1],16.1)
        self.assertEqual(sum(p[2] for c in r['series'] if c['code']>=0 for p in c['data']),s['waves'])
        self.assertEqual(sum(p[2] for p in r['series'][-1]['data']),s['wave_events'])
        frozen=root_rates(path,dict(run_id=s['run_id'],time_bin_s=100,max_seq=10,population='all'),s)
        self.assertLess(frozen['max_s'],s['relative_s'])
        self.assertLessEqual(sum(p[2] for v in frozen['series'] if v['code']>=0 for p in v['data']),9)


if __name__=='__main__':unittest.main()
