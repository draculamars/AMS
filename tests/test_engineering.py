import io
import json
import subprocess
import sys
import os
from pathlib import Path
import pytest
from asset_manager import db,core,project_index as index,git_history as history,project_sync
from test_assets import env,client,HEADERS,run_git,setup_git
from test_models import put


def make_history(root):
    setup_git(root)
    put(root,'README.md','# Test\n[Model](models/part.stl)\n[Notes](docs/notes.md)')
    put(root,'docs/notes.md','# Notes\nFirst design iteration\n')
    put(root,'models/part.stl','solid first\nendsolid')
    run_git(root,'add','.');run_git(root,'commit','-m','Initial design');first=run_git(root,'rev-parse','HEAD')
    put(root,'docs/notes.md','# Notes\nSecond design iteration\n');run_git(root,'add','.');run_git(root,'commit','-m','Revise notes');second=run_git(root,'rev-parse','HEAD')
    run_git(root,'tag','v1',first)
    return first,second


def test_structured_index_empty_directories_relations_search_and_sampling(env,client):
    put(env,'README.md','# 项目\n[Design](docs/design.md)\n[Part](models/a.stl)\n[Missing](absent.txt)')
    put(env,'docs/design.md','# 结构设计\n电机与手爪联动参数。')
    put(env,'models/a.stl','solid a\nendsolid')
    (env/'empty').mkdir()
    state=index.index_one('p')
    root=client.get('/api/engineering/p/tree').json()
    assert {'empty','docs','models','README.md'}=={x['name'] for x in root['items']}
    assert next(x for x in root['items'] if x['name']=='empty')['files']==0
    links=client.get('/api/engineering/p/relations').json()['items']
    assert len(links)==3 and sum(x['status']=='missing' for x in links)==1
    doc=client.get('/api/engineering/p/documents?q=联动').json()
    assert doc['total']==1 and doc['items'][0]['title']=='结构设计'
    first=state['latest_run'];put(env,'docs/new.md','new');(env/'models/a.stl').unlink()
    second=index.index_one('p');changes=index.read_run(second['latest_run'])
    assert changes['added']==['docs/new.md'] and changes['removed']==['models/a.stl']
    assert any(x['path']=='models/a.stl' for x in index.read_run(first)['files'])
    assert client.get('/api/engineering/p/tree?path=../outside').status_code==400


def test_git_history_trees_diff_and_original_checkout_unchanged(env,client):
    first,second=make_history(env);state=index.index_one('p');overview=index.overview('p');repo=overview['repositories'][0]['repository_id']
    before=run_git(env,'status','--porcelain');branch=run_git(env,'symbolic-ref','HEAD')
    response=client.get(f'/api/repositories/{repo}/commits').json()
    assert {x['oid'] for x in response['items']}=={first,second}
    tree=client.get(f'/api/repositories/{repo}/tree',params={'ref':first,'path':'docs'}).json()
    assert tree['items'][0]['path']=='docs/notes.md'
    diff=client.get(f'/api/repositories/{repo}/changes',params={'ref':second,'path':'docs/notes.md'}).json()
    assert '-First design iteration' in diff['diff'] and '+Second design iteration' in diff['diff']
    old=client.get(f'/api/repositories/{repo}/file',params={'ref':first,'path':'docs/notes.md'})
    assert old.status_code==200 and b'First design' in old.content
    assert client.get(f'/api/repositories/{repo}/file',params={'ref':first,'path':'../.ssh/id_rsa'}).status_code==400
    assert run_git(env,'status','--porcelain')==before and run_git(env,'symbolic-ref','HEAD')==branch
    assert run_git(env,'rev-parse','HEAD')==second


def test_worktrees_share_repository_but_retain_distinct_heads(env,tmp_path):
    first,second=make_history(env);other=tmp_path/'second-checkout';run_git(env,'worktree','add','--detach',str(other),first)
    core.import_projects({'projects':[{'projectId':'p2','label':'Other','path':str(other)}]})
    index.index_one('p');index.index_one('p2')
    a=index.overview('p')['repositories'][0];b=index.overview('p2')['repositories'][0]
    assert a['repository_id']==b['repository_id'] and a['head']==second and b['head']==first


def test_historical_model_dependencies_use_same_commit(env,client):
    setup_git(env)
    put(env,'robot/arm.urdf','<robot name="test"><link name="arm"><visual><geometry><mesh filename="meshes/a.stl"/></geometry></visual></link></robot>')
    put(env,'robot/meshes/a.stl','solid original\nendsolid');run_git(env,'add','.');run_git(env,'commit','-m','Original robot');first=run_git(env,'rev-parse','HEAD')
    put(env,'robot/meshes/a.stl','solid new\nendsolid');run_git(env,'add','.');run_git(env,'commit','-m','New mesh')
    index.index_one('p');rid=index.overview('p')['repositories'][0]['repository_id']
    context=history.model_context(rid,first,'robot/arm.urdf')
    assert len(context['assets'])==2 and context['links'][0]['target']=='robot/meshes/a.stl'
    mesh=next(x for x in context['assets'] if x['ext']=='.stl')
    assert client.get(mesh['file_url']).content==b'solid original\nendsolid'


def test_unborn_repository_index_does_not_create_commit(env):
    setup_git(env);put(env,'notes.md','uncommitted');index.index_one('p')
    assert index.overview('p')['repositories'][0]['metadata']['commits']==0
    assert core.git_info(env)['head'] is None


def test_streamed_git_archive_is_independent_and_handles_shallow_history(env,tmp_path,monkeypatch):
    first,second=make_history(env)
    shallow=tmp_path/'shallow';subprocess.run(['git','clone','--depth=1',env.as_uri(),str(shallow)],capture_output=True,check=True)
    core.import_projects({'projects':[{'projectId':'shallow','label':'Shallow','path':str(shallow)}]})
    index.index_one('shallow');rid=index.overview('shallow')['repositories'][0]['repository_id']
    with db.connect() as c:repo=dict(c.execute('SELECT * FROM repositories WHERE id=?',(rid,)).fetchone())
    meta=json.loads(repo['body']);tips=sorted({r['oid'] for r in meta['refs']}|set(meta['heads']))
    packed=subprocess.run(history.command(shallow,'pack-objects','--stdout','--revs'),input=('\n'.join(tips)+'\n').encode(),capture_output=True,check=True,env=history.environment()).stdout
    remote=tmp_path/'remote';envvars={**os.environ,'ASSET_DATA_DIR':str(remote),'ASSET_BLOB_DIR':str(remote/'blobs')}
    result=subprocess.run([sys.executable,'-m','asset_manager.project_sync','receive-git',meta['fingerprint']],input=json.dumps(meta).encode()+b'\n'+packed,capture_output=True,env=envvars)
    assert result.returncode==0,result.stderr.decode()
    archive=remote/'git-history/objects'/meta['fingerprint']
    assert (archive/'shallow').is_file() and not (archive/'objects/info/alternates').exists()
    assert subprocess.check_output(['git','--git-dir='+str(archive),'show',second+':docs/notes.md']).decode().endswith('Second design iteration\n')


def test_sync_import_preserves_paths_metadata_versions_and_empty_dirs(env,tmp_path,monkeypatch):
    put(env,'docs/a.md','original');(env/'empty').mkdir();index.index_one('p')
    data,objects=project_sync.payload('p',{'assets':{},'objects':{},'runs':[]},lambda *a:None)
    remote=tmp_path/'remote';monkeypatch.setattr(db,'DATA',remote);db.init()
    for o in objects:
        with open(o['source'],'rb') as f:core.store_blob(f)
    result=project_sync.import_payload(data)
    assert result['files']==1 and index.overview('p')['index']['synced_at']
    with db.connect() as c:a=c.execute('SELECT * FROM assets').fetchone()
    assert core.source_path(a['id'])[0].read_text()=='original'
    assert 'empty' in {x['name'] for x in index.tree('p')['items']}


def test_partial_clone_retains_available_objects_and_explicit_gaps(env,tmp_path,monkeypatch,client):
    first,second=make_history(env);run_git(env,'config','uploadpack.allowFilter','true')
    partial=tmp_path/'partial'
    subprocess.run(['git','clone','--filter=blob:none','--no-checkout',env.as_uri(),str(partial)],capture_output=True,check=True)
    core.import_projects({'projects':[{'projectId':'partial','label':'Partial','path':str(partial)}]})
    index.index_one('partial');rid=index.overview('partial')['repositories'][0]['repository_id']
    with db.connect() as c:repo=dict(c.execute('SELECT * FROM repositories WHERE id=?',(rid,)).fetchone())
    meta=json.loads(repo['body']);assert meta['missing_count']>0
    lines=history.run(partial,'rev-list','--objects','--all','--missing=print','--no-object-names').decode().splitlines()
    packed=subprocess.run(history.command(partial,'pack-objects','--stdout'),input=('\n'.join(x for x in lines if not x.startswith('?'))+'\n').encode(),capture_output=True,check=True,env=history.environment()).stdout
    remote=tmp_path/'remote-partial';envvars={**os.environ,'ASSET_DATA_DIR':str(remote),'ASSET_BLOB_DIR':str(remote/'blobs')}
    result=subprocess.run([sys.executable,'-m','asset_manager.project_sync','receive-git',meta['fingerprint']],input=json.dumps(meta).encode()+b'\n'+packed,capture_output=True,env=envvars)
    assert result.returncode==0,result.stderr.decode()
    archive=remote/'git-history/objects'/meta['fingerprint']
    assert list((archive/'objects/pack').glob('*.promisor'))
    with db.connect() as c:
        c.execute('INSERT INTO repository_archives VALUES(?,?,?,?,?)',(rid,meta['fingerprint'],repo['body'],str(archive),core.now()))
    key=rid+'@'+meta['fingerprint']
    assert history.commits(key)['items'][0]['oid']==second
    tree=history.tree(key,second,'docs')
    assert tree['items'][0]['content_available'] is False
    with pytest.raises(ValueError,match='部分克隆'):history.blob_info(key,second,'docs/notes.md')


def test_sync_preserves_old_asset_versions_without_replacing_current_content(env,tmp_path,monkeypatch):
    put(env,'notes.md','original');core.scan_project('p')
    with db.connect() as c:aid=c.execute('SELECT id FROM assets').fetchone()[0]
    core.archive_asset(aid)
    core.upload('p','notes.md',io.BytesIO(b'updated'),aid)
    index.index_one('p')
    data,objects=project_sync.payload('p',{'assets':{},'objects':{},'runs':[]},lambda *a:None)
    assert len(data['histories'])==2
    remote=tmp_path/'remote';monkeypatch.setattr(db,'DATA',remote);db.init()
    for o in objects:
        with open(o['source'],'rb') as f:core.store_blob(f)
    project_sync.import_payload(data)
    with db.connect() as c:
        remote_id=c.execute('SELECT id FROM assets').fetchone()[0]
        events=[dict(x) for x in c.execute('SELECT * FROM versions ORDER BY id')]
    assert core.source_path(remote_id)[0].read_bytes()==b'updated'
    old=next(v for v in events if v['reason']=='source-history:snapshot')
    assert core.source_path(remote_id,old['id'])[0].read_bytes()==b'original'
    project_sync.import_payload(data)
    with db.connect() as c:assert c.execute('SELECT count(*) FROM versions').fetchone()[0]==len(events)


def test_backup_includes_index_and_git_history_and_survives_relocation(env,tmp_path,monkeypatch):
    first,second=make_history(env);index.index_one('p');rid=index.overview('p')['repositories'][0]['repository_id']
    with db.connect() as c:r=dict(c.execute('SELECT * FROM repositories WHERE id=?',(rid,)).fetchone())
    meta=json.loads(r['body']);key=meta['fingerprint'];archive=db.DATA/'git-history/objects'/key
    subprocess.run(['git','clone','--bare',str(env),str(archive)],capture_output=True,check=True)
    (archive/'ATLAS-GIT.json').write_text(r['body'])
    with db.connect() as c:c.execute('INSERT INTO repository_archives VALUES(?,?,?,?,?)',(rid,key,r['body'],str(archive),core.now()))
    target=tmp_path/'backup'
    result=subprocess.run([sys.executable,'-m','asset_manager','backup',str(target)],capture_output=True,env={**os.environ,'ASSET_DATA_DIR':str(db.DATA)})
    assert result.returncode==0,result.stderr.decode()
    manifest=json.loads((target/'manifest.json').read_text())
    assert manifest['git_history_snapshots']==1 and manifest['index_snapshots']==1
    archive.rename(archive.with_name('original-unavailable'))
    monkeypatch.setattr(db,'DATA',target);db.init()
    assert history.blob_bytes(rid+'@'+key,first,'docs/notes.md')[0]==b'# Notes\nFirst design iteration\n'
    package,name=history.bundle(rid+'@'+key)
    import zipfile
    with zipfile.ZipFile(package) as z:
        assert 'ATLAS-HISTORY-README.txt' in z.namelist()
        assert any(n.endswith('/ATLAS-GIT.json') for n in z.namelist())
    package.unlink()
