#!/usr/bin/env python3
"""Called only by system SSH for account-password authentication, never for host-key approval."""
import os
import re
import socket
import struct
import sys


def exact(stream,size):
    value=bytearray()
    while len(value)<size:
        data=stream.recv(size-len(value))
        if not data:
            raise ConnectionError('askpass socket closed')
        value.extend(data)
    return bytes(value)


def main():
    prompt=sys.argv[1].strip().lower() if len(sys.argv)>1 else ''
    if not (prompt=='password:' or re.fullmatch(r"[^\r\n]+@[^\r\n]+'s password:",prompt)):
        return 1
    path=os.environ.get('SEASTAR_AUTH_SOCKET')
    if not path:
        return 1
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as stream:
        stream.settimeout(3);stream.connect(path);stream.sendall(b'P')
        length=struct.unpack('>I',exact(stream,4))[0]
        if not 0<length<=4096:
            return 1
        sys.stdout.buffer.write(exact(stream,length)+b'\n')
    return 0


if __name__=='__main__':
    try:
        sys.exit(main())
    except (OSError,ValueError,ConnectionError):
        sys.exit(1)
