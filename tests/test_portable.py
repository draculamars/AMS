import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import socket
import time
import urllib.request
from pathlib import Path

import pytest
from test_engineering import env, client, make_history, put
from asset_manager import core, db, git_history, node, portable, project_index

ROOT = Path(__file__).resolve().parents[1]


def run_export(target, source, blobs, *extra):
    return subprocess.run([sys.executable, str(ROOT / 'scripts/export-portable.py'), str(target),
                           '--source-catalog', str(source), '--source-archive', str(blobs), *extra],
                          env={**os.environ, 'ATLAS_PORTABLE_ROOT': ''}, capture_output=True, text=True)


def test_export_current_history_git_and_relocation_without_host_reads(env, tmp_path, monkeypatch):
    first, second = make_history(env)
    put(env, '模型 A.stl', 'solid test\nendsolid')
    put(env, '.env', 'DO-NOT-COPY')
    project_index.index_one('p')
    node.initialize()
    with db.connect() as c:
        aid = c.execute("SELECT id FROM assets WHERE relpath='docs/notes.md'").fetchone()[0]
    core.archive_asset(aid)
    put(env, 'docs/notes.md', 'uncommitted final content')
    source, blobs = db.DATA, db.blob_root()
    original = (source / 'catalog.sqlite3').read_bytes()
    sibling = tmp_path / 'existing.txt'
    sibling.write_text('keep me')
    target = tmp_path / 'portable'
    result = run_export(target, source, blobs)
    assert result.returncode == 0, result.stderr
    assert (source / 'catalog.sqlite3').read_bytes() == original
    assert sibling.read_text() == 'keep me'
    cfg = json.loads((target / 'portable.json').read_text())
    assert cfg['coverage']['git_snapshots'] == 1
    moved = tmp_path / '另一 挂载点' / 'library'
    moved.parent.mkdir()
    target.rename(moved)
    monkeypatch.setattr(db, 'DATA', moved / cfg['active_dataset'] / 'catalog')
    monkeypatch.setenv('ASSET_BLOB_DIR', str(moved / 'archive'))
    monkeypatch.setenv('ATLAS_PORTABLE_ROOT', str(moved))
    # Even with the original path still online, content and Git come from the disk.
    put(env, 'docs/notes.md', 'WRONG HOST CONTENT')
    assert core.source_path(aid)[0].read_text() == 'uncommitted final content'
    with db.connect() as c:
        rid = c.execute('SELECT id FROM repositories').fetchone()[0]
        assert c.execute("SELECT COUNT(*) FROM assets WHERE relpath='.env'").fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM assets WHERE present=1').fetchone()[0] == 0
    assert git_history.repository(rid)['source'] == 'archive'
    assert {x['oid'] for x in git_history.commits(rid)['items']} == {first, second}
    assert 'First design' in git_history.blob_bytes(rid, first, 'docs/notes.md')[0].decode()
    check = subprocess.run([sys.executable, str(ROOT / 'scripts/export-portable.py'), str(moved), '--verify'], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    blob = core.source_path(aid)[0]
    blob.write_text('corrupt')
    check = subprocess.run([sys.executable, str(ROOT / 'scripts/export-portable.py'), str(moved), '--verify'], capture_output=True, text=True)
    assert check.returncode != 0 and 'verification failed' in check.stderr


def test_export_refuses_existing_directory_and_published_overwrite(env, tmp_path):
    target = tmp_path / 'personal'
    target.mkdir()
    (target / 'important').write_text('keep')
    result = run_export(target, db.DATA, db.blob_root())
    assert result.returncode != 0
    assert (target / 'important').read_text() == 'keep'
    clean = tmp_path / 'new'
    assert run_export(clean, db.DATA, db.blob_root()).returncode == 0
    assert run_export(clean, db.DATA, db.blob_root(), '--resume').returncode != 0


def test_failed_missing_history_is_not_published_then_resume(env, tmp_path):
    put(env, 'a.txt', 'history')
    core.scan_project('p')
    with db.connect() as c:
        aid = c.execute("SELECT id FROM assets WHERE relpath='a.txt'").fetchone()[0]
    stored = core.archive_asset(aid)
    blob = db.blob_root() / stored['sha256'][:2] / stored['sha256']
    content = blob.read_bytes()
    blob.unlink()
    target = tmp_path / 'partial'
    failed = run_export(target, db.DATA, db.blob_root())
    assert failed.returncode != 0 and not (target / 'portable.json').exists()
    blob.write_bytes(content)
    good = run_export(target, db.DATA, db.blob_root(), '--resume')
    assert good.returncode == 0, good.stderr


def test_portable_http_guards_host_mutations_but_allows_library_metadata(env, client, monkeypatch):
    monkeypatch.setenv('ATLAS_PORTABLE_ROOT', str(env))
    headers = {'x-asset-request': '1'}
    for path in ['/api/scan', '/api/projects/import', '/api/engineering-index', '/api/development-index',
                 '/api/knowledge-index', '/api/git/plan', '/api/migration/plan', '/api/vault/sync',
                 '/api/assets/1/source-edit/apply']:
        assert client.post(path, json={}, headers=headers).status_code == 409
    assert client.get('/api/vault/profiles/example/plan').status_code == 409
    assert client.post('/api/project-preferences/p', json={'favorite': True}, headers=headers).status_code == 200
    assert client.get('/api/health').status_code == 200
    monkeypatch.delenv('ATLAS_PORTABLE_ROOT')
    assert client.post('/api/project-preferences/p', json={'favorite': False}, headers=headers).status_code == 200


def test_portable_missing_blob_never_falls_back_to_host(env, monkeypatch):
    put(env, 'a.txt', 'host-private-content')
    core.scan_project('p')
    with db.connect() as c:
        aid = c.execute('SELECT id FROM assets').fetchone()[0]
    monkeypatch.setenv('ATLAS_PORTABLE_ROOT', str(env))
    with pytest.raises(FileNotFoundError, match='不会读取'):
        core.source_path(aid)


def test_portable_rollback_journal_and_regular_mode_wal(env, monkeypatch):
    monkeypatch.setenv('ATLAS_PORTABLE_ROOT', str(env))
    db.init()
    with db.connect() as c:
        assert c.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert c.execute('PRAGMA synchronous').fetchone()[0] == 2
    monkeypatch.delenv('ATLAS_PORTABLE_ROOT')
    db.init()
    with db.connect() as c:
        assert c.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'


def test_launcher_real_http_repeat_stop_backup_and_mount_move(env, tmp_path):
    put(env,'readme.md','portable library')
    target=tmp_path/'随行 资产库'
    assert run_export(target,db.DATA,db.blob_root()).returncode==0
    package=subprocess.run([sys.executable,str(ROOT/'scripts/package-portable.py'),str(target)],capture_output=True,text=True)
    assert package.returncode==0,package.stderr
    runtime=tmp_path/'runtime';runtime.mkdir()
    environment={**os.environ,'ATLAS_RUNTIME_DIR':str(runtime),'ATLAS_NO_BROWSER':'1'}
    def command(root,action):
        return [sys.executable,'-I',str(root/'launcher.py'),str(root),action,'--no-browser']
    def get(port,path):
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{port}'+path,timeout=2) as r:return json.load(r)
    for attempt in range(2):
        proc=subprocess.Popen(command(target,'start'),env=environment,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        try:
            for _ in range(120):
                if proc.poll() is not None:raise AssertionError(proc.stdout.read())
                if (target/'session.json').exists():
                    state=json.loads((target/'session.json').read_text())
                    try:
                        if get(state['port'],'/api/health').get('portable_session')==state['nonce']:break
                    except OSError:pass
                time.sleep(.1)
            else:raise AssertionError('Launcher did not start')
            assert get(state['port'],'/api/portable')['enabled']
            assert get(state['port'],'/api/engineering/p/tree')['items'][0]['name']=='readme.md'
            repeated=subprocess.run(command(target,'start'),env=environment,capture_output=True,text=True,timeout=20)
            assert repeated.returncode==0 and 'already running' in repeated.stdout,repeated.stderr
            stopped=subprocess.run(command(target,'stop'),env=environment,capture_output=True,text=True,timeout=35)
            assert stopped.returncode==0,stopped.stderr
            proc.wait(timeout=20)
            assert not (target/'session.json').exists()
            with sqlite3.connect(target/'backups/catalog-clean-stop.sqlite3') as c:
                assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        finally:
            if proc.poll() is None:proc.terminate();proc.wait(timeout=20)
        if attempt==0:
            moved=tmp_path/'media'/'another disk'/'AtlasPortable';moved.parent.mkdir(parents=True)
            target.rename(moved);target=moved
            shutil.rmtree(env)


def test_probe_detects_files_removed_after_successful_write(tmp_path,monkeypatch):
    import threading
    original=portable.storage_probe
    def remove():
        folder=tmp_path/'build'
        for _ in range(100):
            files=list(folder.glob('atlas-storage-probe-*'))
            if files:
                time.sleep(.02)
                for p in files:p.unlink(missing_ok=True)
                return
            time.sleep(.005)
    thread=threading.Thread(target=remove);thread.start()
    with pytest.raises(ValueError,match='持久性验证失败'):
        original(tmp_path,delay=.2)
    thread.join()


def test_disconnected_portable_volume_refuses_new_database(env,monkeypatch,tmp_path):
    root=tmp_path/'removable';root.mkdir();(root/'portable.json').write_text('{}')
    monkeypatch.setenv('ATLAS_PORTABLE_ROOT',str(root))
    monkeypatch.setenv('ATLAS_PORTABLE_DEVICE',str(root.stat().st_dev))
    (root/'portable.json').unlink()
    monkeypatch.setattr(db,'DATA',root/'must-not-be-created')
    with pytest.raises(FileNotFoundError,match='磁盘已断开'):
        with db.connect():pass
    assert not db.DATA.exists()
