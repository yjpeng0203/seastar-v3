"""Optional real OpenSSH loopback fixture. No system SSH configuration is changed."""
from contextlib import contextmanager
import base64
import getpass
import json
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from remote_reader import REMOTE_CODE,RemoteConfig


@contextmanager
def loopback_ssh(source,pattern='data_fee_3_*.dat'):
    sshd=shutil.which('sshd') or '/usr/sbin/sshd'
    if not Path(sshd).is_file() or not shutil.which('ssh-keygen'):
        raise RuntimeError('本机没有 sshd/ssh-keygen，无法运行可选加密链路测试')
    with tempfile.TemporaryDirectory(prefix='seastar-ssh-',dir='/tmp') as directory:
        root=Path(directory)
        for name in ('host','client'):
            subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(root/name)],
                           check=True,capture_output=True,timeout=10)
        # Even the disposable test key may execute only this read-only helper in this folder.
        payload=base64.b64encode(REMOTE_CODE.encode()).decode('ascii')
        forced=' '.join(shlex.quote(s) for s in [sys.executable,'-B','-u','-c',
                                               "import base64;exec(base64.b64decode('"+payload+"'))",str(Path(source).resolve()),pattern])
        public=(root/'client.pub').read_text().strip()
        (root/'authorized_keys').write_text('restrict,command='+json.dumps(forced)+' '+public+'\n')
        (root/'authorized_keys').chmod(0o600)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        config=root/'sshd.conf'
        config.write_text('\n'.join([
            'Port '+str(port),'ListenAddress 127.0.0.1','HostKey '+str(root/'host'),
            'PidFile '+str(root/'pid'),'AuthorizedKeysFile '+str(root/'authorized_keys'),
            'PasswordAuthentication no','KbdInteractiveAuthentication no','PubkeyAuthentication yes',
            'AuthenticationMethods publickey','UsePAM no','StrictModes no','PermitRootLogin no',
            'AllowUsers '+getpass.getuser(),'LogLevel ERROR','AllowTcpForwarding no','X11Forwarding no',
        ])+'\n')
        log=(root/'sshd.log').open('w+b')
        process=subprocess.Popen([sshd,'-D','-e','-f',str(config)],stdin=subprocess.DEVNULL,stdout=log,stderr=log)
        try:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if process.poll() is not None:
                    log.seek(0)
                    raise RuntimeError('临时 sshd 无法启动: '+log.read().decode(errors='replace'))
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.2):
                        break
                except OSError:
                    time.sleep(.05)
            else:raise RuntimeError('临时 sshd 启动超时')
            host=(root/'host.pub').read_text().split()
            known=root/'known_hosts'
            known.write_text('[127.0.0.1]:'+str(port)+' '+host[0]+' '+host[1]+'\n')
            yield RemoteConfig(getpass.getuser()+'@127.0.0.1',remote_dir=str(Path(source).resolve()),
                               pattern=pattern,port=port,identity_file=str(root/'client'),
                               known_hosts_file=str(known),poll_seconds=.1,scan_seconds=1,
                               chunk_kib=64,max_mib_per_second=20,min_free_gib=0)
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
            log.close()
