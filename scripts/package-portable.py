#!/usr/bin/env python3
"""Install launchers and a checked source ZIP into a marked portable library.

This deliberately does not publish a dataset or overwrite existing user data.
Run after runtime construction and export, with the portable service stopped.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def package(target):
    if not (target/'export-state.json').is_file():
        raise ValueError('Destination is not a marked Atlas portable export')
    if (target/'session.json').exists():
        raise ValueError('Stop the portable service before upgrading the application')
    app=target/'app';app.mkdir(exist_ok=True)
    pending=app/'atlas.pending.zip'
    subprocess.run([sys.executable,str(ROOT/'scripts/package-release.py'),str(pending)],check=True)
    # Keep the preceding application package for explicit rollback.
    if (app/'atlas.zip').exists():
        shutil.copyfile(app/'atlas.zip',app/'atlas.previous.zip')
    os.replace(pending,app/'atlas.zip')
    digest=hashlib.sha256((app/'atlas.zip').read_bytes()).hexdigest()
    (app/'atlas.sha256').write_text(digest+'\n')
    for source in (ROOT/'deploy/portable').iterdir():
        if source.is_file():
            shutil.copyfile(source,target/source.name)
            if source.suffix in {'.sh','.command'}:os.chmod(target/source.name,0o755)
    shutil.copyfile(ROOT/'docs/PORTABLE.md',target/'使用说明.md')
    print(json.dumps({'app_sha256':digest,'destination':str(target),'dataset_unchanged':True}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('destination',type=Path)
    package(p.parse_args().destination.expanduser().resolve())
