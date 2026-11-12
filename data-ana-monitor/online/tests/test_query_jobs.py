import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from analysis import Config
from query_jobs import QueryRunner
from storage import Store,FIELDS


class QueryJobTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=Store(self.root/'metrics.db');self.run=self.store.create('test',Config(),'test')
        rows=[]
        for i in range(20):
            row=dict.fromkeys(FIELDS,0);row.update(seq=i+1,board=3,channel=0,raw_ns=10**18+i*10**9,trigger_number=i,t=float(i),good=1,area=float(i),baseline=200.,noise=1.)
            rows.append(row)
        self.store.save(self.run,{},rows)
        self.target=dict(kind='sqlite',path=str(self.store.path),state_dir=str(self.root),snapshot=dict(run_id=self.run,seq=20,relative_s=19,segment=0))
        self.request=dict(run_id=self.run,max_seq=20,min_s=0,max_s=20,time_bin_s=100,include_distributions=False)
        self.runner=QueryRunner(self.root,timeout=15)

    def tearDown(self):
        self.runner.close();self.store.close();self.temp.cleanup()

    def wait(self,predicate):
        until=time.monotonic()+10
        while time.monotonic()<until:
            if predicate():return
            time.sleep(.02)
        self.fail('query dispatcher timed out')

    def test_same_query_shares_one_child_and_precise_result(self):
        barrier=threading.Barrier(3);results=[];errors=[]
        def run(token):
            barrier.wait()
            try:results.append(self.runner.run(self.target,'/api/time-trend',dict(self.request,_query_id=token)))
            except Exception as error:errors.append(str(error))
        threads=[threading.Thread(target=run,args=(str(i),)) for i in range(2)]
        for t in threads:t.start()
        barrier.wait()
        for t in threads:t.join(12)
        self.assertFalse(errors);self.assertEqual(len(results),2)
        self.assertEqual(results[0],results[1]);self.assertEqual(results[0]['observations'],20)
        logs=[json.loads(s) for s in (self.root/'queries/tasks.jsonl').read_text().splitlines()]
        self.assertEqual(sum(r['event']=='started' for r in logs),1)

    def test_cancel_terminates_waiting_sqlite_child_and_keeps_metrics(self):
        lock=sqlite3.connect(self.store.path);lock.execute('BEGIN IMMEDIATE')
        errors=[]
        def run():
            try:self.runner.run(self.target,'/api/time-trend',dict(self.request,_query_id='cancel-test'))
            except ValueError as e:errors.append(str(e))
        t=threading.Thread(target=run);t.start()
        try:
            self.wait(lambda:bool((self.runner.snapshot().get('active') or {}).get('pid')))
            self.assertTrue(self.runner.cancel('cancel-test')['cancelled'])
            t.join(3);self.wait(lambda:self.runner.snapshot()['active'] is None)
            self.assertIn('取消',errors[0])
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM metrics').fetchone()[0],20)
        finally:lock.rollback();lock.close();t.join(3)

    def test_memory_budget_stops_only_query(self):
        self.runner.budget_bytes=1
        with self.assertRaisesRegex(ValueError,'内存预算'):
            self.runner.run(self.target,'/api/time-trend',dict(self.request,_query_id='memory-test'))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM metrics').fetchone()[0],20)


if __name__=='__main__':unittest.main()
