import csv
import json
import math
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import tempfile
import unittest
from array import array

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
sys.path.insert(0,str(PROJECT/'data-ana-monitor/online'))
from organizer.figure_data import FOCUS,export_plot_data,native_rows,read_histogram


def fixture(root):
    import ROOT as R
    root=Path(root);(root/'root').mkdir();(root/'tables').mkdir()
    file=R.TFile.Open(str(root/'root/analysis.root'),'RECREATE');tree=R.TTree('metrics','metrics')
    integers={name:array('i',[0]) for name in ('channel','board','good','pulses')};event=array('q',[0])
    for name,buffer in integers.items():tree.Branch(name,buffer,name+'/I')
    tree.Branch('event',event,'event/L')
    fields={name:array('d',[0.]) for name in FOCUS if name!='pulses'}
    for name,buffer in fields.items():tree.Branch(name,buffer,name+'/D')
    values=[]
    for i,x in enumerate((-50_000_000.,2.,50_000_000.)):
        row={name:float(i+10) for name in fields};row.update(area=x,wave_dt_ns=x,noise=1.,baseline=197.)
        for name,buffer in fields.items():buffer[0]=row[name]
        integers['channel'][0]=0;integers['board'][0]=3;integers['good'][0]=int(i!=1);integers['pulses'][0]=1;event[0]=i
        tree.Fill();values.append(dict(row,pulses=1.,good=int(i!=1)))
    tree.Write()
    for name in ('multiplicity','trigger_interval'):
        h=R.TH1D(name,'',4,0,4);h.Fill(1);h.Write()
    file.Close()
    header=('metric','population','channel','n','missing','mean','sigma','min','max')
    with (root/'tables/channel_statistics.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=header);writer.writeheader()
        for name in FOCUS:
            for pop in ('all','good'):
                v=[r[name] for r in values if pop=='all' or r['good']]
                mean=sum(v)/len(v)
                writer.writerow(dict(metric=name,population=pop,channel=0,n=len(v),missing=0,mean=mean,sigma=math.sqrt(sum((x-mean)**2 for x in v)/len(v)),min=min(v),max=max(v)))
    metadata=dict(waves=3,events=3,good=2,config=dict(adc_mv=.5,sample_ns=2.,resistance=50.),source_csv=[])
    (root/'analysis.json').write_text(json.dumps(metadata));return values


class SparseFigureTest(unittest.TestCase):
    def test_wide_native_bins_sparse_counts_conversion_and_frozen_query(self):
        import ROOT as R
        from root_queries import native_histogram,histogram
        from replay_cache import query_saved_histogram
        import numpy as np
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);analysis=root/'analysis';analysis.mkdir();fixture(analysis)
            target=root/'figures';metadata=export_plot_data(analysis,target,include_readback=False)
            self.addCleanup(shutil.rmtree,PROJECT/'runtime/figure-cache'/metadata['figure_cache_key'],True)
            file=R.TFile.Open(str(target/'root/focused.root'))
            raw=read_histogram(file,'full_all_area_ch0');converted=read_histogram(file,'full_all_charge_pc_ch0')
            self.assertTrue(raw.InheritsFrom('THnSparse'))
            self.assertEqual(raw.GetAxis(0).GetNbins(),100_000_001)
            self.assertEqual(raw.GetAxis(0).GetBinWidth(1),1)
            self.assertEqual(raw.GetEntries(),3)
            rows=native_rows(raw);physical=native_rows(converted)
            self.assertEqual(rows.tolist(),[[-49_999_999.5,1.],[2.5,1.],[50_000_000.5,1.]])
            np.testing.assert_array_equal(rows[:,1],physical[:,1]);np.testing.assert_allclose(rows[:,0]*.02,physical[:,0])
            self.assertEqual(len(native_rows(raw,32)),3)
            small=read_histogram(file,'all_amplitude_ch0');self.assertTrue(small.InheritsFrom('TH1'))
            del small,converted,raw;file.Close()
            snap=dict(run_id='a'*32,seq=6,source=dict(board_id=3),channels=[dict(key='3:0',n=3,good=2,stats={'all':{'area':dict(n=3,mean=2/3,sigma=1.)},'good':{'area':dict(n=2,mean=0.,sigma=1.)}})])
            q=dict(run_id=snap['run_id'],max_seq=4,metric='area',population='all',channels=['3:0'],binning='native')
            actual=native_histogram(analysis/'root/analysis.root',q,snap)
            self.assertEqual(actual['series'][0]['n'],2)
            self.assertEqual(actual['series'][0]['hist'],[[-49_999_999.5,1],[2.5,1]])
            folder=root/'state/native-bin-cache/area';folder.mkdir(parents=True)
            np.save(folder/'all_0.npy',rows);(folder/'bounds.json').write_text(json.dumps([-50_000_000,50_000_001,100_000_001]))
            actual=query_saved_histogram(dict(path=str(analysis/'root/analysis.root'),state_dir=str(root/'state'),snapshot=snap),dict(q,max_seq=6,preview_points=32))
            self.assertEqual(actual['series'][0]['hist'],rows.tolist());self.assertEqual(actual['bins'],100_000_001)
            # An explicit gigantic dense request is rejected before allocation.
            with self.assertRaisesRegex(ValueError,'64 MiB'):
                histogram(analysis/'root/analysis.root',dict(q,binning='custom',min=0,max=100_000_000,bins=50_000_000),snap,max_bins=50_000_000)

    def test_sparse_span_memory_is_not_proportional_to_empty_bins(self):
        script='''
from pathlib import Path
import sys,resource,json,shutil
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_sparse_figure_data import fixture,PROJECT
from organizer.figure_data import export_plot_data
import tempfile
with tempfile.TemporaryDirectory() as t:
 p=Path(t);a=p/'analysis';a.mkdir();fixture(a)
 metadata=export_plot_data(a,p/'figures',include_readback=False)
 shutil.rmtree(PROJECT/'runtime/figure-cache'/metadata['figure_cache_key'])
 peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
 if sys.platform!='darwin':peak*=1024
 print('SPARSE_RSS='+str(peak))
'''
        result=subprocess.run([sys.executable,'-c',script],cwd=PROJECT,text=True,capture_output=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        peak=int(result.stdout.split('SPARSE_RSS=')[-1].strip())
        self.assertLess(peak,600*1024**2)


if __name__=='__main__':unittest.main()
