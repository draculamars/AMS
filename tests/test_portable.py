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
    result = run_export(target, source, blobs, '--capture-workers', '4')
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


def test_plain_recovery_point_restores_metadata_without_overwriting_dataset(env, tmp_path, monkeypatch):
    from asset_manager import portable_backup, web_assets
    put(env, 'readme.md', 'independent plaintext content')
    target = tmp_path/'ssd'
    result = run_export(target, db.DATA, db.blob_root())
    assert result.returncode == 0, result.stderr
    cfg = json.loads((target/'portable.json').read_text())
    catalog = target/cfg['active_dataset']/'catalog'
    monkeypatch.setattr(db, 'DATA', catalog)
    monkeypatch.setenv('ATLAS_PORTABLE_ROOT', str(target))
    monkeypatch.setenv('ASSET_BLOB_DIR', str(target/'archive'))
    web_assets.init()
    with db.connect() as c:c.execute("UPDATE assets SET note='before snapshot'")
    backup = portable_backup.create(lambda *args:None)
    assert backup['encrypted'] is False
    assert portable_backup.verify(backup['id'])['status'] == 'passed'
    unlisted=target/'backups/library-snapshots'/backup['id']/'catalog/unverified.txt'
    unlisted.write_text('not part of the checked restore point')
    with db.connect() as c:c.execute("UPDATE assets SET note='after snapshot'")
    (target/'session.json').write_text('{}')
    with pytest.raises(ValueError,match='安全停止'):portable_backup.restore(backup['id'])
    (target/'session.json').unlink()
    restored = portable_backup.restore(backup['id'])
    active = target/restored['activated_dataset']/'catalog/catalog.sqlite3'
    assert not (active.parent/'unverified.txt').exists()
    with sqlite3.connect(active) as c:assert c.execute('SELECT note FROM assets').fetchone()[0] == 'before snapshot'
    with db.connect() as c:assert c.execute('SELECT note FROM assets').fetchone()[0] == 'after snapshot'
    assert catalog.is_dir() and restored['previous_dataset_preserved']
    obj=next((target/'archive').glob('[a-f0-9][a-f0-9]/*'))
    obj.write_bytes(b'bad')
    with pytest.raises(ValueError,match='缺失或损坏'):portable_backup.verify(backup['id'])
    assert portable_backup.listing()[0]['verification']['status']=='failed'


def test_portable_allows_signed_publication_preview():
    assert portable.request_allowed('POST', '/api/web-publications/example/preview')
    assert portable.request_allowed('POST', '/api/portable/backup')
    assert not portable.request_allowed('POST', '/api/vault/profiles/example/backup')


def test_export_keeps_web_only_objects_and_manifest(env,tmp_path,monkeypatch):
    import io,hashlib
    from asset_manager import web_assets
    put(env,'readme.md','source')
    core.scan_project('p');web_assets.init()
    raw=b'<html><title>Frozen web</title>original generated bytes</html>'
    sha,size=core.store_blob(io.BytesIO(raw));path='web-publications/example/index.html'
    ident=hashlib.sha256(('p\0'+path).encode()).hexdigest()[:24]
    with db.connect() as c:c.execute('INSERT INTO web_entries VALUES(?,?,?,?,?,?,?)',(ident,'p',path,'Frozen web','document','{}',core.now()))
    pub=web_assets.publish('p',{path:{'sha256':sha,'size':size}})
    target=tmp_path/'web disk';result=run_export(target,db.DATA,db.blob_root())
    assert result.returncode==0,result.stderr
    cfg=json.loads((target/'portable.json').read_text())
    monkeypatch.setattr(db,'DATA',target/cfg['active_dataset']/'catalog')
    monkeypatch.setenv('ASSET_BLOB_DIR',str(target/'archive'))
    monkeypatch.setenv('ATLAS_PORTABLE_ROOT',str(target))
    shutil.rmtree(env)
    assert web_assets.manifest(str(db.DATA),pub['bundle_id'])['files'][path]['sha256']==sha
    assert web_assets.blob(sha).read_bytes()==raw


def test_transport_rejects_bad_hash_and_published_overwrite(tmp_path):
    import hashlib
    target=tmp_path/'ssd';ident='a'*32;raw=b'plain bytes';sha=hashlib.sha256(raw).hexdigest()
    header={'path':'archive/'+sha[:2]+'/'+sha,'sha256':sha,'size':len(raw)}
    command=[sys.executable,str(ROOT/'scripts/portable-receive.py'),str(target),'receive']
    prefix=(json.dumps({'id':ident})+'\n'+json.dumps(header)+'\n').encode()
    bad=subprocess.run(command,input=prefix+b'x'*len(raw)+b'{"end":true}\n',capture_output=True)
    assert bad.returncode!=0 and not (target/header['path']).exists()
    good=subprocess.run(command,input=prefix+raw+b'{"end":true}\n',capture_output=True)
    assert good.returncode==0,good.stderr
    assert (target/header['path']).read_bytes()==raw
    (target/'portable.json').write_text('{}')
    blocked=subprocess.run(command,input=prefix+raw+b'{"end":true}\n',capture_output=True)
    assert blocked.returncode!=0 and (target/header['path']).read_bytes()==raw


@pytest.mark.skipif(sys.platform!='darwin',reason='APFS clone capture is macOS-specific')
def test_clone_capture_is_independent_of_source_changes(tmp_path,monkeypatch):
    import hashlib
    spec=importlib.util.spec_from_file_location('portable_export_clone',ROOT/'scripts/export-portable.py')
    exporter=importlib.util.module_from_spec(spec);spec.loader.exec_module(exporter)
    monkeypatch.setenv('ATLAS_COPY_MODE','clone')
    source=tmp_path/'source';source.write_bytes(b'original independent content')
    objects=exporter.Objects(tmp_path/'archive')
    digest,size=objects.capture(source)
    source.write_bytes(b'changed source')
    frozen=tmp_path/'archive'/digest[:2]/digest
    assert frozen.read_bytes()==b'original independent content'
    assert hashlib.sha256(frozen.read_bytes()).hexdigest()==digest
    assert frozen.stat().st_size==size


def test_received_activation_rejects_corruption_and_missing_platform(env,tmp_path):
    import hashlib
    put(env,'readme.md','checked content')
    target=tmp_path/'received'
    result=run_export(target,db.DATA,db.blob_root())
    assert result.returncode==0,result.stderr
    cfg=json.loads((target/'portable.json').read_text())
    (target/'portable.json').rename(target/'portable.pending.json')
    (target/'transfer-state.json').write_text(json.dumps({'id':cfg['id']}))
    result=subprocess.run([sys.executable,str(ROOT/'scripts/package-portable.py'),str(target)],capture_output=True)
    assert result.returncode==0,result.stderr
    runtime=target/'runtimes';runtime.mkdir()
    archive=runtime/'test.tar.gz';archive.write_bytes(b'fixture runtime bytes')
    (runtime/'manifest.json').write_text(json.dumps({'Windows-AMD64':{'file':archive.name,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}}))
    command=[sys.executable,str(ROOT/'scripts/export-portable.py'),str(target),'--publish-received']
    missing=subprocess.run(command+['--require-platform','Darwin-arm64'],capture_output=True)
    assert missing.returncode!=0 and not (target/'portable.json').exists()
    blob=next((target/'archive').glob('[a-f0-9][a-f0-9]/*'));original=blob.read_bytes();blob.write_bytes(b'damaged')
    corrupt=subprocess.run(command,capture_output=True)
    assert corrupt.returncode!=0 and not (target/'portable.json').exists()
    blob.write_bytes(original)
    success=subprocess.run(command+['--require-platform','Windows-AMD64'],capture_output=True)
    assert success.returncode==0,success.stderr
    assert (target/'portable.json').is_file() and not (target/'portable.pending.json').exists()
    assert json.loads((target/'transfer-state.json').read_text())['encrypted'] is False


def test_disconnected_portable_volume_refuses_new_database(env,monkeypatch,tmp_path):
    root=tmp_path/'removable';root.mkdir();(root/'portable.json').write_text('{}')
    monkeypatch.setenv('ATLAS_PORTABLE_ROOT',str(root))
    monkeypatch.setenv('ATLAS_PORTABLE_DEVICE',str(root.stat().st_dev))
    (root/'portable.json').unlink()
    monkeypatch.setattr(db,'DATA',root/'must-not-be-created')
    with pytest.raises(FileNotFoundError,match='磁盘已断开'):
        with db.connect():pass
    assert not db.DATA.exists()


def test_ubuntu_zstd_deb_and_runtime_symlink_materialization(tmp_path):
    if not shutil.which('zstd'):pytest.skip('build-time zstd not installed')
    import io,tarfile
    spec=importlib.util.spec_from_file_location('portable_builder',ROOT/'scripts/build-portable-runtimes.py')
    builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w') as t:
        member=tarfile.TarInfo('./usr/bin/git');member.size=3;t.addfile(member,io.BytesIO(b'git'))
    compressed=subprocess.run(['zstd','--compress','--stdout'],input=stream.getvalue(),capture_output=True,check=True).stdout
    header=('data.tar.zst/'.ljust(16)+'0'.ljust(12)+'0'.ljust(6)+'0'.ljust(6)+'100644'.ljust(8)+str(len(compressed)).ljust(10)+'`\n').encode()
    assert len(header)==60
    deb=tmp_path/'git.deb';deb.write_bytes(b'!<arch>\n'+header+compressed+(b'\n' if len(compressed)%2 else b''))
    with builder.deb_data(deb) as t:assert t.extractfile('./usr/bin/git').read()==b'git'
    original=tmp_path/'runtime.tar';flattened=tmp_path/'flat.tar'
    with tarfile.open(original,'w') as t:
        member=tarfile.TarInfo('python/bin/python3.12');member.size=3;member.mode=0o755;t.addfile(member,io.BytesIO(b'bin'))
        link=tarfile.TarInfo('python/bin/python3');link.type=tarfile.SYMTYPE;link.linkname='python3.12';t.addfile(link)
    with tarfile.open(original) as src,tarfile.open(flattened,'w') as out:builder.regularized(src,out)
    with tarfile.open(flattened) as t:
        assert t.getmember('python/bin/python3').isfile()
        assert t.extractfile('python/bin/python3').read()==b'bin'
