"""Post-test page archive: confirmation, completeness, raw-file safety and reuse."""
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import shutil
import stat
import struct
import sys
import tempfile
import time
import unittest

ONLINE=Path(__file__).resolve().parents[1]
PROJECT=ONLINE.parents[1]
sys.path.insert(0,str(ONLINE));sys.path.insert(0,str(PROJECT))
from archive_service import ArchiveService
from organizer.discovery import scan_dat_volume
from organizer.state import decode_lock


class ArchiveServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();debug=self.root/'disk/debug'
        self.raw=debug/'data/both';self.raw.mkdir(parents=True)
        binary=debug/'build/decoder';binary.parent.mkdir()
        self.calls=self.root/'decoder-calls.txt'
        binary.write_text(f'''#!{sys.executable}
import json,sys
from pathlib import Path
sys.path.insert(0,{str(PROJECT)!r})
from organizer.decoder_runner import _ensure_pyroot_decoder
assert len(sys.argv)==5 and sys.argv[3]=='--config'
assert Path(sys.argv[4]).is_file()
with open({str(self.calls)!r},'a') as f:f.write(sys.argv[1]+'\\n')
R=_ensure_pyroot_decoder()
n=R.seastar_v2_jit.decode_dat_file(sys.argv[1],sys.argv[2])
raise SystemExit(0 if n>=0 else 1)
''');binary.chmod(0o755)
        cfg=debug/'cfg/config.json';cfg.parent.mkdir();cfg.write_text('{}')
        self.csv=self.root/'csv';self.csv.mkdir()
        self.work=self.root/'work';self.output=self.root/'archive';self.config=self.root/'ipc.json'
        self.config.write_text(json.dumps({'organizer':dict(raw_data_dir=str(self.raw),backend='ipc',
            work_dir=str(self.work),output_root=str(self.output),csv_mirror_dir=str(self.csv),
            settle_seconds=0,csv_sync_wait_seconds=0,nice_increment=0)}))
        self.service=ArchiveService(self.raw,self.root/'state',self.config);self.addCleanup(self.service.close)

    def add_sample(self):
        source=next((PROJECT/'runtime/debug-20261101_1102/samples/G3/raw_data').glob('*.dat'))
        target=self.raw/'data_fee_3_run010189_idx001_20261106011338.dat';shutil.copy2(source,target)
        self.scan=scan_dat_volume(target);first=self.scan.first_wave_trigger
        stamp=int(datetime(2026,11,6,1,13,38,tzinfo=timezone(timedelta(hours=8))).timestamp()*1000)
        (self.csv/'readback_fee_3.csv').write_text('timestamp_ms,fpga_timestamp_seconds,fpga_timestamp_nanoseconds,valid,baseline_ch0\n'+
            '\n'.join(f'{stamp+i*1000},{first[1]+i},0,1,197' for i in range(20))+'\n')
        return target

    def wait(self,predicate,timeout=60):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            value=self.service.snapshot()
            if predicate(value):return value
            time.sleep(.05)
        self.fail('archive job timed out: '+str(self.service.snapshot()))

    def command(self):
        runs=self.service.snapshot()['runs']
        return dict(action='start',confirmed_stopped=True,runs=[r['key'] for r in runs],boards=[3],settings={'session_name':'20261106_test'})

    def add_tiny_run(self,number):
        payload=b'\xff\x12'+struct.pack('>HIII',number,0,100,0)
        payload+=bytes([254,48,1,0])+struct.pack('>III',100,250,64)+struct.pack('>64H',*([197]*64))
        target=self.raw/f'data_fee_3_run{number:06d}_idx001_20261106011338.dat';target.write_bytes(payload)
        stamp=int(datetime(2026,11,6,1,13,38,tzinfo=timezone(timedelta(hours=8))).timestamp()*1000)
        (self.csv/'readback_fee_3.csv').write_text('timestamp_ms,fpga_timestamp_seconds,fpga_timestamp_nanoseconds,valid,baseline_ch0\n'+f'{stamp},100,0,1,197\n')
        return target

    def test_requires_stop_confirmation_and_rejects_missing_indices(self):
        target=self.add_sample();command=self.command();command.pop('confirmed_stopped')
        with self.assertRaisesRegex(ValueError,'确认'):self.service.control(command)
        self.assertIsNone(self.service.process)
        shutil.copy2(target,target.with_name(target.name.replace('idx001','idx003')))
        with self.assertRaisesRegex(ValueError,'缺少中间分卷'):self.service.control(self.command())
        self.assertFalse(self.calls.exists());self.assertIsNone(self.service.process)

    def test_rejects_output_inside_raw_and_malformed_selection(self):
        self.add_sample()
        result=self.service.control(dict(action='check',settings={'output_dir':str(self.raw/'archive')}))
        self.assertEqual(result['status'],'error');self.assertIn('之外',result['error'])
        self.service.control(dict(action='check',settings={'output_dir':str(self.output)}))
        command=self.command();command['boards']=[True]
        with self.assertRaisesRegex(ValueError,'筛选'):self.service.control(command)
        with self.assertRaises(ValueError):self.service.control(dict(action='check',settings={'session_name':'../escape'}))

    @unittest.skipUnless(shutil.which('hadd'),'ROOT/hadd required for real archive fixture')
    def test_real_archive_copies_without_chmod_source_and_reuses_decoded_parts(self):
        source=self.add_sample();before=(source.stat().st_size,source.stat().st_mtime_ns,source.stat().st_mode)
        # Include the stopped tail volume so the page job exercises hadd, not just a single-file copy.
        second=self.scan.last_wave_trigger[1]+2
        payload=b'\xff\x12'+struct.pack('>HIII',1234,0,second,0)
        payload+=bytes([254,48,1,0])+struct.pack('>III',second,250,64)+struct.pack('>64H',*([197]*64))
        tail=source.with_name(source.name.replace('idx001','idx002'));tail.write_bytes(payload)
        tail_before=(tail.stat().st_size,tail.stat().st_mtime_ns,tail.stat().st_mode)
        sidecar=self.raw/'trigger_signal_fee_3_run010189_idx001_20261106011337.dat'
        opaque=b'opaque trigger sidecar\x00\xff\x12\xfe\x03\x00not a waveform stream'
        sidecar.write_bytes(opaque)
        sidecar_before=(sidecar.stat().st_size,sidecar.stat().st_mtime_ns,sidecar.stat().st_mode)
        command=self.command();self.assertEqual(self.service.control(command)['status'],'running')
        result=self.wait(lambda s:s['status'] in ('complete','error'))
        self.assertEqual(result['status'],'complete',result.get('error'));job=result['result'];self.assertEqual(job['status'],'ok')
        folder=Path(job['session_dir']);self.assertTrue((folder/'manifest.json').is_file());self.assertTrue((folder/'SHA256SUMS.txt').is_file())
        self.assertTrue(job['verification']['ok']);self.assertEqual(len(job['runs']),1);run=job['runs'][0]
        self.assertEqual(run['waves'],4037);self.assertEqual(run['rec_root']['entries'],self.scan.wave_events+1)
        self.assertEqual(len(run['trigger_signal_files']),1)
        archived_sidecar=folder/run['trigger_signal_files'][0]['relative_path']
        self.assertEqual(archived_sidecar.read_bytes(),opaque)
        self.assertEqual(sidecar_before,(sidecar.stat().st_size,sidecar.stat().st_mtime_ns,sidecar.stat().st_mode))
        archived=folder/run['raw_volumes'][0]['relative_path']
        self.assertNotEqual(source.stat().st_ino,archived.stat().st_ino)
        self.assertEqual(stat.S_IMODE(archived.stat().st_mode),0o444)
        self.assertEqual(before,(source.stat().st_size,source.stat().st_mtime_ns,source.stat().st_mode))
        self.assertEqual(len(self.calls.read_text().splitlines()),2)
        self.assertEqual(self.service.control(command)['status'],'running')
        again=self.wait(lambda s:s['status'] in ('complete','error'))
        self.assertEqual(again['status'],'complete',again.get('error'))
        self.assertEqual(len(self.calls.read_text().splitlines()),2)
        self.assertEqual(again['result']['session_dir'],str(folder))
        self.assertEqual(again['result']['runs'][0]['cached_volumes_reused'],2)
        self.assertEqual(len(again['result']['runs'][0]['trigger_signal_files']),1)
        self.assertEqual(archived_sidecar.read_bytes(),opaque)
        self.assertEqual(sidecar_before,(sidecar.stat().st_size,sidecar.stat().st_mtime_ns,sidecar.stat().st_mode))
        self.assertEqual(tail_before,(tail.stat().st_size,tail.stat().st_mtime_ns,tail.stat().st_mode))
        self.assertEqual(before,(source.stat().st_size,source.stat().st_mtime_ns,source.stat().st_mode))

    def test_existing_decode_task_lock_blocks_worker(self):
        self.add_sample()
        with decode_lock(self.work):
            self.assertEqual(self.service.control(self.command())['status'],'running')
            value=self.wait(lambda s:s['status'] in ('complete','error'))
            self.assertEqual(value['status'],'error');self.assertIn('占用',value['error'])
        self.assertFalse(self.calls.exists());self.assertFalse((self.output/'20261106_test').exists())

    @unittest.skipUnless(shutil.which('hadd'),'ROOT/hadd required for archive fixture')
    def test_same_board_runs_in_same_minute_get_distinct_root_and_csv(self):
        self.add_tiny_run(10189);self.add_tiny_run(10190)
        self.service.control(self.command())
        value=self.wait(lambda s:s['status'] in ('complete','error'))
        self.assertEqual(value['status'],'complete',value.get('error'));result=value['result'];self.assertTrue(result['verification']['ok'])
        self.assertEqual(len(result['runs']),2)
        roots=[r['rec_root']['relative_path'] for r in result['runs']]
        csvs=[r['csv']['relative_path'] for r in result['runs']]
        self.assertEqual(len(set(roots)),2);self.assertEqual(len(set(csvs)),2);self.assertNotIn(None,csvs)
        folder=Path(result['session_dir'])
        self.assertTrue(all((folder/p).is_file() for p in roots+csvs))
        manifest=json.loads((folder/'manifest.json').read_text());self.assertEqual(len(manifest['runs']),2)
        self.assertEqual(len(self.calls.read_text().splitlines()),2)

    @unittest.skipUnless(shutil.which('hadd'),'ROOT/hadd required for archive fixture')
    def test_new_volume_during_decode_prevents_success(self):
        self.add_tiny_run(10189)
        binary=self.raw.parents[1]/'build/decoder'
        text=binary.read_text().replace('raise SystemExit(0 if n>=0 else 2)',
            "Path(sys.argv[1]).with_name(Path(sys.argv[1]).name.replace('idx001','idx002')).write_bytes(Path(sys.argv[1]).read_bytes())\nraise SystemExit(0 if n>=0 else 2)")
        # The fixture wrapper uses exit1 on native failure.
        if text==binary.read_text():
            text=binary.read_text().replace('raise SystemExit(0 if n>=0 else 1)',
                "Path(sys.argv[1]).with_name(Path(sys.argv[1]).name.replace('idx001','idx002')).write_bytes(Path(sys.argv[1]).read_bytes())\nraise SystemExit(0 if n>=0 else 1)")
        binary.write_text(text)
        self.service.control(self.command())
        value=self.wait(lambda s:s['status'] in ('complete','error'))
        self.assertEqual(value['status'],'error');self.assertIn('文件清单发生变化',value['error'])
        self.assertEqual(value['result']['status'],'generated_unverified')
        self.assertFalse(value['result']['verification']['ok']);self.assertFalse(self.service.result_file.exists())
        self.assertEqual(len(self.calls.read_text().splitlines()),1)

if __name__=='__main__':unittest.main()
