#!/usr/bin/env python3
"""Refresh files changed during transfer and the explicitly selected source project."""
import argparse
import importlib.util
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asset_manager import db,core
spec=importlib.util.spec_from_file_location('prepare_archive',Path(__file__).with_name('prepare-remote-archive.py'))
prepare=importlib.util.module_from_spec(spec);spec.loader.exec_module(prepare)
p=argparse.ArgumentParser();p.add_argument('manifest',type=Path);p.add_argument('--progress',type=Path,required=True);p.add_argument('--refresh-project',required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
data=json.loads(args.manifest.read_text());progress=json.loads(args.progress.read_text())
changed={r['sha256'] for r in progress.get('skipped_changed',[])}
core.scan_project(args.refresh_project)
with db.connect() as con:
    rows=[dict(r) for r in con.execute("SELECT a.*,p.path AS project_path,(SELECT sha256 FROM versions v WHERE v.asset_id=a.id AND reason IN ('upload','update') ORDER BY id DESC LIMIT 1) AS blob_sha FROM assets a JOIN projects p ON p.id=a.project_id WHERE a.present=1 AND a.project_id=?",(args.refresh_project,))]
    project=dict(con.execute('SELECT * FROM projects WHERE id=?',(args.refresh_project,)).fetchone())
assets=[];errors=[]
for a in data['assets']:
    if a['project_id']==args.refresh_project:continue
    if a['sha256'] in changed:
        a=prepare.digest(a)
        if 'error' in a:errors.append(a);continue
        a['captured_at']=core.now()
    assets.append(a)
for row in rows:
    a=prepare.digest(row)
    if 'error' in a:errors.append(a);continue
    a['captured_at']=core.now();assets.append(a)
objects={}
for a in assets:objects.setdefault(a['sha256'],{k:a[k] for k in ['sha256','source_path','size','mtime_ns']})
data.update(assets=assets,objects=list(objects.values()),errors=data.get('errors',[])+errors,source_bytes=sum(a['size'] for a in assets),unique_bytes=sum(a['size'] for a in objects.values()),finalized_at=core.now())
data['projects']=[project if x['id']==project['id'] else x for x in data['projects']]
args.output.write_text(json.dumps(data,ensure_ascii=False))
print(json.dumps({'assets':len(assets),'objects':len(objects),'errors':len(errors),'unique_bytes':data['unique_bytes']},ensure_ascii=False))
