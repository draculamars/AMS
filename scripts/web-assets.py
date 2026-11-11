#!/usr/bin/env python3
"""Register and publish web assets. CLI does not execute discovered programs."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asset_manager import db,web_assets

p=argparse.ArgumentParser();p.add_argument('action',choices=['discover','publish','verify','import','record-verification']);p.add_argument('--project');p.add_argument('--bundle');p.add_argument('--manifest',type=Path);p.add_argument('--publication')
a=p.parse_args();db.init()
if a.action=='discover':result=web_assets.discover()
elif a.action=='verify':result=web_assets.verify(a.bundle)
elif a.action=='record-verification':result=web_assets.record_browser_verification(a.publication,json.loads(a.manifest.read_text()))
elif a.action=='publish':
    with db.connect() as c:ids=[r[0] for r in c.execute('SELECT DISTINCT project_id FROM mirror_files')]
    result=[web_assets.publish(pid) for pid in ([a.project] if a.project else ids)]
else:
    data=json.loads(a.manifest.read_text());pid=data['project_id']
    with db.connect() as c:
        for entry in data['entries']:
            path=web_assets.relative(entry['path']);ident=__import__('hashlib').sha256((pid+'\0'+path).encode()).hexdigest()[:24]
            c.execute('INSERT INTO web_entries VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,kind=excluded.kind,body=excluded.body,updated_at=excluded.updated_at',(ident,pid,path,entry['title'],entry.get('kind','interactive'),json.dumps(entry.get('body',{}),ensure_ascii=False),__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()))
    result=web_assets.publish(pid,data['files'],data.get('metadata'))
print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
