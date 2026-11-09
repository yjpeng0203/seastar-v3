"""Minute-level wall-clock hint; never changes acquisition times or aligns strings."""
from functools import lru_cache
from pathlib import Path
import csv
import math
import statistics
import time


@lru_cache(maxsize=16)
def anchors(path,size,mtime):
    offsets=[];previous=None
    with Path(path).open() as stream:
        for row in csv.DictReader(stream):
            try:
                if row.get('valid','1')!='1':continue
                fpga=int(row['fpga_timestamp_seconds'])+int(row['fpga_timestamp_nanoseconds'])/1e9
                if previous is not None and fpga<previous-120:return None
                previous=fpga
                value=int(row['timestamp_ms'])/1000-fpga
                if math.isfinite(value):offsets.append(value)
            except (ValueError,KeyError):continue
    if not offsets:return None
    center=statistics.median(offsets);accepted=[v for v in offsets if abs(v-center)<=2]
    return dict(offset=statistics.median(accepted),anchors=len(accepted),rejected=len(offsets)-len(accepted))


@lru_cache(maxsize=16)
def minute_anchors(path,minute):
    st=Path(path).stat()
    return anchors(path,st.st_size,st.st_mtime_ns)


def reference(data_dir,source,origin_ns):
    if not source or origin_ns is None:return None
    parent=(Path(data_dir)/source['files'][0]['name']).parent.parent
    board=source.get('board_id')
    for path in sorted((parent/'csv').glob('*.csv')):
        if board is not None and f'fee_{board}_' not in path.name:continue
        result=minute_anchors(str(path),int(time.monotonic()/60))
        if result:return dict(result,origin_unix_ms=round((int(origin_ns)/1e9+result['offset'])*1000),precision='minute',label='北京时间粗略参考（CSV 映射，非同步时间）')
    return None
