#!/usr/bin/env python3
"""Install a configurable per-user service on macOS or Linux; no sudo required."""
import argparse
import os
import platform
import plistlib
import re
import shlex
import subprocess
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('--app',type=Path,default=Path(__file__).resolve().parents[1])
p.add_argument('--root',type=Path,default=Path(os.environ.get('ASSET_VAULT',Path.home()/'AssetVault')))
p.add_argument('--name',default='ams-assets');p.add_argument('--port',type=int,default=8765)
p.add_argument('--host',default='127.0.0.1');p.add_argument('--allowed-hosts',default='localhost,127.0.0.1')
p.add_argument('--write-only',action='store_true')
a=p.parse_args();root=a.app.expanduser().resolve();vault=a.root.expanduser().resolve()
if not re.fullmatch('[a-zA-Z0-9_.-]+',a.name) or not 1<=a.port<=65535:raise SystemExit('Invalid service name or port')
for folder in ['projects','archive','catalog','backups']:(vault/folder).mkdir(parents=True,exist_ok=True)
python=root/'.venv/bin/python'
if not python.is_file():raise SystemExit('Run scripts/install.sh first')
args=[str(python),'-m','asset_manager','serve','--host',a.host,'--port',str(a.port)]
env={'ASSET_DATA_DIR':str(vault/'catalog'),'ASSET_BLOB_DIR':str(vault/'archive'),'ASSET_ALLOWED_HOSTS':a.allowed_hosts,
     'ASSET_COOKIE_NAME':a.name.replace('-','_')+'_session','PYTHONUNBUFFERED':'1','PATH':os.environ.get('PATH','/usr/bin:/bin')}
if platform.system()=='Darwin':
 label='local.'+a.name;path=Path.home()/'Library/LaunchAgents'/f'{label}.plist';path.parent.mkdir(parents=True,exist_ok=True)
 body={'Label':label,'ProgramArguments':args,'WorkingDirectory':str(root),'EnvironmentVariables':env,'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,
       'StandardOutPath':str(vault/'catalog/server.log'),'StandardErrorPath':str(vault/'catalog/server-error.log')}
 path.write_bytes(plistlib.dumps(body));path.chmod(0o600)
 if not a.write_only:
  domain=f'gui/{os.getuid()}';subprocess.run(['launchctl','bootout',domain,str(path)],capture_output=True)
  subprocess.run(['launchctl','bootstrap',domain,str(path)],check=True)
 print(path)
 print('macOS: if project folders are protected by privacy controls, grant the service runtime access in System Settings, or use scripts/background.py from an authorised session.')
elif platform.system()=='Linux':
 # systemd quotes differ from shell quotes; reject line breaks and escape specifiers.
 def quote(s):return '"'+str(s).replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('\n','')+'"'
 path=Path.home()/'.config/systemd/user'/f'{a.name}.service';path.parent.mkdir(parents=True,exist_ok=True)
 body='[Unit]\nDescription=AMS project and asset steward\nAfter=network.target\n[Service]\nType=simple\nWorkingDirectory='+quote(root)+'\n'
 body+=''.join('Environment='+quote(k+'='+v)+'\n' for k,v in env.items())
 body+='ExecStart='+' '.join(quote(s) for s in args)+'\nRestart=on-failure\nRestartSec=5\nNoNewPrivileges=true\nUMask=0077\n[Install]\nWantedBy=default.target\n'
 path.write_text(body);path.chmod(0o600)
 if not a.write_only:
  subprocess.run(['systemctl','--user','daemon-reload'],check=True)
  subprocess.run(['systemctl','--user','enable','--now',path.name],check=True)
 print(path);print('For startup without login: loginctl enable-linger "$USER" (subject to host policy).')
else:raise SystemExit('Service installer supports macOS and Linux')
print(f'AMS: http://{a.host}:{a.port}')
