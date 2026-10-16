#!/usr/bin/env python3
"""Start/stop Atlas from an already authorised interactive session."""
import argparse
import os
import signal
import subprocess
from pathlib import Path
root=Path(__file__).resolve().parents[1]
data=Path(os.environ.get('ASSET_DATA_DIR',str(Path.home()/'AssetVault/catalog'))).expanduser()
data.mkdir(parents=True,exist_ok=True)
pidfile=data/'server.pid'
p=argparse.ArgumentParser();p.add_argument('action',choices=['start','stop','status']);args=p.parse_args()
pid=int(pidfile.read_text()) if pidfile.exists() else None
running=False
if pid:
    cmd=subprocess.run(['ps','-p',str(pid),'-o','args='],capture_output=True,text=True).stdout
    running=str(root/'.venv/bin/python') in cmd and 'asset_manager serve' in cmd
if args.action=='status':print(f'PID {pid} running' if running else 'not running')
elif args.action=='stop':
    if running:os.kill(pid,signal.SIGTERM);print('已请求停止后台服务')
    pidfile.unlink(missing_ok=True)
elif running:print(f'服务已运行，PID {pid}')
else:
    env={**os.environ,'ASSET_DATA_DIR':str(data),'ASSET_BLOB_DIR':os.environ.get('ASSET_BLOB_DIR',str(data.parent/'archive')),'PYTHONUNBUFFERED':'1'}
    with (data/'server.log').open('ab') as out,(data/'server-error.log').open('ab') as err:
        child=subprocess.Popen([str(root/'.venv/bin/python'),'-m','asset_manager','serve','--host',os.environ.get('ASSET_HOST','127.0.0.1'),'--port',os.environ.get('ASSET_PORT','8765')],cwd=root,env=env,stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True)
    pidfile.write_text(str(child.pid));print(f'服务已启动，PID {child.pid}，http://127.0.0.1:{env.get("ASSET_PORT","8765")}')
