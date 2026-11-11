#!/usr/bin/env python3
"""Platform-neutral receiver for an explicitly selected portable SSD directory.

The stream is JSON headers followed by exact file bytes. Only an unfinished,
identified transfer is writable; portable.json is activated separately after QA.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath


def safe(root, name):
    p = PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or not p.parts or '\\' in name or ':' in name or name != p.as_posix():
        raise ValueError('Invalid transfer path')
    if p.parts[0] not in {'archive', 'datasets', 'app', 'runtimes', 'reports', 'launcher.py', 'start.sh', 'stop.sh',
                         'start.cmd', 'Start Atlas.command', 'Stop Atlas.command', 'Start Atlas.cmd', 'Stop Atlas.cmd',
                         'Verify Atlas.cmd', 'Restore Atlas.cmd', 'Restore Atlas.command', '使用说明.md', 'export-state.json', 'portable.pending.json'}:
        raise ValueError('Transfer path is outside portable content')
    target = root / name
    if not target.resolve().is_relative_to(root) or target.is_symlink(): raise ValueError('Transfer escapes destination')
    return target


def digest(p):
    with p.open('rb') as f:return hashlib.file_digest(f, 'sha256').hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);p.add_argument('action',choices=['inventory','receive']);a=p.parse_args()
    root=a.root.resolve();root.mkdir(parents=True,exist_ok=True)
    if (root/'portable.json').exists() or (root/'session.json').exists():raise ValueError('Published or active library cannot be overwritten')
    inp=sys.stdin.buffer
    request=json.loads(inp.readline(128*1024**2));ident=request['id']
    if not re.fullmatch('[a-f0-9]{32}',ident):raise ValueError('Invalid transfer identity')
    marker=root/'transfer-state.json'
    if marker.exists():
        if json.loads(marker.read_text(encoding='utf-8'))['id']!=ident:raise ValueError('Different library already owns this destination')
    else:
        # A preflight-only build directory is allowed; unrelated files are not.
        if any(x.name!='build' for x in root.iterdir()):raise ValueError('Destination is not empty or marked')
        marker.write_text(json.dumps({'id':ident,'status':'receiving','encrypted':False}),encoding='utf-8')
    if a.action=='inventory':
        missing=[]
        for item in request['files']:
            path=safe(root,item['path'])
            if not path.is_file() or path.stat().st_size!=item['size'] or digest(path)!=item['sha256']:missing.append(item['path'])
        print(json.dumps({'missing':missing}));return
    count=0;total=0
    while True:
        line=inp.readline(1024**2)
        if not line:raise ValueError('Incomplete transfer stream')
        item=json.loads(line)
        if item.get('end'):break
        sha=item['sha256'];size=item['size']
        if not re.fullmatch('[a-f0-9]{64}',sha) or not isinstance(size,int) or size<0:raise ValueError('Invalid file header')
        target=safe(root,item['path']);target.parent.mkdir(parents=True,exist_ok=True)
        temp=target.with_name(target.name+'.atlas-incoming')
        h=hashlib.sha256();remaining=size
        try:
            with temp.open('wb') as out:
                while remaining:
                    chunk=inp.read(min(4*1024**2,remaining))
                    if not chunk:raise ValueError('Truncated file')
                    out.write(chunk);h.update(chunk);remaining-=len(chunk)
                # Close each file normally. Per-file device flushes make USB
                # exFAT transfers prohibitively slow; the deployment performs a
                # volume flush and full verification before activation. Safe OS
                # eject is still required before physically unplugging the SSD.
                out.flush()
            if h.hexdigest()!=sha:raise ValueError('Transfer hash mismatch: '+item['path'])
            os.replace(temp,target);count+=1;total+=size
        finally:temp.unlink(missing_ok=True)
    print(json.dumps({'status':'received','files':count,'bytes':total,'encrypted':False,'activation':'pending full verification and volume flush'}),flush=True)


if __name__=='__main__':main()
