"""One persistent system-SSH connection; the remote helper only reads the chosen DAT folder."""
from collections import deque
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import subprocess
import threading
import time


# Sent as an argument to python3 -B -u -c, not copied onto the Raspberry Pi.
# No output files, checkpoints, database, hashing pass, remote shell loop or imports from the project.
REMOTE_CODE = r'''
import errno, fnmatch, json, os, stat, sys
root = os.path.realpath(sys.argv[1])
pattern = sys.argv[2]
try:
    os.nice(10)
except OSError:
    pass
fd = None
active = None
active_noatime = False
def opened(path, directory=False):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    if directory:
        flags |= getattr(os, 'O_DIRECTORY', 0)
    extra = getattr(os, 'O_NOATIME', 0)
    if extra:
        try:
            return os.open(path, flags | extra), True
        except OSError as e:
            if e.errno not in (errno.EPERM, errno.EACCES, errno.EINVAL, errno.ENOTSUP):
                raise
    return os.open(path, flags), False
def safe_name(name):
    if not isinstance(name, str) or any(ord(c) in (0,47,92) for c in name) or name in ('.', '..'):
        raise ValueError('invalid filename')
    if not name.endswith('.dat') or not fnmatch.fnmatchcase(name, pattern):
        raise ValueError('filename outside DAT pattern')
    path = os.path.join(root, name)
    if os.path.islink(path) or os.path.dirname(os.path.realpath(path)) != root:
        raise ValueError('symlink is not allowed')
    return path
def info(s):
    return dict(size=s.st_size, identity=[s.st_dev,s.st_ino], mtime_ns=s.st_mtime_ns)
try:
    for line in sys.stdin.buffer:
        try:
            request=json.loads(line)
            op=request['op']
            payload=b''
            if op=='hello':
                s=os.stat(root)
                if not stat.S_ISDIR(s.st_mode):
                    raise ValueError('remote path is not a directory')
                result=dict(protocol=1, root=root, noatime_available=bool(getattr(os,'O_NOATIME',0)))
            elif op=='list':
                d,noatime=opened(root,True)
                try:
                    with os.scandir(d) as entries:
                        names=[e.name for e in entries if e.name.endswith('.dat') and fnmatch.fnmatchcase(e.name,pattern) and e.is_file(follow_symlinks=False)]
                        if len(names)>100000:
                            raise ValueError('too many DAT files; archive the source directory')
                finally:
                    os.close(d)
                payload=json.dumps(sorted(names),ensure_ascii=False).encode('utf-8')
                v=os.statvfs(root)
                result=dict(directory_noatime=noatime,free_bytes=v.f_bavail*v.f_frsize)
            elif op=='stat':
                s=os.stat(safe_name(request['name']),follow_symlinks=False)
                if not stat.S_ISREG(s.st_mode):
                    raise ValueError('not a regular file')
                result=info(s)
            elif op=='stats':
                names=request['names']
                if not isinstance(names,list) or len(names)>256:
                    raise ValueError('invalid stat batch')
                values={}
                for name in names:
                    s=os.stat(safe_name(name),follow_symlinks=False)
                    if not stat.S_ISREG(s.st_mode):
                        raise ValueError('not a regular file')
                    values[name]=info(s)
                payload=json.dumps(values,separators=(',',':')).encode()
                result={}
            elif op=='read':
                name=request['name']; path=safe_name(name)
                offset=request['offset']; length=request['length']
                if type(offset) is not int or offset<0 or type(length) is not int or not 0<=length<=1048576:
                    raise ValueError('invalid byte range')
                s=os.stat(path,follow_symlinks=False)
                if not stat.S_ISREG(s.st_mode):
                    raise ValueError('not a regular file')
                if active!=name:
                    if fd is not None:
                        os.close(fd)
                    fd,active_noatime=opened(path)
                    active=name
                actual=os.fstat(fd)
                if (s.st_dev,s.st_ino)!=(actual.st_dev,actual.st_ino):
                    raise ValueError('source file was replaced')
                if offset<actual.st_size and length:
                    os.lseek(fd,offset,os.SEEK_SET)
                    payload=os.read(fd,min(length,actual.st_size-offset))
                result=info(actual)
                result['noatime']=active_noatime
            else:
                raise ValueError('unsupported operation')
            result['bytes']=len(payload)
            sys.stdout.buffer.write(json.dumps(result,separators=(',',':')).encode()+b'\n'+payload)
            sys.stdout.buffer.flush()
        except Exception as e:
            sys.stdout.buffer.write(json.dumps(dict(bytes=0,error=str(e))).encode()+b'\n')
            sys.stdout.buffer.flush()
finally:
    if fd is not None:
        os.close(fd)
'''


class TransferConflict(RuntimeError):
    pass


@dataclass
class RemoteConfig:
    host: str
    remote_dir: str = "/home/pi/seastar-daq/data/both"
    pattern: str = "data_fee_3_*.dat"
    port: int = None
    identity_file: str = None
    known_hosts_file: str = None
    poll_seconds: float = 1.0
    scan_seconds: float = 10.0
    chunk_kib: int = 256
    max_mib_per_second: float = 2.0
    min_free_gib: float = 2.0
    request_timeout: float = 15.0
    start_mode: str = "latest"
    auth: str = "key"

    def __post_init__(self):
        if (not isinstance(self.host,str) or not re.fullmatch(r"[A-Za-z0-9_.@:\[\]-]+",self.host) or
                self.host.startswith('-') or "CHANGE_ME" in self.host):
            raise ValueError("请在 remote.json 填写树莓派 SSH 地址，例如 pi@192.168.1.20 或已有 SSH 别名")
        if not isinstance(self.remote_dir,str) or not self.remote_dir.startswith('/') or '\x00' in self.remote_dir:
            raise ValueError("remote_dir 必须是树莓派上的绝对目录")
        if not isinstance(self.pattern,str) or '/' in self.pattern or '\\' in self.pattern or not self.pattern.endswith('.dat'):
            raise ValueError("pattern 只允许当前目录中的 DAT 文件名模式")
        if self.port is not None and (type(self.port) is not int or not 1<=self.port<=65535):
            raise ValueError("SSH port 无效")
        for key,lo,hi in (("poll_seconds",.1,60),("scan_seconds",1,300),("chunk_kib",4,1024),
                          ("max_mib_per_second",.01,1000),("min_free_gib",0,100000),
                          ("request_timeout",2,120)):
            value=getattr(self,key)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not lo<=value<=hi:
                raise ValueError("远程参数超出范围: "+key)
        if type(self.chunk_kib) is not int or self.start_mode not in ("latest","all") or self.auth not in ('key','password'):
            raise ValueError("chunk_kib 必须为整数，start_mode 为 latest 或 all")
        for key in ("identity_file","known_hosts_file"):
            value=getattr(self,key)
            if value is not None:
                path=Path(value).expanduser().resolve()
                if not path.is_file():
                    raise ValueError(key+" 文件不存在")
                setattr(self,key,str(path))

    @classmethod
    def load(cls,path):
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))

    def source_key(self):
        return {key:getattr(self,key) for key in ("host","port","remote_dir","pattern")}


class SSHReader:
    def __init__(self,config,auth=None):
        self.config=config
        self.auth=auth
        self.proc=None
        self.buffer=bytearray()
        self.errors=deque(maxlen=16)
        self.error_thread=None
        self.lifecycle=threading.Lock()
        self.closed=False

    def command(self):
        ssh=shutil.which("ssh")
        if not ssh:
            raise TransferConflict("工控机未安装 OpenSSH 客户端；Linux 可安装 openssh-client")
        args=[ssh,"-T","-a","-x","-o","BatchMode="+('no' if self.config.auth=='password' else 'yes'),"-o","StrictHostKeyChecking=yes",
              "-o","Compression=no","-o","ConnectTimeout=8","-o","ConnectionAttempts=1",
              "-o","ServerAliveInterval=15","-o","ServerAliveCountMax=2",
              "-o","ClearAllForwardings=yes","-o","PermitLocalCommand=no",
              "-o","UpdateHostKeys=no","-o","ControlMaster=no","-o","ControlPath=none",
              "-o","LogLevel=ERROR"]
        if self.config.auth=='password':
            if self.auth is None:
                raise TransferConflict("密码模式尚未在工控机终端输入密码")
            args.extend(['-o','PreferredAuthentications=password','-o','PubkeyAuthentication=no',
                         '-o','KbdInteractiveAuthentication=no','-o','NumberOfPasswordPrompts=1',
                         '-o','ProxyCommand=none','-o','ProxyJump=none'])
        if self.config.port is not None:
            args.extend(["-p",str(self.config.port)])
        if self.config.identity_file:
            args.extend(["-i",self.config.identity_file])
        if self.config.known_hosts_file:
            args.extend(["-o","UserKnownHostsFile="+self.config.known_hosts_file])
        command="python3 -B -u -c "+shlex.quote(REMOTE_CODE)+" "+shlex.quote(self.config.remote_dir)+" "+shlex.quote(self.config.pattern)
        return args+[self.config.host,command]

    def connect(self):
        with self.lifecycle:
            if self.closed or self.proc is not None:
                raise ConnectionError("SSH 读取器已关闭或已连接")
            self.buffer.clear();self.errors.clear()
            env=os.environ.copy();env['LC_ALL']='C'
            if self.config.auth=='password':
                env.update(self.auth.environment())
                if not env.get('DISPLAY'):env['DISPLAY']='seastar-askpass'
            self.proc=subprocess.Popen(self.command(),stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE,bufsize=0,env=env,start_new_session=True)
            process=self.proc
            def stderr_reader():
                while True:
                    chunk=process.stderr.read(1024)
                    if not chunk:
                        break
                    self.errors.append(chunk.decode('utf-8',errors='replace'))
            self.error_thread=threading.Thread(target=stderr_reader,daemon=True)
            self.error_thread.start()
        hello,_=self.request({"op":"hello"})
        if hello.get("protocol")!=1:
            raise TransferConflict("远程只读协议版本不匹配")
        return hello

    def _fill(self,deadline):
        remaining=deadline-time.monotonic()
        if remaining<=0 or not select.select([self.proc.stdout],[],[],max(0,remaining))[0]:
            raise TimeoutError("树莓派读取响应超时；将按已提交偏移续传")
        data=os.read(self.proc.stdout.fileno(),65536)
        if not data:
            if self.error_thread:
                self.error_thread.join(timeout=.2)
            detail=''.join(self.errors)[-3000:].strip()
            lower=detail.lower()
            if any(word in lower for word in ("permission denied","host key verification failed","remote host identification has changed")):
                raise TransferConflict("SSH 认证或主机密钥检查失败；请先在工控机手动确认正常 SSH 登录。"+detail)
            if any(word in lower for word in ('syntaxerror','command not found','python3: not found','bad configuration option')):
                raise TransferConflict("远端 Python 或本机 SSH 配置无法启动只读协议。"+detail)
            raise ConnectionError("SSH 连接关闭。"+detail)
        self.buffer.extend(data)

    def request(self,message):
        if self.proc is None:
            raise ConnectionError("SSH 尚未连接")
        deadline=time.monotonic()+self.config.request_timeout
        data=(json.dumps(message,ensure_ascii=False,separators=(',',':'))+'\n').encode()
        view=memoryview(data)
        while view:
            written=self.proc.stdin.write(view)
            if not written:
                raise ConnectionError("SSH 请求写入失败")
            view=view[written:]
        while b'\n' not in self.buffer:
            if len(self.buffer)>65536:
                raise TransferConflict("SSH 输出不符合数据协议；请检查登录脚本是否向标准输出打印内容")
            self._fill(deadline)
        line,_,rest=self.buffer.partition(b'\n');self.buffer=bytearray(rest)
        try:
            reply=json.loads(line)
            size=reply['bytes']
            limit=16*1024**2 if message['op']=='list' else 1024**2
            if type(size) is not int or not 0<=size<=limit:
                raise ValueError("invalid payload size")
        except (ValueError,KeyError,TypeError) as error:
            raise TransferConflict("SSH 输出协议无效") from error
        if 'error' in reply:
            raise TransferConflict("树莓派只读访问失败: "+reply['error'])
        while len(self.buffer)<size:
            self._fill(deadline)
        payload=bytes(self.buffer[:size]);del self.buffer[:size]
        return reply,payload

    def listing(self):
        info,payload=self.request({"op":"list"})
        names=json.loads(payload)
        if not isinstance(names,list) or any(not isinstance(n,str) or Path(n).name!=n for n in names):
            raise TransferConflict("远端文件列表无效")
        return info,names

    def stat(self,name):
        info,_=self.request({"op":"stat","name":name})
        return info

    def stats(self,names):
        _,data=self.request({"op":"stats","names":names})
        return json.loads(data)

    def read(self,name,offset,length):
        return self.request({"op":"read","name":name,"offset":offset,"length":length})

    def close(self):
        with self.lifecycle:
            self.closed=True
            process=self.proc
            self.proc=None
        if process is None:
            return
        try:
            process.stdin.close()
            process.wait(timeout=.5)
        except (OSError,subprocess.TimeoutExpired):
            try:process.terminate()
            except ProcessLookupError:pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=1)
        finally:
            if self.error_thread:
                self.error_thread.join(timeout=.5)
            for stream in (process.stdout,process.stderr):
                stream.close()
