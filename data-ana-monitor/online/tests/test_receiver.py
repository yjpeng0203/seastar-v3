"""Read-only helper, commit/crash boundaries and receive/analysis integration.

LocalSSH executes the exact remote helper through real pipes. It does not claim
to test network encryption or Raspberry Pi load; the optional SSH check is separate.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from acquisition import DatReader,discover
from mirror_state import committed_files
from password_auth import PasswordBroker
from receiver import Receiver
from remote_reader import REMOTE_CODE,RemoteConfig,SSHReader,TransferConflict
from test_online import trigger,waveform,gaussian
from server import Monitor


class LocalSSH(SSHReader):
    def command(self):
        return [sys.executable,'-B','-u','-c',REMOTE_CODE,self.config.remote_dir,self.config.pattern]


def cfg(source,**kwargs):
    return RemoteConfig('pi@test',remote_dir=str(source),poll_seconds=.1,scan_seconds=1,
                        chunk_kib=4,min_free_gib=0,**kwargs)


def source_info(path):
    s=path.stat()
    return {'identity':[s.st_dev,s.st_ino],'size':s.st_size,'mtime_ns':s.st_mtime_ns}


def wait_for(receiver,predicate,timeout=15):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        snap=receiver.snapshot()
        if predicate(snap):return snap
        if snap['status']=='blocked':raise AssertionError(snap['error'])
        time.sleep(.03)
    raise AssertionError('receiver timeout: '+str(receiver.snapshot()))


class ReceiveTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.source=self.root/'source'
        self.source.mkdir()
        self.name='data_fee_3_run000001_idx001_20261026120000.dat'
        self.file=self.source/self.name
        self.raw=trigger(0,10**9)+waveform(gaussian(),10**9+250)+trigger(1,2*10**9)
        self.file.write_bytes(self.raw)

    def tearDown(self):
        self.temp.cleanup()

    def test_helper_read_ranges_binary_framing_and_scope(self):
        peer=LocalSSH(cfg(self.source))
        before=self.file.stat()
        try:
            self.assertEqual(peer.connect()['protocol'],1)
            info,names=peer.listing()
            self.assertEqual(names,[self.name])
            self.assertGreaterEqual(info['free_bytes'],0)
            self.assertEqual(peer.stats(names)[self.name]['size'],len(self.raw))
            all_bytes=b''
            for offset in range(0,len(self.raw),37):
                header,data=peer.read(self.name,offset,37)
                all_bytes+=data
                self.assertEqual(header['identity'],[before.st_dev,before.st_ino])
            self.assertEqual(all_bytes,self.raw)
            self.assertEqual(peer.read(self.name,len(self.raw),100)[1],b'')
            for bad in ('../outside.dat','/etc/passwd','other.csv'):
                with self.assertRaises(TransferConflict):peer.read(bad,0,10)
            link=self.source/'data_fee_3_link.dat'
            link.symlink_to(self.file)
            self.assertNotIn(link.name,peer.listing()[1])
        finally:
            peer.close()
        self.assertEqual(self.file.read_bytes(),self.raw)
        self.assertEqual(self.file.stat().st_mtime_ns,before.st_mtime_ns)
        self.assertFalse((self.source/'__pycache__').exists())

    def test_uncommitted_bytes_are_hidden_and_recovered(self):
        r=Receiver(cfg(self.source),self.root/'mirror')
        entry=r.register(self.name,source_info(self.file))
        try:
            with patch.object(r,'save',side_effect=sqlite3.OperationalError('simulated commit failure')):
                with self.assertRaises(sqlite3.OperationalError):
                    r.commit(self.name,entry,self.raw,source_info(self.file))
            self.assertEqual((r.directory/self.name).read_bytes(),self.raw)
            source=discover(r.directory)[0]
            self.assertEqual(source['bytes'],0)
            self.assertEqual(DatReader(r.directory).read(source),b'')
        finally:r.close()
        r=Receiver(cfg(self.source),self.root/'mirror')
        try:
            self.assertEqual((r.directory/self.name).stat().st_size,0)
            self.assertEqual(committed_files(r.directory)[self.name]['committed'],0)
            r.commit(self.name,r.manifest['files'][self.name],self.raw,source_info(self.file))
            self.assertEqual(DatReader(r.directory).read(discover(r.directory)[0]),self.raw)
        finally:r.close()

    def test_existing_data_and_duplicate_receivers_are_protected(self):
        mirror=self.root/'mirror';mirror.mkdir()
        original=mirror/self.name;original.write_bytes(b'original data')
        with self.assertRaises(TransferConflict):Receiver(cfg(self.source),mirror)
        self.assertEqual(original.read_bytes(),b'original data')
        r=Receiver(cfg(self.source),self.root/'empty')
        try:
            with self.assertRaises(TransferConflict):Receiver(cfg(self.source),self.root/'empty')
        finally:r.close()
        with self.assertRaises(TransferConflict):
            Receiver(RemoteConfig('other@host',remote_dir=str(self.source)),self.root/'empty')

    def test_latest_run_only_and_idle_does_not_reread_history(self):
        newer=self.source/self.name.replace('run000001','run000002')
        newer.write_bytes(self.raw)
        with patch('receiver.SSHReader',LocalSSH):
            r=Receiver(cfg(self.source),self.root/'mirror');r.start()
            try:
                snap=wait_for(r,lambda s:s['committed_bytes']==len(self.raw))
                self.assertEqual(set(r.manifest['files']),{newer.name})
                read=snap['network_read_bytes'];connections=snap['connections']
                time.sleep(.5)
                self.assertEqual(r.snapshot()['network_read_bytes'],read)
                self.assertEqual(r.snapshot()['connections'],connections)
            finally:r.close()

    def test_pause_append_resume_and_source_replacement(self):
        with patch('receiver.SSHReader',LocalSSH):
            r=Receiver(cfg(self.source),self.root/'mirror');r.start()
            try:
                wait_for(r,lambda s:s['committed_bytes']==len(self.raw))
                r.pause();wait_for(r,lambda s:s['status']=='paused')
                extra=waveform(gaussian(),2*10**9+250)
                with self.file.open('ab') as stream:stream.write(extra)
                time.sleep(.2)
                self.assertEqual(r.snapshot()['committed_bytes'],len(self.raw))
                r.resume();wait_for(r,lambda s:s['committed_bytes']==len(self.raw)+len(extra))
                replacement=self.source/'replacement.tmp';replacement.write_bytes(self.raw)
                replacement.replace(self.file)
                stopped=wait_for(r,lambda s:s['status']=='blocked')
                self.assertTrue('替换' in stopped['error'] or 'replaced' in stopped['error'])
                self.assertEqual((r.directory/self.name).read_bytes(),self.raw+extra)
            finally:r.close()

    def test_disconnect_after_read_resumes_without_duplicate_append(self):
        class DropOnce(LocalSSH):
            dropped=False
            def read(self,name,offset,length):
                header,data=super().read(name,offset,length)
                if data and length>128 and not type(self).dropped:
                    type(self).dropped=True
                    raise ConnectionError('simulated connection loss after remote read')
                return header,data
        with patch('receiver.SSHReader',DropOnce):
            r=Receiver(cfg(self.source),self.root/'mirror');r.start()
            try:
                snap=wait_for(r,lambda s:s['committed_bytes']==len(self.raw))
                self.assertGreaterEqual(snap['connections'],2)
                self.assertEqual((r.directory/self.name).read_bytes(),self.raw)
            finally:r.close()

    def test_disk_reserve_prevents_any_source_connection(self):
        from types import SimpleNamespace
        with patch('receiver.SSHReader',LocalSSH),patch('receiver.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
            r=Receiver(cfg(self.source),self.root/'mirror');r.start()
            try:
                snap=wait_for(r,lambda s:s['status']=='disk_full')
                self.assertEqual(snap['connections'],0)
                self.assertEqual(snap['network_read_bytes'],0)
            finally:r.close()

    def test_rate_limit_is_enforced(self):
        self.file.write_bytes(self.raw*10)
        with patch('receiver.SSHReader',LocalSSH):
            r=Receiver(cfg(self.source,max_mib_per_second=.01),self.root/'mirror')
            start=time.monotonic();r.start()
            try:
                wait_for(r,lambda s:s['committed_bytes']>=8192)
                self.assertGreaterEqual(time.monotonic()-start,.3)
            finally:r.close()

    def test_source_truncation_does_not_truncate_ipc_data(self):
        with patch('receiver.SSHReader',LocalSSH):
            r=Receiver(cfg(self.source),self.root/'mirror');r.start()
            try:
                wait_for(r,lambda s:s['committed_bytes']==len(self.raw))
                self.file.write_bytes(b'')
                wait_for(r,lambda s:s['status']=='blocked')
                self.assertEqual((r.directory/self.name).read_bytes(),self.raw)
            finally:r.close()

    def test_password_askpass_has_no_secret_files_or_environment(self):
        secret='fake-unit-test-password-不是实际密码'
        auth=PasswordBroker(secret)
        directory=Path(auth.directory.name)
        try:
            self.assertEqual(directory.stat().st_mode&0o777,0o700)
            self.assertEqual(auth.path.stat().st_mode&0o777,0o600)
            env=os.environ.copy();env.update(auth.environment())
            self.assertNotIn(secret,json.dumps(auth.environment()))
            self.assertNotIn(secret,auth.wrapper.read_text())
            helper=Path(__file__).resolve().parents[1]/'ssh_askpass.py'
            answer=subprocess.run([sys.executable,str(helper),"pi@test's password:"],env=env,capture_output=True,timeout=5)
            self.assertEqual(answer.returncode,0)
            self.assertEqual(answer.stdout,(secret+'\n').encode())
            for prompt in ('Are you sure (yes/no)?','New password:','Retype new password:','Enter passphrase for key:'):
                denied=subprocess.run([sys.executable,str(helper),prompt],env=env,capture_output=True,timeout=5)
                self.assertNotEqual(denied.returncode,0);self.assertFalse(denied.stdout)
            c=RemoteConfig('pi@test',auth='password')
            command=SSHReader(c,auth=auth).command()
            self.assertNotIn(secret,' '.join(command))
            self.assertIn('StrictHostKeyChecking=yes',command)
            self.assertIn('NumberOfPasswordPrompts=1',command)
            self.assertEqual(set(asdict(c))&{'password','secret'},set())
        finally:auth.close()
        self.assertFalse(directory.exists())

    def test_receiver_and_monitor_commit_exactly_once_across_restart(self):
        state=self.root/'analysis';mirror=self.root/'mirror'
        with patch('receiver.SSHReader',LocalSSH):
            r=Receiver(cfg(self.source),mirror)
            m=Monitor(mirror,state,receiver=r,poll=.1)
            r.start();m.start()
            try:
                wait_for(r,lambda s:s['committed_bytes']==len(self.raw))
                end=time.monotonic()+15
                while time.monotonic()<end and m.snapshot().get('waves')!=1:time.sleep(.05)
                self.assertEqual(m.snapshot().get('waves'),1)
                run=m.snapshot()['run_id']
            finally:m.close();r.close()
            extra=waveform(gaussian(),2*10**9+250)+trigger(2,3*10**9)
            with self.file.open('ab') as stream:stream.write(extra)
            # Simulate bytes written before a receive checkpoint was durable.
            with (mirror/self.name).open('ab') as stream:stream.write(b'uncommitted tail')
            r=Receiver(cfg(self.source),mirror)
            self.assertEqual((mirror/self.name).read_bytes(),self.raw)
            m=Monitor(mirror,state,receiver=r,poll=.1)
            r.start();m.start()
            try:
                wait_for(r,lambda s:s['committed_bytes']==len(self.raw)+len(extra))
                end=time.monotonic()+15
                while time.monotonic()<end and m.snapshot().get('waves')!=2:time.sleep(.05)
                snap=m.snapshot();self.assertEqual(snap['waves'],2);self.assertEqual(snap['run_id'],run)
                self.assertTrue(snap['transfer'])
            finally:m.close();r.close()
            db=sqlite3.connect(state/'analysis.sqlite3')
            self.assertEqual(db.execute('SELECT COUNT(*),COUNT(DISTINCT seq) FROM metrics WHERE run=?',(run,)).fetchone(),(2,2))
            db.close()


if __name__=='__main__':unittest.main()
