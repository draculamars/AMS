#!/usr/bin/env python3
"""Export an independent, relocatable library. Never writes to source catalog/projects.

Run with Python 3.12. A failed export remains unpublished and may be resumed.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))


def stamp():
    return datetime.now(timezone.utc).isoformat()


def emit(**fields):
    print(json.dumps({'at': stamp(), **fields}, ensure_ascii=False), flush=True)


def atomic_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as out:
        json.dump(data, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def digest(path):
    with Path(path).open('rb') as src:
        return hashlib.file_digest(src, 'sha256').hexdigest()


def checked_copy(source, destination, expected=None):
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or not source.is_file():
        raise ValueError('Not a regular source file: ' + str(source))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError('Symlink in destination: ' + str(destination))
    before = source.stat()
    fd, tmp = tempfile.mkstemp(dir=destination.parent, prefix='.copy-')
    h = hashlib.sha256()
    try:
        if sys.platform == 'darwin' and os.environ.get('ATLAS_COPY_MODE') == 'clone':
            # APFS copy-on-write staging avoids another full physical copy on
            # a space-constrained Mac. The clone is independent of its source.
            import ctypes
            os.close(fd); fd = None; os.unlink(tmp)
            libc = ctypes.CDLL('libc.dylib', use_errno=True)
            if libc.clonefile(os.fsencode(source), os.fsencode(tmp), 1) == 0:
                sha = digest(tmp)
                after = source.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError('Source changed during clone: ' + str(source))
                if expected and sha != expected: raise ValueError('Source hash mismatch: ' + str(source))
                with open(tmp, 'rb') as f: os.fsync(f.fileno())
                os.replace(tmp, destination)
                return sha, before.st_size
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with source.open('rb') as src, os.fdopen(fd, 'wb') as out:
            for chunk in iter(lambda: src.read(4 * 1024**2), b''):
                h.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('Source changed during capture: ' + str(source))
        sha = h.hexdigest()
        if expected and sha != expected:
            raise ValueError('Source hash mismatch: ' + str(source))
        os.replace(tmp, destination)
        return sha, after.st_size
    finally:
        Path(tmp).unlink(missing_ok=True)


class Objects:
    def __init__(self, root):
        self.root = root
        self.seen = {}
        self.files = {}
        (root / '.incoming').mkdir(parents=True, exist_ok=True)

    def capture(self, source, expected=None):
        source = Path(source)
        st = source.stat()
        ident = (str(source.resolve()), st.st_size, st.st_mtime_ns)
        if ident in self.seen:
            sha, size = self.seen[ident]
            if expected and expected != sha:
                raise ValueError('Conflicting historical hash: ' + str(source))
            return sha, size
        if sys.platform == 'darwin' and os.environ.get('ATLAS_COPY_MODE') == 'clone':
            import ctypes
            if source.is_symlink() or not source.is_file(): raise ValueError('Not a regular source file')
            temporary = self.root/'.incoming'/uuid.uuid4().hex
            libc = ctypes.CDLL('libc.dylib', use_errno=True)
            if libc.clonefile(os.fsencode(source), os.fsencode(temporary), 1) == 0:
                try:
                    after = source.stat()
                    if (st.st_size, st.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise ValueError('Source changed during clone: '+str(source))
                    # Historical object IDs are checked in the mandatory full
                    # verification before publishing, rather than re-read here.
                    sha = expected or digest(temporary)
                    if not re.fullmatch('[a-f0-9]{64}', sha): raise ValueError('Invalid object digest')
                    target = self.root/sha[:2]/sha
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(temporary, target)
                    self.files[sha] = st.st_size
                    self.seen[ident] = (sha, st.st_size)
                    return sha, st.st_size
                finally:
                    temporary.unlink(missing_ok=True)
        # Hash once before copying to avoid writing duplicate large source files.
        sha = digest(source)
        if expected and expected != sha:
            raise ValueError('Historical object checksum mismatch: ' + str(source))
        after = source.stat()
        if (st.st_size, st.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('Source changed during hashing: ' + str(source))
        target = self.root / sha[:2] / sha
        if sha not in self.files:
            if not target.exists() or target.stat().st_size != st.st_size or digest(target) != sha:
                checked_copy(source, target, sha)
            self.files[sha] = st.st_size
        self.seen[ident] = (sha, st.st_size)
        return sha, st.st_size


def git_snapshot(repo, catalog, archive):
    from asset_manager import git_history
    meta = json.loads(repo['body'])
    tips = sorted({r['oid'] for r in meta['refs']} | set(meta['heads']))
    if not tips:
        return None
    key = meta['fingerprint']
    destination = catalog / 'git-history' / 'objects' / key
    if not (destination / 'ATLAS-GIT.json').is_file():
        args = ['--revs']
        inputs = tips
        if meta.get('missing_count'):
            lines = git_history.run(repo['source_root'], 'rev-list', '--objects', '--all', *meta['heads'], '--missing=print', '--no-object-names').decode().splitlines()
            if sorted(x[1:] for x in lines if x.startswith('?')) != meta['missing_objects']:
                raise ValueError('Partial-clone object state changed')
            inputs = [x for x in lines if not x.startswith('?')]
            args = []
        env = {**os.environ, 'ASSET_DATA_DIR': str(catalog), 'ASSET_BLOB_DIR': str(archive), 'PYTHONPATH': str(APP)}
        receiver = subprocess.Popen([sys.executable, '-m', 'asset_manager.project_sync', 'receive-git', key],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=APP)
        pack = None
        try:
            receiver.stdin.write(json.dumps(meta).encode() + b'\n')
            receiver.stdin.flush()
            with tempfile.TemporaryFile() as errors:
                pack = subprocess.Popen(git_history.command(repo['source_root'], '-c', 'pack.threads=2', 'pack-objects', '--stdout', *args, '--delta-base-offset'),
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, env=git_history.environment())
                pack.stdin.write(('\n'.join(inputs) + '\n').encode())
                pack.stdin.close()
                shutil.copyfileobj(pack.stdout, receiver.stdin, 1024**2)
                pack.stdout.close()
                rc = pack.wait()
                receiver.stdin.close()
                receiver.stdin = None
                out, err = receiver.communicate(timeout=600)
                if rc or receiver.returncode:
                    errors.seek(0)
                    raise ValueError('Git capture failed: ' + (errors.read() + err).decode(errors='replace')[-2000:])
        finally:
            for proc in [pack, receiver]:
                if proc and proc.poll() is None:
                    proc.terminate()
                    proc.wait()
    result = subprocess.run(git_history.command(destination, 'fsck', '--full', '--no-reflogs'), capture_output=True, timeout=600)
    if result.returncode:
        raise ValueError('Git fsck failed: ' + result.stderr.decode(errors='replace')[-2000:])
    return destination


def export(root, source_catalog, source_archive, resume=False, refresh=True, web_imports=(), capture_workers=1):
    root, source_catalog, source_archive = [Path(p).expanduser().resolve() for p in (root, source_catalog, source_archive)]
    if root == Path('/') or root == Path.home() or root.is_relative_to(source_catalog) or root.is_relative_to(source_archive):
        raise ValueError('Choose a new dedicated portable directory')
    marker = root / 'export-state.json'
    if root.exists():
        if not resume or not marker.is_file():
            raise ValueError('Destination already exists; only an unfinished Atlas export supports --resume')
        state = json.loads(marker.read_text())
        if state.get('format')!=1 or not re.fullmatch('[0-9a-f]{32}',state.get('id','')):
            raise ValueError('Invalid export identity')
        if state['status'] == 'complete' or (root / 'portable.json').exists():
            raise ValueError('Published library cannot be overwritten. Export to a new directory.')
        if state['source_catalog'] != str(source_catalog):
            raise ValueError('Resume source differs')
    else:
        root.mkdir(parents=True)
        state = {'format': 1, 'id': uuid.uuid4().hex, 'started_at': stamp(), 'status': 'building',
                 'source_catalog': str(source_catalog), 'indexed': [], 'captured': []}
        atomic_json(marker, state)
    # Refuse symlink substitutions before writing within a resumable export.
    for p in root.rglob('*'):
        if p.is_symlink():
            raise ValueError('Symlink in portable export: ' + str(p))
    from asset_manager.portable import storage_probe
    emit(phase='storage-probe',result=storage_probe(root))
    catalog = root / 'datasets' / state['id'] / 'catalog'
    archive = root / 'archive'
    catalog.mkdir(parents=True, exist_ok=True)
    (root / 'reports').mkdir(exist_ok=True)
    database = catalog / 'catalog.sqlite3'
    if not database.exists():
        # SQLite online backup is the only read of the live directory database.
        temporary=database.with_suffix('.copying')
        with closing(sqlite3.connect(source_catalog.joinpath('catalog.sqlite3').as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(temporary)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise ValueError('Source catalog backup failed integrity check')
            dst.execute('PRAGMA journal_mode=DELETE').fetchall()
        os.replace(temporary,database)
        with sqlite3.connect(database) as c:
            c.execute("DELETE FROM settings WHERE key LIKE 'session:%' OR key IN ('password','sync_target','migration_root','codex_source_root')")
            c.execute('DELETE FROM plans')
            c.execute('DELETE FROM source_edit_plans')
            c.execute("UPDATE jobs SET status='interrupted' WHERE status IN ('queued','running')")
        c.close()
    os.environ['ASSET_DATA_DIR'] = str(catalog)
    os.environ['ASSET_BLOB_DIR'] = str(source_archive)  # Source reads during capture.
    os.environ['ASSET_WATCH_ENABLED'] = '0'
    from asset_manager import db, core, project_index, git_history, node, web_assets
    db.DATA = catalog
    with closing(sqlite3.connect(database)) as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'projects','assets','versions','project_locations'}<=tables:
            raise ValueError('Unfinished catalog is damaged; preserve this export and start in a new directory')
    db.init()
    objects = Objects(archive)
    with db.connect() as c:
        source_node = c.execute("SELECT value FROM settings WHERE key='node_id'").fetchone()
        projects = [dict(r) for r in c.execute('SELECT * FROM projects ORDER BY name')]
        runs = [r[0] for r in c.execute('SELECT manifest FROM project_index_runs')]
    if any(root.is_relative_to(Path(p['path']).resolve()) for p in projects):
        raise ValueError('Export destination must be outside every source project')
    for name in runs:
        if Path(name).name != name:
            raise ValueError('Unsafe index manifest name')
        dest = catalog / 'project-index' / name
        if not dest.exists():
            checked_copy(source_catalog / 'project-index' / name, dest)
    for p in projects:
        if p['id'] not in state['indexed']:
            emit(phase='index', project=p['name'])
            if refresh and node.location(p['id'])['mode'] == 'workspace':
                if not Path(p['path']).is_dir():
                    raise ValueError('Source project offline: ' + p['name'])
                project_index.index_one(p['id'])
            state['indexed'].append(p['id'])
            atomic_json(marker, state)
    # Keep every locally stored historical asset version, including retired paths.
    with db.connect() as c:
        histories = list(c.execute('SELECT DISTINCT sha256,size FROM versions'))
    for i, (sha, size) in enumerate(histories):
        if not re.fullmatch('[0-9a-f]{64}', sha):
            raise ValueError('Invalid historical digest')
        source = source_archive / sha[:2] / sha
        if not source.is_file():
            source = archive / sha[:2] / sha  # Validated content from an interrupted export.
        actual, length = objects.capture(source, sha)
        if length != size:
            raise ValueError('Historical size mismatch: ' + sha)
        if i % 2000 == 0:
            emit(phase='history', done=i, total=len(histories))
    for p in projects:
        if p['id'] in state['captured']:
            continue
        rows = project_index.rows(p['id'])
        current = node.current_paths(p['id'])
        overrides = project_index.source_overrides(p['id'])
        captured = []
        current_rows=[a for a in rows if a['relpath'] in current]
        def capture_row(a):
            source=project_index.source_for(a,p['path'],overrides)
            sha,size=objects.capture(source)
            return a['id'],a['relpath'],sha,size
        # Bound queued tasks and memory. Each capture freezes/checks its own
        # source; content objects still require full verification at publication.
        with ThreadPoolExecutor(max_workers=capture_workers) as pool:
            for offset in range(0,len(current_rows),500):
                captured.extend(pool.map(capture_row,current_rows[offset:offset+500]))
                emit(phase='content',project=p['name'],done=len(captured),total=len(current_rows),objects=len(objects.files))
        with db.connect() as c:
            c.execute('DELETE FROM mirror_files WHERE project_id=?', (p['id'],))
            for aid, path, sha, size in captured:
                c.execute('INSERT INTO mirror_files VALUES(?,?,?,?)', (p['id'], path, sha, size))
                c.execute("INSERT OR IGNORE INTO versions(asset_id,sha256,size,reason) VALUES(?,?,?,'snapshot')", (aid, sha, size))
                c.execute('UPDATE assets SET size=? WHERE id=?', (size, aid))
            # Capture immutable records only; original absolute paths stay as provenance.
            c.execute('UPDATE assets SET present=0 WHERE project_id=?', (p['id'],))
            c.execute('INSERT OR REPLACE INTO project_locations VALUES(?,?,?,?,?,?)',
                      (p['id'], state['id'], 'archive', json.dumps({'id': source_node[0] if source_node else None}), p['path'], stamp()))
        state['captured'].append(p['id'])
        atomic_json(marker, state)
        emit(phase='project-captured', project=p['name'], files=len(captured))
    os.environ['ASSET_BLOB_DIR'] = str(archive)
    # Preserve immutable web history and adapter-generated objects as well as
    # normal asset versions. Missing objects never fall back to live host paths.
    web_assets.init()
    with db.connect() as c:
        bundle_ids = [r[0] for r in c.execute('SELECT id FROM web_bundles')]
    for bid in bundle_ids:
        manifest = web_assets.manifest(str(source_catalog), bid)
        checked_copy(source_catalog/'web-bundles'/(bid+'.json.gz'), catalog/'web-bundles'/(bid+'.json.gz'))
        for info in manifest['files'].values():
            objects.capture(source_archive/info['sha256'][:2]/info['sha256'], info['sha256'])
    for descriptor in web_imports:
        descriptor = Path(descriptor)
        for item in json.loads(descriptor.with_name('objects.json').read_text(encoding='utf-8')):
            existing = archive/item['sha256'][:2]/item['sha256']
            source = existing if existing.is_file() else Path(item['source'])
            sha, size = objects.capture(source, item['sha256'])
            if size != item['size']: raise ValueError('Web publication size mismatch')
        subprocess.run([sys.executable, str(APP/'scripts/web-assets.py'), 'import', '--manifest', str(descriptor)],
                       env={**os.environ, 'ASSET_DATA_DIR': str(catalog), 'ASSET_BLOB_DIR': str(archive)}, check=True)
    emit(phase='web-discovery', result=web_assets.discover())
    with db.connect() as c:
        web_projects = [r[0] for r in c.execute('SELECT DISTINCT project_id FROM mirror_files')]
    for pid in web_projects:
        emit(phase='web-publication', result=web_assets.publish(pid))
    with db.connect() as c:
        repositories = [dict(r) for r in c.execute('SELECT * FROM repositories')]
        old_archives = [dict(r) for r in c.execute('SELECT * FROM repository_archives')]
    for r in old_archives:
        key = r['fingerprint']
        if not re.fullmatch('[0-9a-f]{64}', key):
            raise ValueError('Invalid historical Git fingerprint')
        dst = catalog / 'git-history' / 'objects' / key
        if not (dst / 'ATLAS-GIT.json').is_file():
            src = source_catalog / 'git-history' / 'objects' / key
            for path in src.rglob('*'):
                if path.is_file():
                    checked_copy(path, dst / path.relative_to(src))
        if not (dst / 'ATLAS-GIT.json').is_file():
            raise ValueError('Missing prior Git archive: ' + key)
        with db.connect() as c:
            c.execute('UPDATE repository_archives SET archive_path=? WHERE repository_id=? AND fingerprint=?', (str(dst), r['repository_id'], key))
    for i, r in enumerate(repositories):
        emit(phase='git', done=i, total=len(repositories))
        meta = json.loads(r['body'])
        path = git_snapshot(r, catalog, archive)
        if path:
            with db.connect() as c:
                c.execute('UPDATE repositories SET archive_path=?,archive_at=? WHERE id=?', (str(path), stamp(), r['id']))
                c.execute('INSERT OR REPLACE INTO repository_archives VALUES(?,?,?,?,?)', (r['id'], meta['fingerprint'], r['body'], str(path), stamp()))
    with db.connect() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES('node_id',?)", (state['id'],))
        c.execute("INSERT OR REPLACE INTO settings VALUES('node_label','Atlas 随行资产库')")
        c.execute("INSERT OR REPLACE INTO settings VALUES('watch_status',?)", (json.dumps({'status': 'disabled', 'detail': '便携副本不扫描接入主机'}),))
        db.audit(c, 'portable_export', {'source_node': source_node[0] if source_node else None, 'started_at': state['started_at']})
    with sqlite3.connect(database) as c:
        c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
        c.execute('PRAGMA journal_mode=DELETE').fetchall()
    c.close()
    report = verify(root, catalog)
    config = {'format': 1, 'id': state['id'], 'active_dataset': catalog.parent.relative_to(root).as_posix(),
              'captured_at': stamp(), 'started_at': state['started_at'], 'coverage': report,
              'note': 'File-stable capture interval; not a filesystem-wide atomic snapshot. Existing parser coverage and missing dependencies are preserved. Remote-only data is not included.'}
    atomic_json(root / 'reports' / 'export.json', config)
    atomic_json(root / 'portable.json', config)  # Publish only after full content verification.
    state['status'] = 'complete'
    atomic_json(marker, state)
    emit(phase='complete', **report)
    return config


def verify(root, catalog=None):
    root = Path(root).resolve()
    if catalog is None:
        pointer = root/'portable.json'
        if not pointer.exists(): pointer=root/'portable.pending.json'
        cfg = json.loads(pointer.read_text(encoding='utf-8'))
        if not re.fullmatch('datasets/[0-9a-f]{32}',cfg.get('active_dataset','')):
            raise ValueError('Invalid dataset path')
        catalog = root / cfg['active_dataset'] / 'catalog'
    with sqlite3.connect(Path(catalog).joinpath('catalog.sqlite3').as_uri() + '?mode=ro', uri=True) as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or c.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Catalog integrity check failed')
        objects = dict(c.execute('SELECT sha256,size FROM versions UNION SELECT sha256,size FROM mirror_files'))
        report = {name: c.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for name, table in {
            'projects': 'projects', 'current_files': 'mirror_files', 'asset_versions': 'versions',
            'git_snapshots': 'repository_archives', 'index_snapshots': 'project_index_runs',
            'development_turns': 'development_turns', 'knowledge_entries': 'knowledge_items'}.items()}
        runs = list(c.execute('SELECT manifest FROM project_index_runs'))
        git_keys = [r[0] for r in c.execute('SELECT DISTINCT fingerprint FROM repository_archives')]
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        bundle_ids = [r[0] for r in c.execute('SELECT id FROM web_bundles')] if 'web_bundles' in tables else []
        report['web_entries'] = c.execute('SELECT COUNT(*) FROM web_entries').fetchone()[0] if 'web_entries' in tables else 0
    import gzip
    for bid in bundle_ids:
        if not re.fullmatch('[a-f0-9]{64}', bid): raise ValueError('Invalid web bundle')
        raw = gzip.decompress((Path(catalog)/'web-bundles'/(bid+'.json.gz')).read_bytes())
        if hashlib.sha256(raw).hexdigest() != bid: raise ValueError('Web manifest checksum mismatch')
        for item in json.loads(raw)['files'].values():
            if item['sha256'] in objects and objects[item['sha256']] != item['size']: raise ValueError('Conflicting web size')
            objects[item['sha256']] = item['size']
    total = 0
    for i, (sha, size) in enumerate(objects.items()):
        if not re.fullmatch('[0-9a-f]{64}', sha):
            raise ValueError('Invalid object digest')
        path = root / 'archive' / sha[:2] / sha
        if path.is_symlink() or not path.is_file() or path.stat().st_size != size or digest(path) != sha:
            raise ValueError('Object verification failed: ' + sha)
        total += size
        if i % 2000 == 0:
            emit(phase='verify', done=i, total=len(objects), bytes=total)
    import gzip
    for (name,) in runs:
        if Path(name).name != name:
            raise ValueError('Unsafe manifest path')
        with gzip.open(Path(catalog) / 'project-index' / name, 'rt') as f:
            json.load(f)
    for key in git_keys:
        if not re.fullmatch('[0-9a-f]{64}', key):
            raise ValueError('Invalid Git snapshot')
        folder = Path(catalog) / 'git-history' / 'objects' / key
        from asset_manager import git_history
        subprocess.run(git_history.command(folder, 'fsck', '--full', '--no-reflogs'), check=True, capture_output=True, timeout=600)
    return {**report, 'objects': len(objects), 'object_bytes': total, 'content_sha256': 'all-passed', 'database': 'integrity_check+foreign_key_check passed', 'checked_at': stamp()}


def publish_received(root, platforms):
    root=Path(root).resolve()
    if (root/'portable.json').exists() or (root/'session.json').exists(): raise ValueError('Destination is already published or active')
    config=json.loads((root/'portable.pending.json').read_text(encoding='utf-8'))
    transfer=json.loads((root/'transfer-state.json').read_text(encoding='utf-8'))
    if config['id']!=transfer['id']: raise ValueError('Received library identity differs')
    if digest(root/'app/atlas.zip') != (root/'app/atlas.sha256').read_text().strip(): raise ValueError('Application checksum mismatch')
    runtimes=json.loads((root/'runtimes/manifest.json').read_text(encoding='utf-8'))
    if not set(platforms)<=set(runtimes): raise ValueError('Required platform runtime missing')
    for name,info in runtimes.items():
        if Path(info['file']).name!=info['file'] or digest(root/'runtimes'/info['file'])!=info['sha256']: raise ValueError('Runtime checksum mismatch: '+name)
    for name in ['launcher.py','start.sh','start.cmd','Stop Atlas.cmd','Stop Atlas.command']:
        if not (root/name).is_file(): raise ValueError('Portable launcher missing: '+name)
    result=verify(root)
    atomic_json(root/'reports/received-verification.json',result)
    os.replace(root/'portable.pending.json',root/'portable.json')
    transfer.update(status='complete',verified_at=stamp(),encrypted=False)
    atomic_json(root/'transfer-state.json',transfer)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--source-catalog', type=Path)
    parser.add_argument('--source-archive', type=Path)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--no-refresh', action='store_true')
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--web-publication', type=Path, action='append', default=[], help='Frozen publication.json with sibling objects.json source map')
    parser.add_argument('--clone-staging', action='store_true', help='Use independent APFS clones for local staging on macOS')
    parser.add_argument('--capture-workers',type=int,choices=range(1,9),default=1,help='Bounded concurrent file captures (1–8)')
    parser.add_argument('--publish-received', action='store_true')
    parser.add_argument('--require-platform', action='append', default=[])
    args = parser.parse_args()
    if args.publish_received:
        emit(phase='received-library-published',**publish_received(args.destination,args.require_platform))
    elif args.verify:
        result = verify(args.destination)
        atomic_json(args.destination / 'reports' / 'verification.json', result)
        emit(phase='verified', **result)
    else:
        if not args.source_catalog or not args.source_archive:
            parser.error('--source-catalog and --source-archive required')
        if args.clone_staging: os.environ['ATLAS_COPY_MODE'] = 'clone'
        export(args.destination, args.source_catalog, args.source_archive, args.resume, not args.no_refresh, args.web_publication,args.capture_workers)
