#!/usr/bin/env python3
"""Register successfully received objects and preserve original project provenance."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asset_manager import db,core
p=argparse.ArgumentParser();p.add_argument('manifest',type=Path);p.add_argument('--verify',action='store_true');p.add_argument('--partial',action='store_true');args=p.parse_args()
db.init();data=json.loads(args.manifest.read_text());available={};failures=[]
for obj in data['objects']:
    sha=obj['sha256'];path=db.blob_root()/sha[:2]/sha
    if not path.is_file() or path.stat().st_size!=obj['size']:
        failures.append({'sha256':sha,'error':'missing_or_wrong_size'});continue
    if args.verify:
        h=hashlib.sha256()
        with path.open('rb') as f:
            for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
        if h.hexdigest()!=sha:failures.append({'sha256':sha,'error':'checksum_mismatch'});continue
    available[sha]=obj['size']
fields=['project_id','relpath','name','ext','kind','size','mtime','git_state','repo_root','tags','note','managed']
with db.connect() as con:
    for pr in data['projects']:
        con.execute('INSERT INTO projects(id,name,path,host,git_json,scan_at,scan_error,excluded_json) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,git_json=excluded.git_json,scan_at=excluded.scan_at,excluded_json=excluded.excluded_json',
        (pr['id'],pr['name'],pr['path'],'synced-source',pr['git_json'],pr['scan_at'],'原目录位于来源节点；当前节点提供归档版本',pr['excluded_json']))
    registered=0
    for a in data['assets']:
        if a['sha256'] not in available:continue
        existing=con.execute('SELECT id FROM assets WHERE project_id=? AND relpath=?',(a['project_id'],a['relpath'])).fetchone()
        if existing:
            target_id=existing['id']
            con.execute('UPDATE assets SET size=?,mtime=?,git_state=?,repo_root=?,present=0 WHERE id=?',(a['size'],a['mtime'],a['git_state'],a['repo_root'],target_id))
        else:
            values=[a.get(f) for f in fields]
            cursor=con.execute('INSERT INTO assets('+','.join(fields)+',present) VALUES('+','.join('?' for _ in fields)+',0)',values)
            target_id=cursor.lastrowid
        con.execute("INSERT OR IGNORE INTO versions(asset_id,sha256,size,created_at,reason) VALUES(?,?,?,?, 'snapshot')",(target_id,a['sha256'],a['size'],a.get('captured_at',data['created_at'])))
        registered+=1
    db.audit(con,'remote_archive_partial' if args.partial else 'remote_archive_import',{'registered':registered,'objects':len(available),'bytes':sum(available.values()),'failed_objects':len(failures),'source':'Mac Codex projects'})
result={'registered_assets':registered,'objects':len(available),'bytes':sum(available.values()),'failures':failures,'source_errors':data['errors'],'verified':args.verify,'state':'receiving' if args.partial else ('complete' if not failures and not data['errors'] else 'incomplete')}
(db.DATA/('remote-archive-partial.json' if args.partial else 'remote-archive-result.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in result.items() if k not in {'failures','source_errors'}},ensure_ascii=False))
