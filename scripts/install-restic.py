#!/usr/bin/env python3
"""Install a pinned upstream Restic binary without admin privileges or package managers."""
import argparse,bz2,hashlib,json,os,platform,urllib.request
from pathlib import Path
VERSION='0.19.1'
HASHES={
'darwin_amd64':'c38d579622cf602f665234c5a8c315030b6cf70656028fe6dc29a786b60e5f35',
'darwin_arm64':'7be0a144ccc377880f294204aa271d76e4b79554b42a751151d425ce6ebac143',
'linux_amd64':'f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c',
'linux_arm64':'a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465'}
p=argparse.ArgumentParser();p.add_argument('--destination',type=Path,required=True);p.add_argument('--archive',type=Path);a=p.parse_args()
arch={'x86_64':'amd64','aarch64':'arm64'}.get(platform.machine(),platform.machine());key=platform.system().lower()+'_'+arch
if key not in HASHES:raise SystemExit('Unsupported platform: '+key)
url=f'https://github.com/restic/restic/releases/download/v{VERSION}/restic_{VERSION}_{key}.bz2'
raw=a.archive.read_bytes() if a.archive else urllib.request.urlopen(url,timeout=60).read()
if hashlib.sha256(raw).hexdigest()!=HASHES[key]:raise SystemExit('Restic archive checksum mismatch')
a.destination.parent.mkdir(parents=True,exist_ok=True)
tmp=a.destination.with_suffix('.new');tmp.write_bytes(bz2.decompress(raw));tmp.chmod(0o755);tmp.replace(a.destination)
print(json.dumps({'version':VERSION,'path':str(a.destination),'archive_sha256':HASHES[key],'source':url}))
