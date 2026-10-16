import hashlib
import json
import sqlite3
from pathlib import Path
import pytest
from test_engineering import env, client, put
from asset_manager import core, db, node, project_index, project_sync, provenance, source_edit

def setting(key,value):
    with db.connect() as c:c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,value))

def history_source(tmp_path,project):
    root=tmp_path/'codex';root.mkdir()
    with sqlite3.connect(root/'state_5.sqlite') as c:
        c.execute('CREATE TABLE threads(id,cwd,title,updated_at,git_sha,git_branch,archived)')
        c.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?)',('t',str(project),'Design the robot',100,None,'main',0))
    with sqlite3.connect(root/'thread_history_1.sqlite') as c:
        c.execute('CREATE TABLE thread_turns(thread_id,turn_id,rollout_ordinal,status,started_at,completed_at,duration_ms)')
        c.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?,?,?)',('t','turn',1,'completed',90,100,10000))
        c.execute('CREATE TABLE thread_items(thread_id,turn_id,item_id,rollout_ordinal,updated_at_ordinal,item_type,item_json)')
        events=[{'type':'userMessage','content':[{'type':'text','text':'Resize bracket. api_key=secret-value'}]},
                {'type':'agentMessage','phase':'final','text':'Changed bracket height and checked export.'},
                {'type':'fileChange','status':'completed','changes':[{'path':str(project/'bracket.py'),'kind':{'type':'update'},'diff':'-height=1\n+height=2'}]},
                {'type':'fileChange','status':'failed','changes':[{'path':str(project/'attempt.py'),'kind':{'type':'update'},'diff':'+attempt'}]},
                {'type':'commandExecution','status':'completed','command':'pytest','exitCode':0,'durationMs':42},
                {'type':'reasoning','text':'MUST NEVER BE INDEXED'},
                {'type':'fileChange','status':'completed','changes':[{'path':str(project/'.env'),'kind':{'type':'update'},'diff':'+password=private'}]}]
        for i,e in enumerate(events):c.execute('INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)',('t','turn',str(i),i,i,e['type'],json.dumps(e)))
    setting('codex_source_root',str(root));return root

def test_codex_projection_readonly_provenance_and_redaction(env,tmp_path):
    root=history_source(tmp_path,env);before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}
    r=provenance.ingest();assert r['sessions_updated']==1
    data=provenance.listing('p');assert data['total']==1
    t=data['items'][0];assert t['body']['operations'][0]['exit_code']==0
    assert len(t['artifacts'])==2 and t['artifacts'][1]['body']['status']=='completed'
    output=json.dumps(data);assert 'MUST NEVER' not in output and 'secret-value' not in output and 'password=private' not in output
    assert '[REDACTED]' in output and 'Changed bracket' in output
    assert provenance.ingest()['sessions_updated']==0
    assert before=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}

def test_provenance_schema_failure_is_visible(env,tmp_path):
    root=tmp_path/'bad';root.mkdir()
    for n in ['state_5.sqlite','thread_history_1.sqlite']:sqlite3.connect(root/n).close()
    setting('codex_source_root',str(root))
    with pytest.raises(ValueError,match='格式已变化'):provenance.ingest()

def test_source_edit_conflict_then_backup_and_replay_protection(env):
    put(env,'notes.md','original\n');path=env/'notes.md';core.scan_project('p');node.initialize()
    with db.connect() as c:aid=c.execute("SELECT id FROM assets WHERE relpath='notes.md'").fetchone()[0]
    state=source_edit.read(aid);plan=source_edit.plan(aid,{'sha256':state['sha256'],'text':'changed\n','note':'clarify'})
    path.write_text('external\n')
    with pytest.raises(ValueError,match='已变化'):source_edit.apply(aid,{'plan_id':plan['id'],'acknowledgement':'确认写入本节点原文件'})
    assert path.read_text()=='external\n'
    state=source_edit.read(aid);plan=source_edit.plan(aid,{'sha256':state['sha256'],'text':'changed\n'})
    result=source_edit.apply(aid,{'plan_id':plan['id'],'acknowledgement':'确认写入本节点原文件'})
    assert result['applied'] and path.read_text()=='changed\n'
    sha=result['backup_sha256'];assert (db.blob_root()/sha[:2]/sha).read_bytes()==b'external\n'
    with pytest.raises(ValueError,match='已经执行'):source_edit.apply(aid,{'plan_id':plan['id'],'acknowledgement':'确认写入本节点原文件'})

def test_edits_reject_archive_and_symlinks(env,tmp_path):
    put(env,'notes.md','safe');path=env/'notes.md';core.scan_project('p');node.initialize()
    with db.connect() as c:aid=c.execute("SELECT id FROM assets WHERE relpath='notes.md'").fetchone()[0]
    external=tmp_path/'external';external.write_text('private');path.unlink();path.symlink_to(external)
    with pytest.raises((ValueError,FileNotFoundError)):source_edit.read(aid)
    assert external.read_text()=='private'
    with db.connect() as c:c.execute("UPDATE project_locations SET mode='archive' WHERE project_id='p'")
    with pytest.raises(ValueError,match='本节点在线'):source_edit.read(aid)

def test_live_reconcile_add_delete_and_no_duplicate_index(env):
    put(env,'a.md','one');project_index.index_one('p');node.initialize()
    assert node.reconcile('p')['changed'] is False
    put(env,'b.md','two');assert node.reconcile('p')['changed'] is True
    assert node.reconcile('p')['changed'] is False
    (env/'a.md').unlink();assert node.reconcile('p')['changed'] is True
    assert {r['path'] for r in project_index.tree('p')['items']}=={'b.md'}

def test_current_tree_excludes_removed_but_history_remains(env):
    put(env,'old.md','old');path=env/'old.md';core.scan_project('p')
    with db.connect() as c:aid=c.execute("SELECT id FROM assets WHERE relpath='old.md'").fetchone()[0]
    core.archive_asset(aid);path.unlink();core.scan_project('p')
    assert project_index.tree('p')['items']==[]
    assert project_index.tree('p',scope='all')['items'][0]['path']=='old.md'

def test_mirror_current_pointer_can_revert_to_earlier_blob(env):
    put(env,'a.md','A');path=env/'a.md';core.scan_project('p')
    with db.connect() as c:aid=c.execute("SELECT id FROM assets WHERE relpath='a.md'").fetchone()[0]
    a=core.archive_asset(aid);path.write_text('B');b=core.archive_asset(aid);path.unlink()
    with db.connect() as c:
        c.execute('UPDATE assets SET present=0 WHERE id=?',(aid,))
        c.execute('INSERT INTO mirror_files VALUES(?,?,?,?)',('p','a.md',a['sha256'],a['size']))
    assert core.source_path(aid)[0].read_text()=='A'
    assert node.current_paths('p')=={'a.md'}
    assert project_sync.remote_inventory('p')['assets']['a.md']['sha']==a['sha256']
    # Repeat synchronization must reuse the current A pointer, not the later-inserted B history.
    with db.connect() as c:c.execute('UPDATE mirror_files SET sha256=? WHERE project_id=?',(b['sha256'],'p'))
    assert project_sync.remote_inventory('p')['assets']['a.md']['sha']==b['sha256']

def test_workbench_and_development_api(env,tmp_path,client):
    put(env,'notes.md','Overview');core.scan_project('p');node.initialize();history_source(tmp_path,env);provenance.ingest()
    data=client.get('/api/workbench').json();assert data['projects'][0]['counts']['current_files']==1
    assert data['development']['total']==1
    graph=client.get('/api/development-graph/p').json();assert {e['kind'] for e in graph['edges']}=={'changed','attempted'}
    assert client.get('/api/knowledge-search',params={'q':'bracket'}).json()['items']
    r=client.post('/api/node/configure',json={'label':'test-host'},headers={'X-Asset-Request':'1'});assert r.status_code==200
    assert client.get('/api/node').json()['node']['label']=='test-host'

def test_mutation_lock_serializes_other_processes(env,tmp_path):
    import os,subprocess,sys,time
    marker=tmp_path/'lock-acquired'
    code='from asset_manager import core; from pathlib import Path;\nwith core.MUTATION_LOCK: Path('+repr(str(marker))+').write_text("acquired")'
    child=None
    try:
        with core.MUTATION_LOCK:
            with core.MUTATION_LOCK:
                child=subprocess.Popen([sys.executable,'-c',code],env={**os.environ,'ASSET_DATA_DIR':str(db.DATA)})
                time.sleep(.3)
                assert not marker.exists()
        assert child.wait(timeout=10)==0
        assert marker.read_text()=='acquired'
    finally:
        if child and child.poll() is None:child.terminate();child.wait()

def test_explicit_conversation_file_links_are_not_modification_proof(env,tmp_path):
    put(env,'bracket.stl','solid bracket\nendsolid');core.scan_project('p')
    turn={'status':'completed','completed_at':100,'duration_ms':20};session={'cwd':str(env)}
    item={'type':'agentMessage','phase':'final','text':'See [model](bracket.stl) and `unrelated.stl`.'}
    body,artifacts=provenance.parse_turn(turn,[{'item_id':'message','item_json':json.dumps(item)}],session,[{'id':'p','path':str(env)}],{('p','bracket.stl')})
    assert len(artifacts)==1 and artifacts[0]['action']=='mentioned'
    assert artifacts[0]['body']['status']=='referenced' and artifacts[0]['body']['diff']==''

def test_development_metadata_sync_is_idempotent_and_preserves_assets(env,tmp_path):
    put(env,'notes.md','original');core.scan_project('p');history_source(tmp_path,env);provenance.ingest()
    data={'project_id':'p','development':provenance.export_project('p')}
    with db.connect() as c:before=[dict(r) for r in c.execute('SELECT * FROM assets')]
    assert provenance.receive_project(data)['turns']==1
    assert provenance.receive_project(data)['turns']==1
    with db.connect() as c:
        assert [dict(r) for r in c.execute('SELECT * FROM assets')]==before
        assert c.execute('SELECT COUNT(*) FROM development_turns').fetchone()[0]==1
    with pytest.raises(ValueError,match='先同步工程'):provenance.receive_project({**data,'project_id':'absent'})

def test_rollout_public_event_fallback_skips_reasoning_and_syncs_session_metadata(env,tmp_path):
    root=history_source(tmp_path,env)
    with sqlite3.connect(root/'thread_history_1.sqlite') as c:c.execute('DELETE FROM thread_items');c.execute('DELETE FROM thread_turns')
    path=root/'rollout.jsonl'
    events=[{'type':'event_msg','timestamp':'2026-01-01T00:00:00Z','payload':{'type':'task_started','turn_id':'old'}},
      {'type':'event_msg','payload':{'type':'item_completed','thread_id':'t','turn_id':'old','item':{'type':'userMessage','id':'u','content':[{'type':'text','text':'Restore older model'}]}}},
      {'type':'event_msg','payload':{'type':'item_completed','thread_id':'t','turn_id':'old','item':{'type':'reasoning','id':'hidden','text':'NOT PUBLIC'}}},
      {'type':'event_msg','payload':{'type':'item_completed','thread_id':'t','turn_id':'old','item':{'type':'agentMessage','id':'answer','phase':'final','text':'Model restored'}}},
      {'type':'event_msg','timestamp':'2026-01-01T00:01:00Z','payload':{'type':'task_complete','turn_id':'old'}}]
    path.write_text('\n'.join(json.dumps(e) for e in events))
    with sqlite3.connect(root/'state_5.sqlite') as c:
        c.execute('ALTER TABLE threads ADD COLUMN rollout_path');c.execute('UPDATE threads SET rollout_path=?',(str(path),))
        c.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)',('empty',str(env),'Metadata only',100,None,None,0,''))
    provenance.ingest();data=provenance.listing('p')
    assert data['total']==1 and data['items'][0]['body']['source']=='codex-rollout-public-v1'
    assert data['items'][0]['body']['status']=='completed' and 'NOT PUBLIC' not in json.dumps(data)
    assert len(provenance.export_project('p')['development_sessions'])==2
    assert provenance.ingest()['sessions_updated']==0
    assert provenance.rollout_turns(tmp_path/'outside.jsonl',root,'t')==[]

def test_interrupted_index_retries_after_file_metadata_was_updated(env,monkeypatch):
    put(env,'notes.md','before');project_index.index_one('p');node.initialize();node.reconcile('p')
    put(env,'notes.md','after interruption')
    discover=project_index.git_history.discover
    def crash(*args):raise RuntimeError('simulated interruption after filesystem scan')
    monkeypatch.setattr(project_index.git_history,'discover',crash)
    with pytest.raises(RuntimeError):project_index.index_one('p')
    assert db.setting('watch_dirty:p')=='1'
    monkeypatch.setattr(project_index.git_history,'discover',discover)
    assert node.reconcile('p')['changed'] is True
    assert db.setting('watch_dirty:p') is None
    assert node.reconcile('p')['changed'] is False
