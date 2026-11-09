#!/usr/bin/env python3
"""Build the IPC tar.gz from an allowlist; never include raw data, state or credentials."""
import hashlib
import io
import json
from pathlib import Path
import tarfile


def build():
    online=Path(__file__).resolve().parent
    root=online.parent
    files=list(online.glob('*.py'))+list(online.glob('*.h'))+list(online.glob('*.cpp'))+list(online.glob('*.md'))
    files += [online/'start.sh',online/'requirements.txt',online/'config.example.json',online/'remote.example.json',online/'.gitignore']
    files += list((online/'static').rglob('*'))+list((online/'tests').glob('*.py'))
    files += [p for p in (online/'docs').iterdir() if p.suffix in ('.json','.md','.txt','.png')]
    files += [root/'README.md',root/'start.sh',root/'calculate_20260809/README.md',root/'calculate_20260809/analyze_charge.C']
    files=sorted(set(p for p in files if p.is_file() and '__pycache__' not in p.parts and p.name!='.DS_Store'))
    for path in files:
        if path.is_symlink():raise ValueError('Package does not follow symlinks: '+str(path))
    manifest={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    output=root/'seastar-online.tar.gz'
    with tarfile.open(output,'w:gz') as archive:
        for path in files:
            info=archive.gettarinfo(str(path),'seastar-online/'+str(path.relative_to(root)))
            info.uid=info.gid=0;info.uname=info.gname=''
            if path.name=='start.sh':info.mode=0o755
            with path.open('rb') as source:archive.addfile(info,source)
        payload=(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode()
        info=tarfile.TarInfo('seastar-online/SHA256SUMS.json');info.size=len(payload);info.mode=0o644
        archive.addfile(info,io.BytesIO(payload))
    with tarfile.open(output,'r:gz') as archive:
        for name,digest in manifest.items():
            assert hashlib.sha256(archive.extractfile('seastar-online/'+name).read()).hexdigest()==digest
        assert archive.getmember('seastar-online/start.sh').mode&0o111
        assert all('/state/' not in n and '/runtime/' not in n and not n.endswith('/remote.json') for n in archive.getnames())
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_name(output.name+'.sha256').write_text(digest+'  '+output.name+'\n')
    print(str(output));print('Files:',len(files)+1,'Bytes:',output.stat().st_size,'SHA256:',digest)
    return output


if __name__=='__main__':build()
