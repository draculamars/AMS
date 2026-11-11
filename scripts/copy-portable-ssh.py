#!/usr/bin/env python3
"""Resumable, byte-verified transfer to a Python receiver on Windows/Mac/Linux.

Supply the complete remote Python command (including receiver path/root/action)
explicitly; this tool never guesses a machine, drive, or privilege level.
"""
import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path


def sha(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('--host',required=True)
    p.add_argument('--receiver-command',required=True);p.add_argument('--objects-only',action='store_true');a=p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.@:-]+',a.host) or a.host.startswith('-'):raise ValueError('Invalid SSH host')
    root=a.source.resolve();state=json.loads((root/'export-state.json').read_text(encoding='utf-8'))
    if not a.objects_only and (state['status']!='complete' or not (root/'portable.json').is_file()):raise ValueError('Source export is not published')
    files=[]
    for path in sorted((root/'archive' if a.objects_only else root).rglob('*')):
        if not path.is_file():continue
        if path.is_symlink():raise ValueError('Symlink in source export')
        name=path.relative_to(root).as_posix();parts=Path(name).parts
        if parts[0] in {'build','backups'} or '.incoming' in parts or 'expanded' in parts or path.name.startswith('.'):continue
        if name=='portable.json':name='portable.pending.json'
        digest=path.name if parts[0]=='archive' and re.fullmatch('[a-f0-9]{64}',path.name) else sha(path)
        files.append({'path':name,'source':str(path),'sha256':digest,'size':path.stat().st_size})
    ssh=['ssh','-oBatchMode=yes','-oStrictHostKeyChecking=yes','-oConnectTimeout=15','-oServerAliveInterval=15','-oServerAliveCountMax=4',a.host]
    request={'id':state['id'],'files':[{k:v for k,v in x.items() if k!='source'} for x in files]}
    r=subprocess.run(ssh+[a.receiver_command+' inventory'],input=(json.dumps(request)+'\n').encode(),capture_output=True)
    if r.returncode:raise RuntimeError(r.stderr.decode(errors='replace')[-2000:])
    missing=set(json.loads(r.stdout)['missing']);selected=[x for x in files if x['path'] in missing]
    print(json.dumps({'phase':'transfer','files':len(selected),'bytes':sum(x['size'] for x in selected)}),flush=True)
    with tempfile.TemporaryFile() as err:
        proc=subprocess.Popen(ssh+[a.receiver_command+' receive'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err)
        try:
            proc.stdin.write((json.dumps({'id':state['id']})+'\n').encode());done=0
            for i,item in enumerate(selected):
                proc.stdin.write((json.dumps({k:v for k,v in item.items() if k!='source'})+'\n').encode())
                with Path(item['source']).open('rb') as f:
                    for chunk in iter(lambda:f.read(4*1024**2),b''):proc.stdin.write(chunk)
                done+=item['size']
                if i%1000==0:print(json.dumps({'done':i+1,'total':len(selected),'bytes':done}),flush=True)
            proc.stdin.write(b'{"end":true}\n');proc.stdin.close();proc.stdin=None
            out,_=proc.communicate()
            if proc.returncode:err.seek(0);raise RuntimeError(err.read().decode(errors='replace')[-2000:])
            print(out.decode(),flush=True)
        finally:
            if proc.poll() is None:proc.terminate();proc.wait()


if __name__=='__main__':main()
