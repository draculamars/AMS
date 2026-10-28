#!/usr/bin/env python3
"""Synthetic offline-runtime acceptance. Contains no user projects or credentials."""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP=Path(__file__).resolve().parents[1]


def main(destination):
    target=destination.resolve()
    source=target.parent/(target.name+'-fixture')
    source.mkdir()
    project=source/'project';project.mkdir();(project/'README.md').write_text('# Portable runtime check\n')
    (project/'part.stl').write_text('solid part\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid part\n')
    subprocess.run(['git','init','-b','main',str(project)],check=True,capture_output=True)
    subprocess.run(['git','-C',str(project),'add','.'],check=True)
    subprocess.run(['git','-C',str(project),'-c','user.name=Atlas fixture','-c','user.email=fixture@example.invalid','commit','-m','Portable fixture'],check=True,capture_output=True)
    env={**os.environ,'ASSET_DATA_DIR':str(source/'catalog'),'ASSET_BLOB_DIR':str(source/'archive'),'ASSET_WATCH_ENABLED':'0'}
    manifest=source/'projects.json';manifest.write_text(json.dumps({'projects':[{'projectId':'portable-fixture','name':'Portable fixture','path':str(project)}]}))
    subprocess.run([sys.executable,'-m','asset_manager','import',str(manifest)],cwd=APP,env=env,check=True)
    subprocess.run([sys.executable,str(APP/'scripts/export-portable.py'),str(target),'--source-catalog',str(source/'catalog'),'--source-archive',str(source/'archive')],check=True)
    system=platform.system();arch=platform.machine();key=system+'-'+arch
    args=[sys.executable,str(APP/'scripts/build-portable-runtimes.py'),str(target),'--targets',key]
    if system=='Darwin':args+=['--mac-git',subprocess.check_output(['xcrun','--find','git'],text=True).strip()]
    subprocess.run(args,check=True)
    subprocess.run([sys.executable,str(APP/'scripts/package-portable.py'),str(target)],check=True)
    shutil.rmtree(source)  # Acceptance cannot fall back to original files.
    moved=target.parent/(target.name+' moved 空间');target.rename(moved);target=moved
    env={**os.environ,'ATLAS_NO_BROWSER':'1'}
    proc=subprocess.Popen(['sh',str(target/'start.sh')],env=env)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        for _ in range(900):
            if proc.poll() is not None:raise RuntimeError('Portable runtime exited early')
            status=target/'session.json'
            if status.is_file():
                state=json.loads(status.read_text());url=f"http://127.0.0.1:{state['port']}"
                try:
                    with opener.open(url+'/api/health',timeout=2) as r:
                        if json.load(r).get('portable_session')==state['nonce']:break
                except OSError:pass
            time.sleep(.2)
        else:raise RuntimeError('Runtime startup timeout')
        with opener.open(url+'/api/assets') as r:assets=json.load(r)['items']
        for asset in assets:
            with opener.open(url+f"/api/assets/{asset['id']}/file") as r:
                assert len(r.read())==asset['size']
        with opener.open(url+'/api/engineering/portable-fixture/tree') as r:
            assert json.load(r)['total']==2
        with opener.open(url+'/api/engineering/portable-fixture') as r:
            repository=json.load(r)['repositories'][0]['repository_id']
        with opener.open(url+'/api/repositories/'+repository+'/commits') as r:
            assert json.load(r)['items'][0]['subject']=='Portable fixture'
        subprocess.run(['sh',str(target/'stop.sh')],env=env,check=True,timeout=90)
        proc.wait(timeout=30)
        subprocess.run(['sh',str(target/'start.sh'),'verify'],env=env,check=True)
        (target/'reports/runtime-smoke.json').write_text(json.dumps({'platform':key,'status':'passed','relocated':True,'original_removed':True,'files':len(assets)}))
    finally:
        if proc.poll() is None:proc.terminate();proc.wait(timeout=60)
    # Distribute offline runtime/application only, never the build cache.
    shutil.rmtree(target/'build')
    if (target/'runtimes/expanded').exists():shutil.rmtree(target/'runtimes/expanded')
    print(json.dumps({'passed':key,'artifact':str(target)}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('destination',type=Path)
    main(parser.parse_args().destination)
