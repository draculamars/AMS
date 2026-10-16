import hashlib
import io
import json
import subprocess
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from asset_manager import db, core
from asset_manager.server import app

HEADERS={'X-Asset-Request':'1'}

@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DATA',tmp_path/'data')
    monkeypatch.delenv('ASSET_BLOB_DIR',raising=False)
    monkeypatch.setenv('ASSET_LOCAL_BOOTSTRAP','1')
    monkeypatch.setenv('ASSET_ALLOWED_HOSTS','localhost,127.0.0.1,testserver')
    monkeypatch.setenv('ASSET_RESERVE_BYTES','0')
    db.init()
    root=tmp_path/'project'; root.mkdir()
    core.import_projects({'projects':[{'projectId':'p','label':'项目测试','path':str(root)}]})
    return root

@pytest.fixture
def client(env):
    with TestClient(app) as c: yield c

def write_scan(root,name='报告.md',content=b'# Report\nOriginal'):
    (root/name).write_bytes(content); core.scan_project('p')
    with db.connect() as con: return con.execute('SELECT id FROM assets WHERE relpath=?',(name,)).fetchone()[0]

def run_git(root,*args):
    return subprocess.run(['git','-C',str(root),*args],capture_output=True,check=True).stdout.decode().strip()

def setup_git(root):
    run_git(root,'init','-b','main'); run_git(root,'config','user.name','Test User'); run_git(root,'config','user.email','test@example.invalid')

def test_real_snapshot_and_immutable_update(env,client):
    aid=write_scan(env)
    snap=core.archive_asset(aid)
    assert snap['sha256']==hashlib.sha256(b'# Report\nOriginal').hexdigest()
    res=client.post('/api/upload',headers=HEADERS,data={'project_id':'p','asset_id':aid},files={'file':('报告.md',b'Updated')})
    assert res.status_code==200,res.text
    assert (env/'报告.md').read_bytes()==b'# Report\nOriginal'
    assert client.get(f'/api/assets/{aid}/file').content==b'Updated'
    detail=client.get(f'/api/assets/{aid}').json(); old=detail['versions'][-1]
    assert client.get(f'/api/assets/{aid}/file?version={old["id"]}').content==b'# Report\nOriginal'
    (env/'报告.md').unlink(); core.scan_project('p')
    assert client.get(f'/api/assets/{aid}/file').content==b'Updated'
    assert client.get('/api/assets').json()['total']==1

def test_scan_rejects_secrets_symlinks_and_outside_access(env,client,tmp_path):
    aid=write_scan(env)
    (env/'.env').write_text('PASSWORD=secret')
    (env/'auth.json').write_text('{}')
    (env/'private.key').write_text('x')
    (env/'node_modules').mkdir(); (env/'node_modules'/'x.js').write_text('x')
    outside=tmp_path/'outside.txt'; outside.write_text('outside'); (env/'linked.txt').symlink_to(outside)
    core.scan_project('p')
    assert client.get('/api/assets').json()['total']==1
    with pytest.raises(ValueError): core.safe_path(env,'../outside.txt')
    with pytest.raises(ValueError): core.safe_path(env,'linked.txt')
    with pytest.raises(ValueError): core.safe_path(env,'.env')
    (env/'报告.md').unlink(); (env/'报告.md').symlink_to(outside)
    assert client.get(f'/api/assets/{aid}/file').status_code==400

def test_password_sessions_and_csrf(env,client,monkeypatch):
    write_scan(env)
    assert client.post('/api/scan',json={}).status_code==403
    assert client.post('/api/scan',json={},headers={**HEADERS,'Origin':'https://evil.invalid'}).status_code==403
    monkeypatch.setenv('ASSET_LOCAL_BOOTSTRAP','0')
    assert client.get('/api/assets').status_code==401
    assert client.post('/api/auth/setup',json={'password':'long-enough-test-password'},headers=HEADERS).status_code==200
    assert client.get('/api/assets').status_code==401
    assert client.post('/api/auth/login',json={'password':'wrong'},headers=HEADERS).status_code==401
    r=client.post('/api/auth/login',json={'password':'long-enough-test-password'},headers=HEADERS)
    assert r.status_code==200 and 'HttpOnly' in r.headers['set-cookie']
    assert client.get('/api/assets').status_code==200
    assert client.get('/api/health',headers={'Host':'evil.invalid'}).status_code==400
    assert client.post('/api/auth/logout',headers=HEADERS).status_code==200
    assert client.get('/api/assets').status_code==401

def test_new_upload_unicode_preview_and_metadata(env,client):
    r=client.post('/api/upload',headers=HEADERS,data={'project_id':'p'},files={'file':('数据.csv','名称,值\n电压,3.3\n'.encode())})
    assert r.status_code==200,r.text
    aid=r.json()['asset_id']
    assert client.get(f'/api/assets/{aid}/preview').json()['rows'][1]==['电压','3.3']
    assert client.patch(f'/api/assets/{aid}',headers=HEADERS,json={'tags':'电气,交付','note':'验收结果'}).status_code==200
    assert client.get('/api/assets',params={'q':'交付'}).json()['total']==1
    assert client.get('/api/assets',params={'q':'%'}).json()['total']==0
    core.scan_project('p')
    assert client.get('/api/assets').json()['total']==1
    assert client.post('/api/upload',headers=HEADERS,data={'project_id':'no'},files={'file':('x.txt',b'x')}).status_code==400

def test_git_plan_requires_exact_approval_and_revalidates(env,client):
    setup_git(env); write_scan(env,'source.py',b'print("original")\n')
    (env/'.gitignore').write_text('ignored.txt\n'); (env/'ignored.txt').write_text('x'); (env/'model.stl').write_bytes(b'abc'); core.scan_project('p')
    p=core.git_plan('p')
    assert {f['path'] for f in p['files']}=={'source.py','.gitignore'}
    with pytest.raises(ValueError): core.execute_plan(p['id'],'yes')
    (env/'source.py').write_text('changed')
    with pytest.raises(ValueError,match='已变化'): core.execute_plan(p['id'],'确认目录、名称、分支和文件清单并执行首次提交')
    assert core.git_info(env)['head'] is None
    core.scan_project('p'); p=core.git_plan('p',branch='archive/main')
    result=core.execute_plan(p['id'],'确认目录、名称、分支和文件清单并执行首次提交')
    assert len(result['head'])==40
    assert run_git(env,'branch','--show-current')=='archive/main'
    assert set(run_git(env,'ls-files').splitlines())=={'.gitignore','source.py'}
    with pytest.raises(ValueError): core.execute_plan(p['id'],'确认目录、名称、分支和文件清单并执行首次提交')

def test_nested_repository_classification(env):
    setup_git(env)
    nested=env/'vendor'; nested.mkdir(); setup_git(nested)
    (nested/'lib.py').write_text('lib'); run_git(nested,'add','lib.py'); run_git(nested,'commit','-m','lib')
    (env/'own.py').write_text('own'); core.scan_project('p')
    with db.connect() as con:
        rows={r['relpath']:dict(r) for r in con.execute('SELECT * FROM assets')}
    assert rows['vendor/lib.py']['git_state']=='tracked'
    assert rows['own.py']['git_state']=='untracked'
    p=core.git_plan('p'); assert [f['path'] for f in p['files']]==['own.py']

def test_file_preview_never_executes_html(env,client):
    aid=write_scan(env,'unsafe.html',b'<script>alert(1)</script>')
    r=client.get(f'/api/assets/{aid}/file?inline=true')
    assert 'attachment' in r.headers['content-disposition']
    assert r.headers['content-type']=='application/octet-stream'
    assert client.get(f'/api/assets/{aid}/preview').json()['type']=='text'

def test_disk_capacity_guard_precedes_archive(env,monkeypatch):
    aid=write_scan(env)
    monkeypatch.setenv('ASSET_RESERVE_BYTES',str(10**18))
    with pytest.raises(ValueError,match='空间不足'):core.archive_many(lambda *a:None,[aid])
    with db.connect() as con: assert con.execute('SELECT COUNT(*) FROM versions').fetchone()[0]==0

def test_migration_proposal_does_not_move_files(env,client,tmp_path):
    write_scan(env)
    target=tmp_path/'vault'
    r=client.post('/api/migration/plan',headers=HEADERS,json={'root':str(target)})
    assert r.status_code==200
    assert r.json()['status']=='proposal_only'
    assert not target.exists() and (env/'报告.md').exists()

def test_export_is_downloadable_with_checksums(env,client):
    aid=write_scan(env)
    r=client.get('/api/export/project/p')
    assert r.status_code==200
    import zipfile
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        manifest=json.loads(z.read('MANIFEST.json'))
        assert manifest['files'][0]['sha256']==hashlib.sha256(z.read('files/报告.md')).hexdigest()
    assert not list((db.DATA/'tmp').glob('*.zip'))

def test_archived_offline_assets_remain_in_dashboard(env,client):
    aid=write_scan(env);core.archive_asset(aid);(env/'报告.md').unlink();core.scan_project('p')
    assert client.get('/api/stats').json()['assets']==1
    assert client.get('/api/projects').json()[0]['assets']==1
    assert client.get(f'/api/assets/{aid}/file').status_code==200

def test_remote_receiver_checks_hash_and_rejects_tar_escape(tmp_path):
    import tarfile
    import sys
    root=tmp_path/'archive'; script=Path(__file__).parents[1]/'scripts/remote-receiver.py'
    data=b'verified-object';sha=hashlib.sha256(data).hexdigest()
    def tar(name,content):
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w') as z:
            info=tarfile.TarInfo(name);info.size=len(content);z.addfile(info,io.BytesIO(content))
        return stream.getvalue()
    valid=subprocess.run([sys.executable,str(script),'receive',str(root)],input=tar(sha[:2]+'/'+sha,data),capture_output=True)
    assert valid.returncode==0,valid.stderr
    assert (root/sha[:2]/sha).read_bytes()==data
    bad=subprocess.run([sys.executable,str(script),'receive',str(root)],input=tar(sha[:2]+'/'+sha,b'changed'),capture_output=True)
    assert bad.returncode!=0 and (root/sha[:2]/sha).read_bytes()==data
    escape=subprocess.run([sys.executable,str(script),'receive',str(root)],input=tar('../escape',data),capture_output=True)
    assert escape.returncode!=0 and not (tmp_path/'escape').exists()

def test_reuploading_old_content_becomes_current_version(env,client):
    a=client.post('/api/upload',headers=HEADERS,data={'project_id':'p'},files={'file':('version.txt',b'A')}).json()['asset_id']
    for content in [b'B',b'A',b'B']:
        response=client.post('/api/upload',headers=HEADERS,data={'project_id':'p','asset_id':a},files={'file':('version.txt',content)})
        assert response.status_code==200
        assert client.get(f'/api/assets/{a}/file').content==content
    assert len(client.get(f'/api/assets/{a}').json()['versions'])==4

def test_backup_can_restore_archive_without_original_project(env,client,tmp_path,monkeypatch):
    import os,sqlite3,sys
    aid=write_scan(env);snap=core.archive_asset(aid)
    backup=tmp_path/'backup'
    result=subprocess.run([sys.executable,'-m','asset_manager','backup',str(backup)],cwd=Path(__file__).parents[1],env={**os.environ,'ASSET_DATA_DIR':str(db.DATA)},capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    with sqlite3.connect(backup/'catalog.sqlite3') as con:
        assert con.execute('SELECT COUNT(*) FROM versions').fetchone()[0]==1
    (env/'报告.md').unlink()
    monkeypatch.setattr(db,'DATA',backup)
    # No rescan is needed for a restored archive to serve its saved bytes.
    path,a=core.source_path(aid)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==snap['sha256']

def test_remote_import_matches_paths_without_overwriting_upload_ids(env,client,tmp_path):
    import os,sys
    aid=write_scan(env);snap=core.archive_asset(aid)
    upload=core.upload('p','remote.txt',io.BytesIO(b'remote-owned'))
    a=core.asset_row(aid)
    a.update(id=upload['asset_id'],relpath='new-source.txt',name='new-source.txt',sha256=snap['sha256'])
    with db.connect() as con:projects=[dict(r) for r in con.execute('SELECT * FROM projects')]
    manifest=tmp_path/'import.json'
    manifest.write_text(json.dumps({'created_at':core.now(),'projects':projects,'assets':[a],'objects':[snap],'errors':[]}))
    script=Path(__file__).parents[1]/'scripts/import-remote-manifest.py'
    result=subprocess.run([sys.executable,str(script),str(manifest),'--verify'],env={**os.environ,'ASSET_DATA_DIR':str(db.DATA)},capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert client.get(f'/api/assets/{upload["asset_id"]}/file').content==b'remote-owned'
    with db.connect() as con:new=con.execute("SELECT id FROM assets WHERE relpath='new-source.txt'").fetchone()[0]
    assert new!=upload['asset_id']
    assert client.get(f'/api/assets/{new}/file').content==b'# Report\nOriginal'

def test_remote_sender_stops_and_records_lost_connection(tmp_path):
    import os,sys
    # A local SSH stub simulates a disconnected receiver without a real network.
    ssh=tmp_path/'ssh'
    ssh.write_text('#!/bin/sh\ncase "$*" in *inventory*) exit 0;; *) exit 37;; esac\n')
    ssh.chmod(0o700)
    source=tmp_path/'large.bin';source.write_bytes(b'x'*(4*1024*1024))
    manifest=tmp_path/'manifest.json'
    manifest.write_text(json.dumps({'objects':[{'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'source_path':str(source),'size':source.stat().st_size,'mtime_ns':source.stat().st_mtime_ns}]}))
    script=Path(__file__).parents[1]/'scripts/send-remote-archive.py'
    result=subprocess.run([sys.executable,str(script),'--host','fixture','--root','/archive',str(manifest)],env={**os.environ,'PATH':str(tmp_path)+os.pathsep+os.environ['PATH']},capture_output=True,text=True,timeout=15)
    assert result.returncode!=0
    progress=json.loads((tmp_path/'remote-transfer-progress.json').read_text())
    assert progress['complete'] is False
    assert progress['sent']==0
    assert progress['error']
