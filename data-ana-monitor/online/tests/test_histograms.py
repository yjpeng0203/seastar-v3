import json
import math
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from analysis import Config
from histograms import query_histogram
from server import Monitor,PLOT_COMPATIBLE_BACKENDS
from storage import Store,FIELDS
from test_online import trigger,waveform,gaussian


class HistogramTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=Store(self.root/'metrics.db')
        self.run=self.store.create('test',Config(),'test-backend')

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def row(self,seq,value,**extra):
        row={field:0 for field in FIELDS}
        row.update(seq=seq,board=3,channel=0,amplitude=value,area=value,baseline=197.,noise=2.,
                   good=1,raw_ns=10**18+250+seq*1000,wave_dt_ns=250,trigger_number=seq,
                   start=10,end=14,points=504)
        row.update(extra)
        return row

    def request(self,**kwargs):
        request={'run_id':self.run,'metric':'area','channels':['3:0'],'population':'all',
                 'min':0.,'max':2.,'bins':4,'max_seq':10000}
        request.update(kwargs)
        return query_histogram(self.store.path,request)

    def test_exact_bins_boundaries_range_statistics_and_missing(self):
        values=[-1,0,.49,.5,1,1.5,2,3,None]
        rows=[self.row(i+1,v,good=0 if v==1.5 else 1) for i,v in enumerate(values)]
        rows+=[self.row(20,.2,channel=1),self.row(21,1.9,channel=1)]
        self.store.save(self.run,{},rows)
        result=self.request(channels=['3:0','3:1'])
        a,b=result['series']
        self.assertEqual(a['counts'],[2,1,1,2])
        self.assertEqual((a['total'],a['n'],a['underflow'],a['overflow'],a['missing']),(8,6,1,1,1))
        inside=[0,.49,.5,1,1.5,2]
        self.assertAlmostEqual(a['mean'],float(np.mean(inside)))
        self.assertAlmostEqual(a['sigma'],float(np.std(inside)))
        self.assertEqual(b['counts'],[1,0,0,1])
        good=self.request(population='good')['series'][0]
        self.assertEqual(good['counts'],[2,1,1,1])
        self.assertEqual(self.request(max_seq=4)['series'][0]['counts'],[2,1,0,0])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM metrics').fetchone()[0],11)

    def test_fine_bins_use_raw_values_not_coarse_histogram_centers(self):
        values=[.001,.002,.009,.011,.101,.999]
        self.store.save(self.run,{},[self.row(i+1,v) for i,v in enumerate(values)])
        result=self.request(min=0,max=1,bins=1000)['series'][0]
        expected=np.histogram(values,bins=1000,range=(0,1))[0].tolist()
        self.assertEqual(result['counts'],expected)
        self.assertEqual(sum(result['counts']),len(values))

    def test_native_defaults_keep_raw_bins_full_range_and_incremental_watermark(self):
        values=[-1.1,0,.99,1,4000,1202438.4,None]
        self.store.save(self.run,{},[self.row(i+1,v,good=0 if v==4000 else 1)
                                    for i,v in enumerate(values)])
        result=self.request(binning='native')
        self.assertEqual((result['min'],result['max'],result['bins'],result['bin_width']),
                         (-2,1202439,1202441,1))
        a=result['series'][0]
        self.assertEqual(a['hist'],[[-1.5,1],[.5,2],[1.5,1],[4000.5,1],[1202438.5,1]])
        self.assertEqual((a['n'],a['underflow'],a['overflow'],a['missing']),(6,0,0,1))
        self.assertAlmostEqual(a['mean'],float(np.mean(values[:-1])))
        # A future requested watermark must not hide later real inserts; refresh cannot double count.
        self.store.save(self.run,{},[self.row(8,4000.4)])
        updated=self.request(binning='native')['series'][0]
        self.assertEqual(updated['n'],7);self.assertEqual(updated['hist'][3],[4000.5,2])
        self.assertEqual(updated,self.request(binning='native')['series'][0])
        old=self.request(binning='native',max_seq=3)['series'][0]
        self.assertEqual(old['n'],3);self.assertEqual(old['hist'],[[-1.5,1],[.5,2]])
        self.assertEqual(self.request(binning='native')['series'][0],updated)
        self.assertEqual(self.request(binning='native',population='good')['series'][0]['n'],6)
        self.assertEqual(self.request(binning='native',metric='window_ns')['series'][0]['hist'],[[8.5,8]])

    def test_derived_metrics_and_empty_range(self):
        self.store.save(self.run,{},[self.row(1,10),self.row(2,0,noise=0)])
        self.assertEqual(self.request(metric='window_ns',min=0,max=10,bins=10)['series'][0]['counts'][8],2)
        result=self.request(metric='snr',min=0,max=10,bins=10)['series'][0]
        self.assertEqual((result['n'],result['mean']),(2,2.5))
        empty=self.request(min=100,max=200)['series'][0]
        self.assertEqual(empty['n'],0);self.assertIsNone(empty['mean']);self.assertIsNone(empty['sigma'])
        self.assertEqual(empty['underflow'],2)

    def test_intervals_and_pair_differences_match_event_semantics(self):
        base=10**18
        rows=[self.row(1,1,raw_ns=base+250,trigger_number=0,cfd_ns=10),
              self.row(2,1,raw_ns=base+250,trigger_number=0,cfd_ns=99),
              self.row(3,1,channel=1,raw_ns=base+250,trigger_number=0,cfd_ns=12),
              self.row(4,1,raw_ns=base+1250,trigger_number=1,cfd_ns=10,good=0),
              self.row(5,1,channel=1,raw_ns=base+1250,trigger_number=1,cfd_ns=14),
              self.row(6,1,raw_ns=base+3250,trigger_number=2,cfd_ns=8),
              self.row(7,1,channel=1,raw_ns=base+3250,trigger_number=2,cfd_ns=12),
              self.row(8,1,raw_ns=250,segment=1,trigger_number=3,cfd_ns=10)]
        self.store.save(self.run,{},rows)
        pair=self.request(metric='pair_dt_ns',channels=['3:0','3:1'],min=0,max=4,bins=4)['series'][0]
        self.assertEqual(pair['key'],'3:0/3:1')
        self.assertEqual(pair['counts'],[0,0,1,1]);self.assertEqual(pair['mean'],3)
        for metric in ('interval_log10_s','trigger_interval_log10_s'):
            result=self.request(metric=metric,min=-9,max=0,bins=9)['series'][0]
            self.assertEqual(result['n'],2)
            self.assertAlmostEqual(result['mean'],(-6+math.log10(2e-6))/2)

    def test_invalid_settings_and_sql_column_names_are_rejected(self):
        for change in ({'min':2,'max':1},{'min':float('nan')},{'bins':0},{'bins':4097},
                       {'bins':2.5},{'bins':True},{'metric':'area); DROP TABLE metrics;--'},
                       {'max_seq':-1},{'channels':['../0']},{'run_id':'unknown'}):
            with self.subTest(change=change),self.assertRaises(ValueError):self.request(**change)

    def test_plot_only_upgrade_keeps_existing_metrics_and_cursor(self):
        data=self.root/'data';data.mkdir();state=self.root/'state'
        (data/'sample.dat').write_bytes(trigger(0,10**9)+waveform(gaussian(),10**9+250))
        first=Monitor(data,state,poll=.1);first.start()
        try:
            end=time.monotonic()+10
            while time.monotonic()<end and first.snapshot().get('waves')!=1:time.sleep(.03)
            self.assertEqual(first.snapshot().get('waves'),1)
            run=first.snapshot()['run_id']
        finally:first.close()
        db=sqlite3.connect(state/'analysis.sqlite3')
        db.execute('UPDATE runs SET kernel=? WHERE id=?',(PLOT_COMPATIBLE_BACKENDS[0],run));db.commit();db.close()
        second=Monitor(data,state,poll=.1)
        def must_not_reanalyze(_):raise AssertionError('plot update reprocessed a waveform')
        second.analyzer=must_not_reanalyze;second.start()
        try:
            end=time.monotonic()+10
            while time.monotonic()<end and second.snapshot().get('waves')!=1:time.sleep(.03)
            snap=second.snapshot()
            self.assertFalse(snap.get('error'))
            self.assertEqual(snap['run_id'],run)
            self.assertEqual(snap['analysis_sha256'],PLOT_COMPATIBLE_BACKENDS[0])
            self.assertNotEqual(snap['server_sha256'],snap['analysis_sha256'])
        finally:second.close()


if __name__=='__main__':unittest.main()
