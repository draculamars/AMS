#!/usr/bin/env python3
import argparse,json,shlex,subprocess
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--host',required=True);p.add_argument('--root',required=True);p.add_argument('progress',type=Path);a=p.parse_args()
d=json.loads(a.progress.read_text());done=d['sent']+d['already_present'];total=d['total']+d['already_present']
code="import sqlite3,json,sys; d=json.load(sys.stdin); c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE jobs SET done=?,total=?,detail=? WHERE id='initial-remote-archive'\",(d['done'],d['total'],d['detail'])); c.commit()"
cmd='python3 -c '+shlex.quote(code)+' '+shlex.quote(a.root+'/catalog/catalog.sqlite3')
payload={'done':done,'total':total,'detail':f"已传输 {(d['bytes_sent']+d.get('already_bytes',0))/1024**3:.1f} / {(d['bytes_total']+d.get('already_bytes',0))/1024**3:.1f} GiB；接收端逐对象校验"}
subprocess.run(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',a.host,cmd],input=json.dumps(payload).encode(),check=True)
print(json.dumps(payload,ensure_ascii=False))
