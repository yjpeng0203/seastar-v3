"""Small process-tree RSS diagnostics; no optional monitoring dependency."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

SOFT_BYTES = 4 * 1024**3
HARD_BYTES = 6 * 1024**3


def process_tree(pid=None):
    pid = pid or os.getpid()
    rows = []
    if sys.platform.startswith('linux'):
        pending = [pid]; seen = set()
        while pending:
            current = pending.pop()
            if current in seen:continue
            seen.add(current)
            try:
                fields = {}
                for line in Path(f'/proc/{current}/status').read_text().splitlines():
                    key,_,value=line.partition(':');fields[key]=value.strip()
                rows.append(dict(pid=current,ppid=int(fields['PPid']),role=fields['Name'],rss_bytes=int(fields.get('VmRSS','0 kB').split()[0])*1024))
                pending.extend(int(v) for v in Path(f'/proc/{current}/task/{current}/children').read_text().split())
            except (OSError,ValueError,KeyError):pass
    else:
        try:
            values=subprocess.check_output(['ps','-axo','pid=,ppid=,rss=,comm='],text=True,timeout=2)
            all_rows=[]
            for line in values.splitlines():
                values=line.split(None,3)
                if len(values)==4:all_rows.append(dict(pid=int(values[0]),ppid=int(values[1]),rss_bytes=int(values[2])*1024,role=Path(values[3]).name))
            selected={pid}
            while True:
                children={r['pid'] for r in all_rows if r['ppid'] in selected}
                if children<=selected:break
                selected|=children
            rows=[r for r in all_rows if r['pid'] in selected]
        except (OSError,ValueError,subprocess.SubprocessError):pass
    return rows


def snapshot(pid=None):
    rows=process_tree(pid)
    value=dict(processes=rows,rss_bytes=sum(r['rss_bytes'] for r in rows),soft_bytes=SOFT_BYTES,protection_bytes=HARD_BYTES,checked_unix_s=time.time())
    if sys.platform.startswith('linux'):
        try:
            memory=dict(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())
            value['available_bytes']=int(memory['MemAvailable'].split()[0])*1024
            value['swap_used_bytes']=(int(memory['SwapTotal'].split()[0])-int(memory['SwapFree'].split()[0]))*1024
            vm=dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines())
            value.update(swap_in_pages=int(vm['pswpin']),swap_out_pages=int(vm['pswpout']),load=list(os.getloadavg()))
        except (OSError,ValueError,KeyError):pass
    return value


def role(name):
    if sys.platform.startswith('linux'):
        import ctypes
        ctypes.CDLL(None).prctl(15,name.encode()[:15],0,0,0)


def record(path,event,**fields):
    # One writer per process/task log; keep just two bounded log generations.
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists() and path.stat().st_size>1024*1024:
        path.replace(path.with_suffix('.previous.jsonl'))
    value=dict(time=time.time(),writer_pid=os.getpid(),ppid=os.getppid(),event=event)
    value.update(fields)
    with path.open('a') as stream:
        stream.write(json.dumps(value,ensure_ascii=False)+'\n')
