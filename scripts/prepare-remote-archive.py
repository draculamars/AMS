#!/usr/bin/env python3
"""Hash indexed source files without staging another copy on the source disk."""
import concurrent.futures
import hashlib
import json
import os
import time
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asset_manager import db,core

def digest(row):
    try:
        path=core.safe_path(row['project_path'],row['relpath']) if not row.get('blob_sha') else db.blob_root()/row['blob_sha'][:2]/row['blob_sha']; before=path.stat()
        h=hashlib.sha256()
        with path.open('rb') as f:
            for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
        after=path.stat()
        if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):raise ValueError('file_changed_during_hash')
        return {**row,'sha256':h.hexdigest(),'source_path':str(path),'size':after.st_size,'mtime':after.st_mtime,'mtime_ns':after.st_mtime_ns}
    except Exception as e:return {'id':row['id'],'path':row['relpath'],'error':str(e)}

def main():
    db.init()
    with db.connect() as con:
        rows=[dict(r) for r in con.execute("SELECT a.*,p.path AS project_path,(SELECT sha256 FROM versions v WHERE v.asset_id=a.id AND reason IN ('upload','update') ORDER BY id DESC LIMIT 1) AS blob_sha FROM assets a JOIN projects p ON p.id=a.project_id WHERE a.present=1 ORDER BY a.id")]
        projects=[dict(r) for r in con.execute('SELECT * FROM projects')]
    start=time.time(); assets=[]; errors=[]
    report=db.DATA/'remote-archive-preparation.json'
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for i,r in enumerate(pool.map(digest,rows)):
            (errors if 'error' in r else assets).append(r)
            if i%2000==0:
                progress={'done':i,'total':len(rows),'errors':len(errors),'elapsed':round(time.time()-start)}
                report.write_text(json.dumps(progress));print(json.dumps(progress),flush=True)
    unique={}
    for a in assets:unique.setdefault(a['sha256'],{'sha256':a['sha256'],'source_path':a['source_path'],'size':a['size'],'mtime_ns':a['mtime_ns']})
    result={'schema':1,'created_at':core.now(),'projects':projects,'assets':assets,'objects':list(unique.values()),'errors':errors,
            'source_bytes':sum(a['size'] for a in assets),'unique_bytes':sum(a['size'] for a in unique.values())}
    target=db.DATA/'remote-archive-manifest.json'
    temp=target.with_suffix('.tmp');temp.write_text(json.dumps(result,ensure_ascii=False));os.replace(temp,target)
    report.write_text(json.dumps({'done':len(rows),'total':len(rows),'errors':len(errors),'elapsed':round(time.time()-start),'complete':True,'unique_objects':len(unique),'unique_bytes':result['unique_bytes']}))
    print(json.dumps({k:v for k,v in result.items() if k not in {'projects','assets','objects'}},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
