"""Developer-only encrypted loopback SSH server. Paramiko is NOT a runtime dependency."""
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import threading

from remote_reader import REMOTE_CODE,RemoteConfig


class PasswordSSH:
    def __init__(self,source):
        import paramiko
        self.paramiko=paramiko
        self.source=Path(source).resolve()
        self.password='temporary-test-password-only'
        self.host_key=paramiko.ECDSAKey.generate()
        self.temp=tempfile.TemporaryDirectory(prefix='seastar-password-ssh-',dir='/tmp')
        self.root=Path(self.temp.name)
        self.listener=socket.socket();self.listener.bind(('127.0.0.1',0));self.listener.listen(8);self.listener.settimeout(.2)
        self.port=self.listener.getsockname()[1]
        self.known=self.root/'known_hosts'
        self.known.write_text('[127.0.0.1]:'+str(self.port)+' '+self.host_key.get_name()+' '+self.host_key.get_base64()+'\n')
        self.stop=threading.Event();self.transports=[];self.helpers=[];self.threads=[]
        self.auth_successes=0;self.auth_failures=0
        self.thread=threading.Thread(target=self.serve,daemon=True);self.thread.start()

    def config(self):
        return RemoteConfig('test-user@127.0.0.1',remote_dir=str(self.source),port=self.port,
                            known_hosts_file=str(self.known),auth='password',poll_seconds=.1,
                            scan_seconds=1,chunk_kib=64,max_mib_per_second=10,min_free_gib=0)

    def serve(self):
        while not self.stop.is_set():
            try:connection,_=self.listener.accept()
            except socket.timeout:continue
            except OSError:break
            thread=threading.Thread(target=self.session,args=(connection,),daemon=True)
            self.threads.append(thread);thread.start()

    def session(self,connection):
        p=self.paramiko;owner=self
        cfg=self.config()
        expected="python3 -B -u -c "+shlex.quote(REMOTE_CODE)+" "+shlex.quote(cfg.remote_dir)+" "+shlex.quote(cfg.pattern)
        ready=threading.Event()
        class Server(p.ServerInterface):
            command=None
            def get_allowed_auths(self,username):return 'password'
            def check_auth_password(self,username,password):
                if username=='test-user' and password==owner.password:
                    owner.auth_successes+=1;return p.AUTH_SUCCESSFUL
                owner.auth_failures+=1;return p.AUTH_FAILED
            def check_channel_request(self,kind,chanid):
                return p.OPEN_SUCCEEDED if kind=='session' else p.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
            def check_channel_exec_request(self,channel,command):
                if command.decode()!=expected:return False
                self.command=command.decode();ready.set();return True
        transport=p.Transport(connection);self.transports.append(transport)
        helper=None
        try:
            transport.add_server_key(self.host_key)
            server=Server();transport.start_server(server=server)
            channel=transport.accept(timeout=10)
            if channel is None or not ready.wait(10):return
            # The equality check above allows only the exact project's read-only helper.
            helper=subprocess.Popen(['/bin/sh','-c',server.command],stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
            self.helpers.append(helper)
            def incoming():
                try:
                    while True:
                        data=channel.recv(65536)
                        if not data:break
                        view=memoryview(data)
                        while view:view=view[helper.stdin.write(view):]
                except (OSError,EOFError):pass
                finally:
                    try:helper.stdin.close()
                    except OSError:pass
            thread=threading.Thread(target=incoming,daemon=True);thread.start()
            while True:
                data=os.read(helper.stdout.fileno(),65536)
                if not data:break
                channel.sendall(data)
            helper.wait(timeout=3)
            channel.send_exit_status(helper.returncode)
        except (OSError,EOFError,p.SSHException,subprocess.TimeoutExpired):
            pass
        finally:
            if helper:
                if helper.poll() is None:helper.terminate();helper.wait(timeout=3)
                for stream in (helper.stdin,helper.stdout,helper.stderr):stream.close()
            transport.close();connection.close()

    def drop(self):
        for transport in list(self.transports):transport.close()

    def close(self):
        self.stop.set();self.listener.close();self.drop();self.thread.join(timeout=2)
        for helper in self.helpers:
            if helper.poll() is None:helper.terminate()
        for thread in self.threads:thread.join(timeout=3)
        self.temp.cleanup()


@contextmanager
def password_ssh(source):
    server=PasswordSSH(source)
    try:yield server
    finally:server.close()
