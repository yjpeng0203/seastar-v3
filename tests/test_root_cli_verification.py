"""Final archive verification must not rely on ROOT sharing Python's /tmp."""
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from organizer.decoder_runner import RootInspection,_inspect_root_with_cli
from organizer.packager import verify_dataset

class RootCliVerificationTest(unittest.TestCase):
    def test_scratch_uses_shared_workdir_or_archive_parent(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d).resolve();batch=base/'batch';batch.mkdir();(batch/'data_fee_3_test.root').write_bytes(b'placeholder')
            for supplied in (None,base/'shared-work'):
                def inspect(path,root_bin,work_dir):
                    self.assertTrue(work_dir.is_dir());self.assertEqual(work_dir.parent,supplied or base)
                    return RootInspection(str(path),True,0,None,None)
                with patch('organizer.packager.inspect_root_file',side_effect=inspect):
                    self.assertTrue(verify_dataset(batch,work_dir=supplied)['ok'])

    def test_unread_root_is_not_reported_as_zero_count_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            batch=Path(d).resolve();dat=batch/'data_fee_3_run010189_idx001_20261106011338.dat'
            dat.write_bytes(b'\xff\x12'+struct.pack('>HIII',1,0,100,0)+bytes([254,48,1,0])+struct.pack('>III',100,250,2)+struct.pack('>2H',197,200))
            root=batch/'data_fee_3_test.root';root.write_bytes(b'placeholder')
            with patch('organizer.packager.inspect_root_file',return_value=RootInspection(str(root),False,0,None,None,'root CLI macro not found')):
                result=verify_dataset(batch)
            self.assertFalse(result['ok']);self.assertTrue(any('ROOT 校验未通过' in e for e in result['errors']))
            self.assertFalse(any('ROOT 总事件数' in e for e in result['errors']))
            self.assertTrue(any('暂不比较' in e for e in result['warnings']))

    @unittest.skipUnless(shutil.which('root'),'ROOT command required')
    def test_cli_with_private_tmp_wrapper_verifies_existing_batch(self):
        batch=PROJECT/'runtime/integrated-archive-20261106/fixture/archived/20261101_1101'
        if not batch.is_dir():self.skipTest('real three-string archive fixture unavailable')
        parent=PROJECT/'runtime/archive-verifier-fix-20261106';parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as d:
            base=Path(d).resolve();wrapper=base/'root-private-tmp'
            wrapper.write_text(f'''#!{sys.executable}
import os,sys,tempfile
from pathlib import Path
for arg in sys.argv[1:]:
    if '.C(' in arg and Path(arg.split('(',1)[0]).parent.parent.resolve()==Path(tempfile.gettempdir()).resolve():
        sys.stderr.write('macro '+arg+' not found\\n');raise SystemExit(2)
os.execv({shutil.which('root')!r},[{shutil.which('root')!r}]+sys.argv[1:])
''');wrapper.chmod(0o755)
            with tempfile.TemporaryDirectory(prefix='seastar_verify_') as isolated:
                rejected=_inspect_root_with_cli(next(batch.glob('G3/rec_data/*.root')),root_bin=str(wrapper),work_dir=Path(isolated))
                self.assertFalse(rejected.valid);self.assertIn('not found',rejected.error)
            with patch('organizer.decoder_runner.has_pyroot',return_value=False):
                result=verify_dataset(batch,root_bin=str(wrapper),work_dir=base/'shared')
            self.assertTrue(result['ok'],result['errors']);self.assertEqual(len(result['groups']),3)
            self.assertTrue(all(g['root_total_entries']==g['clean_dat_wave_events']>0 for g in result['groups']))

if __name__=='__main__':unittest.main()
