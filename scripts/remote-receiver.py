#!/usr/bin/env python3
"""Receive a bounded stream of SHA-256 objects; never extract arbitrary tar paths."""
import argparse
import hashlib
import json
import os
import re
import sys
import tarfile
import tempfile
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('mode',choices=['receive','inventory']);p.add_argument('root',type=Path);args=p.parse_args()
root=args.root.resolve();root.mkdir(parents=True,exist_ok=True)
if args.mode=='inventory':
    for folder in root.iterdir():
        if folder.is_dir() and re.fullmatch('[0-9a-f]{2}',folder.name):
            for f in folder.iterdir():
                if f.is_file() and re.fullmatch('[0-9a-f]{64}',f.name):print(f.name,f.stat().st_size)
    sys.exit(0)
incoming=root/'.incoming';incoming.mkdir(exist_ok=True)
result={'received':0,'bytes':0,'errors':[]}
with tarfile.open(fileobj=sys.stdin.buffer,mode='r|') as archive:
    for member in archive:
        if not member.isfile() or not re.fullmatch(r'[0-9a-f]{2}/[0-9a-f]{64}',member.name):
            raise SystemExit('Invalid object path')
        folder,sha=member.name.split('/')
        if sha[:2]!=folder:raise SystemExit('Invalid object prefix')
        fd,tmp=tempfile.mkstemp(dir=incoming)
        try:
            h=hashlib.sha256();size=0
            with os.fdopen(fd,'wb') as out,archive.extractfile(member) as src:
                for chunk in iter(lambda:src.read(1024*1024),b''):
                    h.update(chunk);out.write(chunk);size+=len(chunk)
                out.flush();os.fsync(out.fileno())
            if h.hexdigest()!=sha or size!=member.size:
                result['errors'].append({'sha256':sha,'error':'checksum_mismatch'});continue
            dest=root/folder/sha;dest.parent.mkdir(exist_ok=True);os.replace(tmp,dest)
            result['received']+=1;result['bytes']+=size
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
print(json.dumps(result),flush=True)
sys.exit(1 if result['errors'] else 0)
