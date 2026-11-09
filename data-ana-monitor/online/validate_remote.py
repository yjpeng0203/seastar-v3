#!/usr/bin/env python3
"""Developer-only full encrypted/password SSH receive + analysis fault test.

Optional test server needs Paramiko; production needs only system OpenSSH.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import threading
import time

from acquisition import DatParser
from password_auth import PasswordBroker
from receiver import Receiver
from remote_reader import SSHReader,TransferConflict
from server import Monitor


def validate(sample):
    sys.path.insert(0,str(Path(__file__).resolve().parent/'tests'))
    from password_ssh_fixture import password_ssh
    from test_online import trigger,waveform,gaussian
    raw=sample.read_bytes()
    expected_triggers=expected_waves=0
    for record in DatParser().feed(raw):
        expected_triggers+=record['type']=='trigger';expected_waves+=record['type']=='wave'
    with tempfile.TemporaryDirectory(prefix='seastar-remote-check-',dir='/tmp') as directory:
        root=Path(directory);source=root/"source's directory";source.mkdir()
        mirror=root/'mirror';state=root/'analysis'
        stopped=threading.Event();started=threading.Event();errors=[]
        def writer():
            rng=random.Random(19);offset=0;volume=0;begin=time.monotonic()
            try:
                while offset<len(raw) and not stopped.is_set():
                    volume+=1;end=min(len(raw),offset+256*1024+7)
                    path=source/f'data_fee_3_run000001_idx{volume:03d}_20261026120000.dat'
                    with path.open('xb') as stream:
                        started.set()
                        while offset<end and not stopped.is_set():
                            n=min(end-offset,rng.randint(1,16384))
                            stream.write(raw[offset:offset+n]);stream.flush();offset+=n
                            wait=offset/(1024**2)-(time.monotonic()-begin)
                            if wait>0:stopped.wait(wait)
            except Exception as error:errors.append(str(error))
        producer=threading.Thread(target=writer,daemon=True);producer.start();assert started.wait(3)
        monitor=receiver=None
        with password_ssh(source) as ssh_server:
            config=ssh_server.config()
            auth=PasswordBroker(ssh_server.password)
            begin=time.monotonic()
            def wait(predicate,timeout=90):
                end=time.monotonic()+timeout
                while time.monotonic()<end:
                    rs=receiver.snapshot() if receiver else {}
                    ms=monitor.snapshot() if monitor else {}
                    if errors or rs.get('status')=='blocked' or ms.get('error'):
                        raise AssertionError(errors or rs.get('error') or ms.get('error'))
                    if predicate(rs,ms):return rs,ms
                    time.sleep(.05)
                raise AssertionError('remote replay timed out: '+str(receiver.snapshot()))
            try:
                # Real OpenSSH must request the fake password through the private local broker.
                receiver=Receiver(config,mirror,auth=auth)
                monitor=Monitor(mirror,state,receiver=receiver,poll=.1)
                receiver.start();monitor.start()
                wait(lambda r,m:m.get('waves',0)>=1000)
                print('Encrypted password SSH: receiving real sample',flush=True)
                run_id=monitor.snapshot()['run_id']
                ssh_server.drop()
                wait(lambda r,m:r.get('connections',0)>=2 and m.get('waves',0)>=1500)
                print('Connection loss recovered without another password prompt',flush=True)
                receiver.close()
                target=receiver.snapshot()['committed_bytes']
                def durable_caught_up(_r,m):
                    db=sqlite3.connect(state/'analysis.sqlite3')
                    saved=db.execute('SELECT state FROM runs WHERE id=?',(run_id,)).fetchone();db.close()
                    saved=json.loads(saved[0])
                    return sum(saved['reader']['offsets'].values())==target and m.get('waves')==saved['engine']['waves']
                wait(durable_caught_up)
                before=monitor.snapshot().get('waves',0)
                active=mirror/receiver.snapshot()['file']
                junk=trigger(65000,999999*10**9)+waveform(gaussian(),999999*10**9+250)
                with active.open('ab') as stream:stream.write(junk)
                time.sleep(.3)
                assert monitor.snapshot().get('waves',0)==before,'uncommitted tail leaked into analysis'
                monitor.close()
                receiver=Receiver(config,mirror,auth=auth)
                monitor=Monitor(mirror,state,receiver=receiver,poll=.1)
                receiver.start();monitor.start()
                rs,ms=wait(lambda r,m:not producer.is_alive() and m.get('waves')==expected_waves and
                           m.get('triggers')==expected_triggers and not m.get('pending_bytes') and
                           not m.get('backlog') and r.get('committed_bytes')==len(raw))
                assert ms['run_id']==run_id
                assert not ms['bad_bytes'] and not ms['orphan_waves']
                files=sorted(mirror.glob('*.dat'))
                digest=hashlib.sha256(b''.join(p.read_bytes() for p in files)).hexdigest()
                assert digest==hashlib.sha256(raw).hexdigest()
                wrong=PasswordBroker('intentionally-wrong-test-password')
                peer=SSHReader(config,auth=wrong)
                try:
                    try:peer.connect();raise AssertionError('wrong password accepted')
                    except TransferConflict:pass
                finally:peer.close();wrong.close()
                assert ssh_server.auth_failures==1
                attempts=ssh_server.auth_successes+ssh_server.auth_failures
                bad_known=root/'wrong_known_hosts'
                other=ssh_server.paramiko.ECDSAKey.generate()
                bad_known.write_text('[127.0.0.1]:'+str(config.port)+' '+other.get_name()+' '+other.get_base64()+'\n')
                peer=SSHReader(replace(config,known_hosts_file=str(bad_known)),auth=auth)
                try:
                    try:peer.connect();raise AssertionError('wrong host key accepted')
                    except TransferConflict:pass
                finally:peer.close()
                assert attempts==ssh_server.auth_successes+ssh_server.auth_failures,'password sent before host-key verification'
                print('Checksum, restart, wrong-password and host-key checks passed',flush=True)
                report={'passed':True,'real_ssh_encryption':True,'password_authentication':True,
                        'password_reused_after_connection_loss':True,'wrong_password_stopped_after_one_attempt':True,
                        'host_key_checked_before_password':True,'unaligned_volumes':len(files),
                        'network_drop':True,'receiver_and_analysis_restart':True,'uncommitted_tail_hidden':True,
                        'same_analysis_session':True,'triggers':ms['triggers'],'waves':ms['waves'],'good':ms['good'],
                        'received_bytes':rs['committed_bytes'],'sha256':digest,
                        'successful_password_authentications':ssh_server.auth_successes,
                        'final_receiver_connections':rs['connections'],
                        'analysis_sha256':ms['analysis_sha256'],'kernel_sha256':ms['kernel_sha256'],
                        'elapsed_s':time.monotonic()-begin,'real_raspberry_pi_connected':False,
                        'source_has_no_project_state_files':not any(p.suffix in ('.py','.pyc','.json','.sqlite3') for p in source.iterdir())}
            finally:
                stopped.set();producer.join(timeout=3)
                if monitor:monitor.close()
                # close() has already been called at the simulated restart, so use only the current instance.
                if receiver:receiver.close()
                auth.close()
        db=sqlite3.connect(state/'analysis.sqlite3')
        count,unique=db.execute('SELECT COUNT(*),COUNT(DISTINCT seq) FROM metrics WHERE run=?',(run_id,)).fetchone();db.close()
        assert count==unique==expected_waves
        report.update(persisted_metrics=count,unique_metric_sequences=unique)
        return report


if __name__=='__main__':
    import argparse
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('sample',type=Path);cli.add_argument('--output',type=Path,required=True)
    args=cli.parse_args()
    result=validate(args.sample)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
