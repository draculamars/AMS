#!/usr/bin/env python3
"""Verify and install a source release into an existing app, preserving runtime data.
Stop the service first, then run this on the target host. A previous-source ZIP is
saved before any writes. Runtime directories and settings are never packaged.
"""
import argparse
import hashlib
import json
import os
import tempfile
import zipfile
from datetime import datetime,timezone
from pathlib import Path,PurePosixPath

p=argparse.ArgumentParser();p.add_argument('release',type=Path);p.add_argument('--root',type=Path,required=True);a=p.parse_args()
vault=a.root.expanduser().resolve();app=vault/'app';backup=vault/'backups';backup.mkdir(parents=True,exist_ok=True)
with zipfile.ZipFile(a.release) as archive:
 manifest=json.loads(archive.read('RELEASE-MANIFEST.json'));names=manifest['files']
 for name,sha in names.items():
  rel=PurePosixPath(name)
  if rel.is_absolute() or '..' in rel.parts or not rel.parts or rel.parts[0] in {'.git','.venv','data','node_modules'}:raise SystemExit('Unsafe release path: '+name)
  if hashlib.sha256(archive.read(name)).hexdigest()!=sha:raise SystemExit('Release hash mismatch: '+name)
 previous=app/'RELEASE-MANIFEST.json';old=json.loads(previous.read_text())['files'] if previous.is_file() else {}
 stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
 snapshot=backup/f'ams-before-{stamp}.zip';saved={}
 app.mkdir(parents=True,exist_ok=True)
 with zipfile.ZipFile(snapshot,'w',zipfile.ZIP_DEFLATED) as out:
  for name in set(names)|set(old):
   path=app/name
   if path.is_file() and not path.is_symlink():saved[name]=hashlib.sha256(path.read_bytes()).hexdigest();out.write(path,name)
  out.writestr('RELEASE-MANIFEST.json',json.dumps({'version':'rollback-'+stamp,'files':saved}))
 for name in names:
  dest=app/name
  if any(p.is_symlink() for p in [dest,*dest.parents] if p!=vault.parent):raise SystemExit('Symlink in install destination: '+name)
  dest.parent.mkdir(parents=True,exist_ok=True)
  fd,tmp=tempfile.mkstemp(dir=dest.parent)
  try:
   with os.fdopen(fd,'wb') as f:f.write(archive.read(name));f.flush();os.fsync(f.fileno())
   os.chmod(tmp,0o755 if archive.getinfo(name).external_attr>>16 & 0o111 else 0o644);os.replace(tmp,dest)
  finally:
   if os.path.exists(tmp):os.unlink(tmp)
 for name in set(old)-set(names):
  rel=PurePosixPath(name)
  if not rel.is_absolute() and '..' not in rel.parts and rel.parts[0] not in {'.venv','.git','data','node_modules'}:(app/name).unlink(missing_ok=True)
 previous.write_text(json.dumps(manifest,indent=2))
print(json.dumps({'version':manifest['version'],'app':str(app),'files':len(names),'rollback':str(snapshot)}))
print('Install locked dependencies if needed, restart the service, and check /api/node and /api/workbench.')
