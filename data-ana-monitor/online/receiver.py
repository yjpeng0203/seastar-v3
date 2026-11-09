"""Read-only remote DAT to a committed IPC mirror. All mutable state is on the IPC."""
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sqlite3
import threading
import time

from mirror_state import sync_directory
from remote_reader import RemoteConfig, SSHReader, TransferConflict

DAQ_NAME = re.compile(r"^(.*)_run(\d+)_idx(\d+)_(\d{14})\.dat$", re.I)


def name_parts(name):
    match=DAQ_NAME.match(name)
    if not match:
        return None
    return match[1],int(match[2]),int(match[3]),match[4]


class Receiver:
    def __init__(self,config,directory,auth=None):
        self.config=config
        self.auth=auth
        self.directory=Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True,exist_ok=True)
        self.meta=self.directory/".receiver"
        if self.meta.is_symlink():
            raise TransferConflict("接收元数据目录不能是符号链接")
        if not self.meta.exists() and any(self.directory.glob('*.dat')):
            raise TransferConflict("接收目录已有非本接收器管理的 DAT；请选择空目录，避免覆盖原始数据")
        self.meta.mkdir(exist_ok=True)
        self.lock_file=(self.meta/'lock').open('a+b')
        try:
            fcntl.flock(self.lock_file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.lock_file.close()
            raise TransferConflict("同一接收目录已被另一进程占用") from error
        self.manifest_path=self.meta/'receive.sqlite3'
        try:
            self.db=sqlite3.connect(self.manifest_path,check_same_thread=False,timeout=10)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA journal_size_limit=16777216')
            self.db.execute('CREATE TABLE IF NOT EXISTS receive_state (id INTEGER PRIMARY KEY,value TEXT NOT NULL)')
            self.db.commit()
            row=self.db.execute('SELECT value FROM receive_state WHERE id=1').fetchone()
            if row:
                self.manifest=json.loads(row[0])
                if self.manifest.get('version')!=1 or self.manifest.get('source')!=config.source_key():
                    raise TransferConflict("接收目录对应另一台树莓派/路径/模式；更换源时请选择新的接收目录")
            else:
                # A crashed first initialization is distinguishable from an unowned data folder.
                if any(self.directory.glob('*.dat')):
                    raise TransferConflict("有 DAT 但缺少接收清单，不能自动接管或覆盖")
                self.manifest={'version':1,'source':config.source_key(),'files':{},'selected_runs':[]}
                self.save()
            self.recover()
        except Exception:
            if hasattr(self,'db'):self.db.close()
            self.lock_file.close()
            raise
        self.stop=threading.Event()
        self.paused=threading.Event()
        self.resume_requested=threading.Event()
        self.view_lock=threading.Lock()
        self.peer=None
        self.verified=set()
        self.names=[]
        self.todo=[]
        self.active=[]
        self.active_index=0
        self.remote_sizes={}
        self.last_scan=0.
        self.next_read=0.
        self.last_commit=None
        self.last_check=None
        self.source_free=None
        self.file_noatime=None
        self.directory_noatime=None
        self.connections=0
        self.read_bytes=0
        self.started=time.monotonic()
        self.status='starting'
        self.error=None
        self.current=None
        self.cached={}
        self.publish()
        self.thread=threading.Thread(target=self.work,daemon=True,name='seastar-receiver')

    def save(self):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO receive_state VALUES (1,?)',
                            (json.dumps(self.manifest,ensure_ascii=False,separators=(',',':'),allow_nan=False),))

    def local_path(self,name):
        if Path(name).name!=name or not name.endswith('.dat') or '/' in name or '\\' in name:
            raise TransferConflict("接收文件名无效")
        path=self.directory/name
        if path.is_symlink():
            raise TransferConflict("接收 DAT 不允许是符号链接")
        return path

    def recover(self):
        for name,entry in self.manifest['files'].items():
            path=self.local_path(name)
            offset=entry['committed']
            if type(offset) is not int or offset<0:
                raise TransferConflict("接收偏移无效")
            if entry.get('pending_create'):
                if path.exists() and path.stat().st_size:
                    raise TransferConflict("未完成创建的接收文件含未知数据，停止以保护文件")
                self.create_local(name,entry)
            if not path.is_file():
                raise TransferConflict("已提交的本地文件缺失: "+name)
            s=path.stat()
            if [s.st_dev,s.st_ino]!=entry['local_identity'] or s.st_size<offset:
                raise TransferConflict("已提交的本地文件被替换或截短: "+name)
            with path.open('r+b') as stream:
                if entry.get('head'):
                    head=bytes.fromhex(entry['head']);stream.seek(0)
                    if stream.read(len(head))!=head:
                        raise TransferConflict("本地接收文件头改变: "+name)
                if entry.get('tail'):
                    tail=bytes.fromhex(entry['tail']);stream.seek(offset-len(tail))
                    if stream.read(len(tail))!=tail:
                        raise TransferConflict("本地已提交尾部改变: "+name)
                if s.st_size>offset:
                    # Only this receiver's uncommitted bytes may be removed, never committed data.
                    stream.truncate(offset);stream.flush();os.fsync(stream.fileno())

    def create_local(self,name,entry):
        path=self.local_path(name)
        if not path.exists():
            with path.open('xb') as stream:
                stream.flush();os.fsync(stream.fileno())
            sync_directory(self.directory)
        s=path.stat()
        if not stat.S_ISREG(s.st_mode) or s.st_size:
            raise TransferConflict("接收创建阶段遇到非空/非普通文件: "+name)
        entry['local_identity']=[s.st_dev,s.st_ino]
        entry['pending_create']=False
        self.save()

    def register(self,name,info):
        if name in self.manifest['files']:
            return self.manifest['files'][name]
        path=self.local_path(name)
        if path.exists():
            raise TransferConflict("不覆盖已有同名本地文件: "+name)
        entry={'committed':0,'identity':info['identity'],'head':'','tail':'',
               'source_size':info['size'],'source_mtime_ns':info['mtime_ns'],
               'local_identity':None,'pending_create':True}
        self.manifest['files'][name]=entry
        self.save()  # Persist ownership before creating even a zero-length DAT.
        self.create_local(name,entry)
        return entry

    def scan(self):
        info,names=self.peer.listing()
        parsed={name:name_parts(name) for name in names}
        parsed={name:parts for name,parts in parsed.items() if parts is not None}
        chosen=set(self.manifest['selected_runs'])
        latest={}
        for name,(prefix,run,idx,stamp) in parsed.items():
            key=(stamp,run)
            if self.config.start_mode=='all':
                chosen.add(prefix+'|'+stamp+'|'+str(run))
            elif prefix not in latest or key>latest[prefix]:
                latest[prefix]=key
        for prefix,(stamp,run) in latest.items():
            chosen.add(prefix+'|'+stamp+'|'+str(run))
        wanted=[name for name,(prefix,run,idx,stamp) in parsed.items()
                if prefix+'|'+stamp+'|'+str(run) in chosen]
        missing=set(self.manifest['files'])-set(names)
        if missing:
            raise TransferConflict("源目录中的已接收文件消失，停止自动接收: "+sorted(missing)[0])
        self.names=sorted(wanted,key=lambda n:(parsed[n][3],parsed[n][1],parsed[n][0],parsed[n][2]))
        metadata={}
        for start in range(0,len(self.names),256):
            metadata.update(self.peer.stats(self.names[start:start+256]))
        for name,entry in self.manifest['files'].items():
            if name in metadata:
                self.validate_info(name,entry,metadata[name])
                if metadata[name]['size']>entry['committed']:
                    prefix,run,idx,stamp=parsed[name]
                    later=[other for other in self.manifest['files'] if parsed.get(other) and
                           parsed[other][0]==prefix and parsed[other][1]==run and parsed[other][3]==stamp and
                           parsed[other][2]>idx and self.manifest['files'][other]['committed']>0]
                    if later:
                        raise TransferConflict("已切换的源分卷再次增长，停止避免乱序: "+name)
        self.remote_sizes={n:i['size'] for n,i in metadata.items()}
        self.todo=[n for n in self.names if n not in self.manifest['files'] or
                   metadata[n]['size']>self.manifest['files'][n]['committed']]
        active={}
        for name in self.names:
            prefix,run,idx,stamp=parsed[name]
            if prefix not in active or (stamp,run,idx)>(parsed[active[prefix]][3],parsed[active[prefix]][1],parsed[active[prefix]][2]):
                active[prefix]=name
        self.active=sorted(active.values())
        self.active_index=0
        if sorted(chosen)!=self.manifest['selected_runs']:
            self.manifest['selected_runs']=sorted(chosen)
            self.save()
        self.source_free=info.get('free_bytes')
        self.directory_noatime=info.get('directory_noatime')
        self.last_scan=time.monotonic()
        self.last_check=time.time()

    def validate_info(self,name,entry,info):
        if info['identity']!=entry['identity'] or info['size']<entry['committed']:
            raise TransferConflict("树莓派文件被替换/截短，拒绝覆盖本地历史: "+name)
        if (entry['committed']>0 and info['size']==entry['committed'] and
                entry.get('source_size')==entry['committed'] and
                info['mtime_ns']!=entry.get('source_mtime_ns')):
            raise TransferConflict("源文件长度未增但修改时间改变，可能原地改写: "+name)

    def verify_resume(self,name,entry,info):
        self.validate_info(name,entry,info)
        offset=entry['committed']
        for position,expected in ((0,entry.get('head','')),
                                  (offset-len(bytes.fromhex(entry.get('tail',''))),entry.get('tail',''))):
            if expected:
                header,data=self.peer.read(name,position,len(bytes.fromhex(expected)))
                self.validate_info(name,entry,header)
                self.read_bytes+=len(data)
                self.file_noatime=header.get('noatime')
                if data!=bytes.fromhex(expected):
                    raise TransferConflict("续传边界内容不一致，停止以防混合不同数据: "+name)
        self.verified.add(name)

    def commit(self,name,entry,data,info):
        path=self.local_path(name)
        offset=entry['committed']
        s=path.stat()
        if [s.st_dev,s.st_ino]!=entry['local_identity'] or s.st_size<offset:
            raise TransferConflict("本地接收文件被更改: "+name)
        with path.open('r+b') as stream:
            stream.truncate(offset)
            stream.seek(offset)
            stream.write(data);stream.flush();os.fsync(stream.fileno())
            end=offset+len(data)
            stream.seek(0);head=stream.read(min(64,end)).hex()
            stream.seek(max(0,end-64));tail=stream.read(min(64,end)).hex()
        previous=dict(entry)
        entry.update(committed=end,head=head,tail=tail,source_size=info['size'],source_mtime_ns=info['mtime_ns'])
        try:
            self.save()  # Publish only AFTER data fsync. Reader is capped at this watermark.
        except Exception:
            entry.clear();entry.update(previous)
            raise
        self.last_commit=time.time()

    def step(self):
        if time.monotonic()-self.last_scan>=self.config.scan_seconds:
            self.scan()
        candidates=self.todo[:1]
        if not candidates and self.active:
            candidates=[self.active[self.active_index%len(self.active)]]
            self.active_index+=1
        for name in candidates:
            entry=self.manifest['files'].get(name)
            if entry is None:
                info=self.peer.stat(name)
                entry=self.register(name,info)
            if name not in self.verified:
                info=self.peer.stat(name)
                self.verify_resume(name,entry,info)
            rate=self.config.max_mib_per_second*1024**2
            amount=self.config.chunk_kib*1024
            wait=self.next_read-time.monotonic()
            if wait>0:
                self.stop.wait(min(wait,self.config.poll_seconds))
                return False
            header,data=self.peer.read(name,entry['committed'],amount)
            self.last_check=time.time()
            self.validate_info(name,entry,header)
            self.file_noatime=header.get('noatime')
            self.remote_sizes[name]=header['size']
            self.current=name
            if not data:
                if header['size']>entry['committed']:
                    raise ConnectionError("远端未返回期望的新增数据")
                if name in self.todo:self.todo.remove(name)
                continue
            if len(data)>amount:
                raise TransferConflict("远端返回超出请求的数据块")
            self.commit(name,entry,data,header)
            if entry['committed']>=header['size'] and name in self.todo:self.todo.remove(name)
            self.read_bytes+=len(data)
            self.next_read=time.monotonic()+len(data)/rate
            return True
        return False

    def publish(self):
        committed=sum(e['committed'] for e in self.manifest['files'].values())
        backlog=sum(max(0,size-self.manifest['files'].get(n,{}).get('committed',0)) for n,size in self.remote_sizes.items())
        value={'status':self.status,'error':self.error,'host':self.config.host,'remote_dir':self.config.remote_dir,
               'pattern':self.config.pattern,'start_mode':self.config.start_mode,'file':self.current,
               'committed_bytes':committed,'backlog_bytes':backlog,'known_files':len(self.names),
               'received_files':len(self.manifest['files']),'connections':self.connections,
               'network_read_bytes':self.read_bytes,'max_mib_per_second':self.config.max_mib_per_second,
               'last_commit_host_s':self.last_commit,'last_remote_check_host_s':self.last_check,
               'source_free_bytes':self.source_free,'file_noatime':self.file_noatime,
               'directory_noatime':self.directory_noatime,'paused':self.paused.is_set(),
               'mirror_dir':str(self.directory)}
        with self.view_lock:
            self.cached=value

    def snapshot(self):
        with self.view_lock:
            return self.cached

    def start(self):
        self.thread.start()

    def pause(self):
        self.paused.set()

    def resume(self):
        self.paused.clear();self.resume_requested.set()

    def work(self):
        retry=2.
        try:
            while not self.stop.is_set():
                if self.resume_requested.is_set():
                    self.resume_requested.clear();self.error=None;self.status='starting'
                if self.paused.is_set() or self.status=='blocked':
                    if self.paused.is_set():self.status='paused'
                    self.publish();self.stop.wait(self.config.poll_seconds);continue
                try:
                    free=shutil.disk_usage(self.directory).free
                    if free<self.config.min_free_gib*1024**3+self.config.chunk_kib*1024:
                        self.status='disk_full';self.error='工控机接收盘低于预留空间，暂停拉取；树莓派原始数据不删除'
                        self.publish();self.stop.wait(self.config.poll_seconds);continue
                    if self.peer is None:
                        saved=self.db.execute('SELECT value FROM receive_state WHERE id=1').fetchone()
                        self.manifest=json.loads(saved[0])
                        self.recover()
                        self.status='connecting';self.publish()
                        self.peer=SSHReader(self.config,auth=self.auth)
                        self.peer.connect();self.connections+=1
                        self.verified.clear();self.last_scan=0
                    self.error=None;self.status='receiving'
                    changed=self.step()
                    retry=2.
                    if not changed:self.status='waiting'
                    self.publish()
                    if not changed:self.stop.wait(self.config.poll_seconds)
                except Exception as error:
                    if self.stop.is_set():break
                    self.error=str(error)
                    self.status='blocked' if isinstance(error,(TransferConflict,ValueError,sqlite3.DatabaseError)) else 'reconnecting'
                    if self.peer is not None:
                        self.peer.close();self.peer=None
                    self.publish()
                    if self.status!='blocked':
                        self.stop.wait(retry);retry=min(60,retry*2)
        finally:
            if self.peer is not None:self.peer.close();self.peer=None
            self.status='stopped';self.publish()

    def close(self):
        self.stop.set()
        if self.peer is not None:
            self.peer.close()
        if self.thread.ident is not None:self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("接收线程未结束；保留接收锁与上次提交水位")
        self.db.close()
        self.lock_file.close()
