#!/usr/bin/env python3
"""Build offline macOS/Ubuntu runtime archives without executing foreign binaries.

Requires a build host with Python 3.12, uv, curl and a macOS Git binary for Mac
targets. Builds only inside the marked export directory. Sources and SHA-256
receipts are saved alongside the archives. End users need none of these tools.
"""
import argparse
import hashlib
import io
import json
import lzma
import os
import posixpath
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
RELEASE = '20260728'
PYTHON = '3.12.13'
BASE = 'https://releases.astral.sh/github/python-build-standalone/releases/download/' + RELEASE
TARGETS = {
    'Darwin-arm64': 'aarch64-apple-darwin',
    'Darwin-x86_64': 'x86_64-apple-darwin',
    'Linux-x86_64': 'x86_64-unknown-linux-gnu',
    'Linux-aarch64': 'aarch64-unknown-linux-gnu',
}


def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def download(url, dest, expected=None):
    if dest.exists() and expected and sha(dest) == expected:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + '.download')
    for attempt in range(8):
        result = subprocess.run(['curl', '-fL', '--retry', '2', '--connect-timeout', '20', '--max-time', '600',
                                 '-C', '-', url, '-o', str(tmp)])
        if result.returncode == 0:
            break
        if result.returncode in {22, 33, 36}:
            tmp.unlink(missing_ok=True)
    else:
        raise ValueError('Download failed after retries: ' + url)
    if expected and sha(tmp) != expected:
        raise ValueError('Publisher checksum mismatch: ' + url)
    os.replace(tmp, dest)


def add_bytes(out, name, content, mode=0o644):
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = mode
    out.addfile(info, io.BytesIO(content))


def regularized(source, output):
    """Materialize internal symlinks so ExFAT never needs to create a symlink."""
    members = {m.name.rstrip('/'): m for m in source.getmembers()}
    for name, member in members.items():
        if not name or name.startswith('/') or '..' in Path(name).parts:
            raise ValueError('Unsafe vendor archive path')
        if member.isdir():
            continue
        resolved = member
        seen = set()
        while resolved.issym() or resolved.islnk():
            if resolved.name in seen:
                raise ValueError('Vendor symlink loop')
            seen.add(resolved.name)
            target = posixpath.normpath(posixpath.join(posixpath.dirname(resolved.name), resolved.linkname)) if resolved.issym() else resolved.linkname
            if target not in members:
                raise ValueError('Unresolved vendor symlink: ' + target)
            resolved = members[target]
        if not resolved.isfile():
            raise ValueError('Unsupported archive entry: ' + name)
        info = tarfile.TarInfo(name)
        info.size = resolved.size
        info.mode = resolved.mode
        with source.extractfile(resolved) as data:
            output.addfile(info, data)


def deb_data(path):
    raw = path.read_bytes()
    if not raw.startswith(b'!<arch>\n'):
        raise ValueError('Invalid Debian archive')
    offset = 8
    while offset + 60 <= len(raw):
        name = raw[offset:offset + 16].decode().strip().rstrip('/')
        size = int(raw[offset + 48:offset + 58])
        data = raw[offset + 60:offset + 60 + size]
        if name.startswith('data.tar'):
            return tarfile.open(fileobj=io.BytesIO(data), mode='r:*')
        offset += 60 + size + size % 2
    raise ValueError('Missing Debian data archive')


def ubuntu_git(arch, stage):
    deb_arch = 'amd64' if arch == 'x86_64' else 'arm64'
    base = 'https://archive.ubuntu.com/ubuntu' if deb_arch == 'amd64' else 'https://ports.ubuntu.com/ubuntu-ports'
    index = stage / ('Packages-' + deb_arch + '.xz')
    download(base + '/dists/jammy/main/binary-' + deb_arch + '/Packages.xz', index)
    records = {}
    for block in lzma.decompress(index.read_bytes()).decode().split('\n\n'):
        values = dict(line.split(': ', 1) for line in block.splitlines() if ': ' in line and not line.startswith(' '))
        if values.get('Package') in {'git', 'libpcre2-8-0'}:
            records[values['Package']] = values
    files, sources = {}, []
    for package in ['git', 'libpcre2-8-0']:
        record = records[package]
        url = base + '/' + record['Filename']
        path = stage / Path(record['Filename']).name
        download(url, path, record['SHA256'])
        sources.append({'package': package, 'version': record['Version'], 'url': url, 'sha256': record['SHA256']})
        with deb_data(path) as tar:
            for member in tar.getmembers():
                name = member.name.removeprefix('./')
                if not member.isfile():
                    continue
                target = 'git/bin/git' if name == 'usr/bin/git' else 'git/lib/libpcre2-8.so.0' if '/libpcre2-8.so.0.' in name else 'git/licenses/' + package + '-copyright' if name == 'usr/share/doc/' + package + '/copyright' else None
                if target:
                    files[target] = (tar.extractfile(member).read(), member.mode)
    if 'git/bin/git' not in files or 'git/lib/libpcre2-8.so.0' not in files:
        raise ValueError('Missing Git binary/dependency')
    return files, sources


def build(root, targets, mac_git):
    if not (root / 'export-state.json').is_file():
        raise ValueError('Runtime output must be inside a marked Atlas export')
    sys.path.insert(0,str(APP))
    from asset_manager.portable import storage_probe
    storage_probe(root)
    stage = root / 'build' / 'runtime-inputs'
    stage.mkdir(parents=True, exist_ok=True)
    runtime = root / 'runtimes'
    runtime.mkdir(exist_ok=True)
    sums = stage / 'SHA256SUMS'
    download(BASE + '/SHA256SUMS', sums)
    checksums = {line.split()[1]: line.split()[0] for line in sums.read_text().splitlines() if len(line.split()) == 2}
    receipts = json.loads((runtime / 'manifest.json').read_text()) if (runtime / 'manifest.json').exists() else {}
    for target in targets:
        if target in receipts and (runtime / receipts[target]['file']).is_file() and sha(runtime / receipts[target]['file']) == receipts[target]['sha256']:
            print('Already built:', target, flush=True)
            continue
        triple = TARGETS[target]
        filename = f'cpython-{PYTHON}+{RELEASE}-{triple}-install_only_stripped.tar.gz'
        python = stage / filename
        download(BASE + '/' + filename.replace('+', '%2B'), python, checksums[filename])
        deps = stage / ('deps-' + target)
        if deps.exists():
            shutil.rmtree(deps)
        subprocess.run(['uv', 'pip', 'install', '--target', str(deps), '--python-version', '3.12', '--python-platform', triple,
                        '--only-binary', ':all:', '-r', str(APP / 'requirements.lock')],
                       check=True, env={**os.environ, 'UV_CACHE_DIR': str(stage / 'uv-cache'), 'TMPDIR': str(stage)})
        sources = [{'url': BASE + '/' + filename, 'sha256': checksums[filename], 'publisher_checksums': BASE + '/SHA256SUMS'}]
        if target.startswith('Darwin'):
            if not mac_git or not mac_git.is_file():
                raise ValueError('Supply --mac-git with a macOS universal Git binary')
            libraries = subprocess.check_output(['otool', '-L', str(mac_git)], text=True)
            if any(line.startswith('\t') and line.strip().startswith('/') and not line.strip().startswith(('/usr/lib/', '/System/Library/')) for line in libraries.splitlines()[1:]):
                raise ValueError('macOS Git requires non-system libraries')
            git_files = {'git/bin/git': (mac_git.read_bytes(), 0o755)}
            # Git's GPL license and corresponding upstream source reference travel with it.
            license_path = stage / 'git-COPYING'
            download('https://raw.githubusercontent.com/git/git/master/COPYING', license_path)
            git_files['git/licenses/COPYING'] = (license_path.read_bytes(), 0o644)
            sources.append({'git': subprocess.check_output([str(mac_git), '--version'], text=True).strip(),
                            'source': 'https://github.com/apple-oss-distributions/Git', 'binary_sha256': sha(mac_git), 'requires': 'macOS system libraries'})
        else:
            git_files, git_sources = ubuntu_git(target.split('-', 1)[1], stage)
            sources += git_sources
        dest = runtime / (target + '.tar.gz')
        with tarfile.open(dest.with_suffix('.tmp'), 'w:gz', compresslevel=5) as out:
            with tarfile.open(python, 'r:gz') as src:
                regularized(src, out)
            for path in sorted(deps.rglob('*')):
                if path.is_file() and not path.is_symlink():
                    out.add(path, arcname='python/lib/python3.12/site-packages/' + path.relative_to(deps).as_posix(), recursive=False)
            for name, (content, mode) in git_files.items():
                add_bytes(out, name, content, mode)
            add_bytes(out, 'SOURCES.json', json.dumps(sources, ensure_ascii=False, indent=2).encode())
        os.replace(dest.with_suffix('.tmp'), dest)
        receipts[target] = {'file': dest.name, 'sha256': sha(dest), 'bytes': dest.stat().st_size,
                            'python': PYTHON, 'requirements_sha256': sha(APP / 'requirements.lock'), 'sources': sources}
        (runtime / 'manifest.json').write_text(json.dumps(receipts, ensure_ascii=False, indent=2))
        (runtime / (target + '.sha256')).write_text(receipts[target]['sha256'] + '\n')
        print(json.dumps({'built': target, 'bytes': dest.stat().st_size}), flush=True)
    return receipts


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--targets', nargs='+', choices=TARGETS, default=list(TARGETS))
    parser.add_argument('--mac-git', type=Path)
    args = parser.parse_args()
    build(args.root.resolve(), args.targets, args.mac_git)
