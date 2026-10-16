#!/usr/bin/env python3
"""Resumable streaming archive upload over an existing verified SSH connection."""
import argparse
import json
import shlex
import subprocess
import tarfile
import time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--host',required=True);p.add_argument('--root',required=True);p.add_argument('manifest',type=Path);args=p.parse_args()
manifest=json.loads(args.manifest.read_text());objects=manifest['objects'];root=args.root.rstrip('/')
ssh=['ssh','-C','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=4',args.host]
receiver=root+'/app/scripts/remote-receiver.py';blobs=root+'/archive'
remote=lambda mode:'python3 '+shlex.quote(receiver)+' '+mode+' '+shlex.quote(blobs)
inventory=subprocess.run(ssh+[remote('inventory')],capture_output=True,text=True,check=True)
existing={parts[0]:int(parts[1]) for line in inventory.stdout.splitlines() if len(parts:=line.split())==2}
pending=[o for o in objects if existing.get(o['sha256'])!=o['size']]
progress_file=args.manifest.parent/'remote-transfer-progress.json'
report={'total':len(pending),'already_present':len(objects)-len(pending),'already_bytes':sum(o['size'] for o in objects if existing.get(o['sha256'])==o['size']),'bytes_total':sum(o['size'] for o in pending),'sent':0,'bytes_sent':0,'skipped_changed':[],'started_at':time.time()}
progress_file.write_text(json.dumps(report));last_progress=time.monotonic()
log=args.manifest.parent/'remote-transfer-receiver.log'
with log.open('w') as output:
    proc=subprocess.Popen(ssh+[remote('receive')],stdin=subprocess.PIPE,stdout=output,stderr=output)
    try:
        with tarfile.open(fileobj=proc.stdin,mode='w|',bufsize=1024*1024) as archive:
            for obj in pending:
                path=Path(obj['source_path'])
                try:
                    before=path.stat()
                    if before.st_size!=obj['size'] or before.st_mtime_ns!=obj['mtime_ns']:
                        report['skipped_changed'].append({'sha256':obj['sha256'],'path':str(path),'error':'source_changed'});continue
                    stream=path.open('rb')
                except OSError as e:
                    report['skipped_changed'].append({'sha256':obj['sha256'],'path':str(path),'error':str(e)})
                    continue
                # Any failure after writing a tar header invalidates the stream.
                # Stop; a retry inventories and resumes verified remote objects.
                info=tarfile.TarInfo(obj['sha256'][:2]+'/'+obj['sha256']);info.size=obj['size'];info.mode=0o600
                with stream:archive.addfile(info,stream)
                report['sent']+=1;report['bytes_sent']+=obj['size']
                if report['sent']%500==0 or time.monotonic()-last_progress>3:
                    last_progress=time.monotonic()
                    temp=progress_file.with_suffix('.tmp');temp.write_text(json.dumps(report));temp.replace(progress_file)
                    print(json.dumps({k:v for k,v in report.items() if k!='skipped_changed'}),flush=True)
        proc.stdin.close();rc=proc.wait()
    except BaseException as e:
        proc.terminate();proc.wait()
        report.update(complete=False,returncode=proc.returncode,error=str(e),finished_at=time.time())
        progress_file.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        raise
report.update(complete=rc==0,returncode=rc,finished_at=time.time());progress_file.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False));raise SystemExit(rc)
