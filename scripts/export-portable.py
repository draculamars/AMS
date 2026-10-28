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
    result = subprocess.run(['git', '-C', str(destination), 'fsck', '--full', '--no-reflogs'], capture_output=True, timeout=600)
    if result.returncode:
        raise ValueError('Git fsck failed: ' + result.stderr.decode(errors='replace')[-2000:])
    return destination


def export(root, source_catalog, source_archive, resume=False, refresh=True):
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
    from asset_manager import db, core, project_index, git_history, node
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
        for i, a in enumerate(rows):
            if a['relpath'] not in current:
                continue
            source = project_index.source_for(a, p['path'], overrides)
            sha, size = objects.capture(source)
            captured.append((a['id'], a['relpath'], sha, size))
            if i % 500 == 0:
                emit(phase='content', project=p['name'], done=i, total=len(rows), objects=len(objects.files))
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
        cfg = json.loads((root / 'portable.json').read_text())
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
        subprocess.run(['git', '-C', str(folder), 'fsck', '--full', '--no-reflogs'], check=True, capture_output=True, timeout=600)
    return {**report, 'objects': len(objects), 'object_bytes': total, 'content_sha256': 'all-passed', 'database': 'integrity_check+foreign_key_check passed', 'checked_at': stamp()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--source-catalog', type=Path)
    parser.add_argument('--source-archive', type=Path)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--no-refresh', action='store_true')
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.verify:
        result = verify(args.destination)
        atomic_json(args.destination / 'reports' / 'verification.json', result)
        emit(phase='verified', **result)
    else:
        if not args.source_catalog or not args.source_archive:
            parser.error('--source-catalog and --source-archive required')
        export(args.destination, args.source_catalog, args.source_archive, args.resume, not args.no_refresh)
