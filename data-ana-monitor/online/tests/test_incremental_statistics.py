import json
import math
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from analysis import Config
from histograms import Counts,query_histogram
import histograms
from storage import Store,FIELDS,index_time,ensure_time_index
from time_bins import sqlite_time_bins,_sqlite_time_bins_scan,sqlite_distributions


class IncrementalStatisticsTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'metrics.db')
        self.run=self.store.create('synthetic',Config(),'test')

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def rows(self):
        rows=[];seq=0
        for i in range(1200):
            segment=int(i>=800);t=(i if segment==0 else i-800)*.5
            origin=10**18+segment*10**15
            seq+=1
            for channel in range(2 if i%2==0 else 1):
                seq+=1;row={name:0 for name in FIELDS};dt=-250 if i%100==0 else 250
                row.update(seq=seq,board=3,channel=channel,segment=segment,t=t,
                           raw_ns=origin+round(t*1e9)+dt,wave_dt_ns=dt,trigger_number=i,
                           baseline=197+i%7,noise=1+i%3,amplitude=i%100+.25,
                           area=i*3+.75,good=int((i+channel)%7!=0),start=10,end=14)
                rows.append(row)
        return rows

    def assert_statistics_equal(self,actual,expected):
        self.assertEqual(actual['observations'],expected['observations'])
        self.assertEqual(len(actual['rows']),len(expected['rows']))
        for a,b in zip(actual['rows'],expected['rows']):
            for x,y in zip(a,b):
                if y is None:self.assertIsNone(x)
                else:self.assertAlmostEqual(x,y,delta=max(1e-7,abs(y)*1e-12))
        for a,b in zip(actual['bin_statistics'],expected['bin_statistics']):
            for x,y in zip(a,b):
                if y is None:self.assertIsNone(x)
                else:self.assertAlmostEqual(x,y,delta=max(1e-7,abs(y)*1e-12))

    def test_persisted_moments_match_raw_scan_edges_good_flags_and_segments(self):
        rows=self.rows()
        for i in range(0,len(rows),137):self.store.save(self.run,{},rows[i:i+137])
        snapshot=dict(run_id=self.run,seq=rows[-1]['seq'],segment=1)
        for segment,lo,hi in ((0,0,399.5),(0,0,400),(0,100,300),(0,99.9999999,300.0000001),(0,37,361),(1,0,199.5)):
            for population in ('all','good'):
                request=dict(run_id=self.run,max_seq=snapshot['seq'],segment=segment,min_s=lo,max_s=hi,time_bin_s=100,population=population,include_distributions=False)
                with self.subTest(segment=segment,lo=lo,hi=hi,population=population):
                    actual=sqlite_time_bins(self.store.path,request,snapshot)
                    expected=_sqlite_time_bins_scan(self.store.path,request,snapshot)
                    self.assertEqual(actual['index_source'],'persistent 100 s moments')
                    self.assert_statistics_equal(actual,expected)

    def test_backfill_is_restartable_atomic_and_explicit_frozen_watermark(self):
        rows=self.rows();self.store.save(self.run,{},rows)
        with self.store.db:
            self.store.db.execute('DELETE FROM time_bins100');self.store.db.execute('DELETE FROM time_index')
            index_time(self.store.db,self.run,rows[:319])
        ensure_time_index(self.store.path,self.run,rows[-1]['seq'])
        before=self.store.db.execute('SELECT SUM(n) FROM time_bins100').fetchone()[0]
        ensure_time_index(self.store.path,self.run,rows[-1]['seq'])
        self.assertEqual(before,self.store.db.execute('SELECT SUM(n) FROM time_bins100').fetchone()[0])
        snapshot=dict(run_id=self.run,seq=rows[-1]['seq'],segment=0)
        request=dict(run_id=self.run,max_seq=rows[400]['seq'],segment=0,min_s=0,max_s=400,time_bin_s=100,population='all',include_distributions=False)
        frozen=sqlite_time_bins(self.store.path,request,snapshot)
        self.assert_statistics_equal(frozen,_sqlite_time_bins_scan(self.store.path,request,snapshot))
        live=sqlite_time_bins(self.store.path,dict(request,allow_latest=True),snapshot)
        self.assertTrue(live['snapshot_advanced']);self.assertEqual(live['requested_max_seq'],request['max_seq'])
        self.assertEqual(live['max_seq'],rows[-1]['seq'])
        watermark=self.store.db.execute('SELECT max_seq FROM time_index').fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):self.store.save(self.run,{},[rows[-1]])
        self.assertEqual(self.store.db.execute('SELECT max_seq FROM time_index').fetchone()[0],watermark)

    def test_native_single_pass_and_preview_watermark_are_consistent(self):
        rows=self.rows()[:512];self.store.save(self.run,{},rows)
        stat=Counts(0,1,None);stat.add([r['area'] for r in rows if r['channel']==0]);n=stat.n
        engine=dict(seq=rows[-1]['seq']+1,channels={'3:0':dict(n=n,good=n,stats={'all':{'area':dict(n=n,mean=stat.mean,m2=stat.m2)}})})
        with self.store.db:
            traced=[];self.store.db.set_trace_callback(traced.append)
            self.store.index_native(self.run,0,rows[-1]['seq'],{'sample_ns':2})
            self.store.db.set_trace_callback(None)
            self.store.db.execute('UPDATE runs SET state=? WHERE id=?',(json.dumps(dict(engine=engine)),self.run))
        scans=[s for s in traced if s.upper().startswith('SELECT') and 'FROM metrics' in s]
        self.assertEqual(len(scans),1)
        request=dict(run_id=self.run,metric='area',channels=['3:0'],population='all',binning='native',max_seq=rows[30]['seq'],preview_points=32,allow_latest=True)
        result=query_histogram(self.store.path,request)
        self.assertEqual(result['index_source'],'persistent');self.assertTrue(result['snapshot_advanced'])
        self.assertEqual(result['max_seq'],engine['seq']);self.assertEqual(result['series'][0]['n'],n)
        self.assertLessEqual(len(result['series'][0]['hist']),32)
        self.assertEqual(result['min'],min(math.floor(r['area']) for r in rows if r['channel']==0))
        self.assertEqual(result['max'],max(math.floor(r['area']) for r in rows if r['channel']==0)+1)

    def test_backfill_can_overlap_new_metric_commits_without_skipping_or_double_counting(self):
        rows=self.rows();self.store.save(self.run,{},rows[:700])
        with self.store.db:
            self.store.db.execute('DELETE FROM time_bins100');self.store.db.execute('DELETE FROM time_index')
        errors=[]
        def backfill():
            try:ensure_time_index(self.store.path,self.run,rows[-1]['seq'])
            except Exception as error:errors.append(error)
        thread=threading.Thread(target=backfill);thread.start()
        for i in range(700,len(rows),31):self.store.save(self.run,{},rows[i:i+31])
        thread.join(timeout=10);self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
        ensure_time_index(self.store.path,self.run,rows[-1]['seq'])
        snapshot=dict(run_id=self.run,seq=rows[-1]['seq'],segment=0)
        request=dict(run_id=self.run,max_seq=snapshot['seq'],segment=0,min_s=0,max_s=400,time_bin_s=100,population='good',include_distributions=False)
        self.assert_statistics_equal(sqlite_time_bins(self.store.path,request,snapshot),_sqlite_time_bins_scan(self.store.path,request,snapshot))

    def test_interval_distribution_two_pass_has_exact_count_and_moments(self):
        rows=self.rows();self.store.save(self.run,{},rows)
        snapshot=dict(run_id=self.run,seq=rows[-1]['seq'],segment=0)
        request=dict(run_id=self.run,max_seq=rows[-1]['seq'],segment=0,min_s=0,max_s=400,population='all')
        distributions=sqlite_distributions(self.store.path,request,snapshot)
        from raw_trends import sqlite_page
        page=sqlite_page(self.store.path,dict(request,limit=100000),snapshot)
        for d in distributions['interval']:
            values=[row[9] for row in page['rows'] if int(row[1])==d['code']]
            reference=Counts(d['min'],d['max'],d['bins']);reference.add(values)
            result=reference.result()
            self.assertEqual(d['n'],result['n']);self.assertEqual(d['missing'],result['missing'])
            self.assertAlmostEqual(d['mean'],result['mean'])
            self.assertEqual(sum(n for _,n in d['hist']),d['n'])


if __name__=='__main__':unittest.main()
