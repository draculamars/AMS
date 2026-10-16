#!/usr/bin/env python3
"""Package application source and vendored viewer assets, preserving executable modes."""
import hashlib
import json
import sys
import subprocess
import zipfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asset_manager import __version__
root=Path(__file__).resolve().parents[1]
target=Path(sys.argv[1]).expanduser() if len(sys.argv)>1 else Path.home()/'AssetVault'/'backups'/f'atlas-asset-manager-{__version__}.zip'
target.parent.mkdir(parents=True,exist_ok=True)
folders={'asset_manager','frontend','scripts','tests','deploy','docs','.github'}
files={'.gitignore','.dockerignore','README.md','CHANGELOG.md','Dockerfile','compose.yaml','requirements.txt','requirements.lock','requirements-dev.txt','package.json','package-lock.json'}
selected=[]
for path in root.rglob('*'):
    rel=path.relative_to(root)
    if path.is_symlink() or not path.is_file() or '__pycache__' in rel.parts:continue
    if (len(rel.parts)==1 and rel.name in files) or rel.parts[0] in folders:selected.append((path,rel.as_posix()))
revision=subprocess.run(['git','-C',str(root),'rev-parse','HEAD'],capture_output=True,text=True)
manifest={'version':__version__,'source_commit':revision.stdout.strip() if revision.returncode==0 else None,'files':{}}
with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for path,rel in sorted(selected):
        manifest['files'][rel]=hashlib.sha256(path.read_bytes()).hexdigest();z.write(path,rel)
    z.writestr('RELEASE-MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2))
with zipfile.ZipFile(target) as z:
    for rel,sha in manifest['files'].items():assert hashlib.sha256(z.read(rel)).hexdigest()==sha
    assert z.getinfo('scripts/run.sh').external_attr>>16 & 0o111
print(json.dumps({'path':str(target),'version':__version__,'files':len(selected),'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}))
