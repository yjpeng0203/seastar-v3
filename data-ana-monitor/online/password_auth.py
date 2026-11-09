"""IPC-only, owner-private askpass socket. The password is never written to a file or environment."""
import os
from pathlib import Path
import shlex
import socket
import struct
import sys
import tempfile
import threading


class PasswordBroker:
    def __init__(self,password):
        if not isinstance(password,str) or not password or len(password.encode('utf-8'))>4096 or any(c in password for c in ('\n','\r','\x00')):
            raise ValueError("SSH 密码为空、过长或含无效换行")
        self.secret=password.encode('utf-8')
        self.directory=tempfile.TemporaryDirectory(prefix='seastar-auth-',dir='/tmp')
        root=Path(self.directory.name)
        os.chmod(root,0o700)
        self.path=root/'password.sock'
        self.socket=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        try:
            self.socket.bind(str(self.path));os.chmod(self.path,0o600)
        except OSError:
            self.socket.close();self.secret=b'';self.directory.cleanup()
            raise
        self.socket.listen(4);self.socket.settimeout(.2)
        self.wrapper=root/'askpass.sh'
        helper=Path(__file__).resolve().parent/'ssh_askpass.py'
        self.wrapper.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(helper))+' "$@"\n',encoding='utf-8')
        os.chmod(self.wrapper,0o700)
        self.stop=threading.Event()
        self.thread=threading.Thread(target=self.serve,daemon=True,name='seastar-askpass')
        self.thread.start()

    def serve(self):
        while not self.stop.is_set():
            try:
                client,_=self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with client:
                client.settimeout(1)
                try:
                    if client.recv(1)==b'P':
                        client.sendall(struct.pack('>I',len(self.secret))+self.secret)
                except OSError:
                    pass

    def environment(self):
        return {'SSH_ASKPASS':str(self.wrapper),'SSH_ASKPASS_REQUIRE':'force',
                'SEASTAR_AUTH_SOCKET':str(self.path)}

    def close(self):
        self.stop.set();self.socket.close();self.thread.join(timeout=2)
        self.secret=b''
        self.directory.cleanup()
