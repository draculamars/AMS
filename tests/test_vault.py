import hashlib,json,os,sqlite3,subprocess
from pathlib import Path
import pytest
from test_assets import env,client,HEADERS,setup_git,run_git
from asset_manager import db,vault,knowledge,node

@pytest.fixture
def recovery(env,tmp_path):
    codex=tmp_path/'codex';(codex/'skills/drawing').mkdir(parents=True)
    (codex/'skills/drawing/SKILL.md').write_text('---\nname: drawing\n---\nDraw the component. [Reference](reference.md)\n')
    (codex/'skills/drawing/reference.md').write_text('reference')
    (codex/'auth.json').write_text('{"token":"SECRET-RAW-AUTH-FOR-RECOVERY"}')
    (codex/'archived_sessions').mkdir();(codex/'archived_sessions/chat.jsonl').write_text('{"message":"original transcript"}\n')
    (codex/'config.toml').write_text('[mcp_servers.docs]\ncommand="SECRET-COMMAND"\n[mcp_servers.docs.env]\nTOKEN="SECRET-CONFIG-TOKEN"\n')
    con=sqlite3.connect(codex/'example.sqlite');con.execute('PRAGMA journal_mode=WAL');con.execute('CREATE TABLE sample(value)');con.execute("INSERT INTO sample VALUES('saved')");con.commit()
    (env/'README.md').write_text('uncommitted document')
    p=vault.configure('Recovery',str(tmp_path/'repository'),[str(env)],str(codex))
    yield p,codex,con
    con.close()

@pytest.fixture
def real_restic(monkeypatch):
    path=os.environ.get('ATLAS_RESTIC')
    if not path:pytest.skip('ATLAS_RESTIC required for encrypted integration test')
    assert Path(path).is_file()
    return path

def test_profile_keeps_key_private_and_includes_shared_git(recovery,env,tmp_path,client):
    p,codex,_=recovery;key=Path(p['recovery_key_file']);assert key.stat().st_mode&0o777==0o600
    setup_git(env);run_git(env,'add','.');run_git(env,'commit','-m','fixture')
    wt=codex/'worktrees/one/work';wt.parent.mkdir(parents=True);run_git(env,'worktree','add','--detach',str(wt))
    plan=vault.plan(p['id']);assert any(x['common_dir']==str(env/'.git') for x in plan['worktrees'])
    assert str(key) in plan['excluded']
    body=client.get('/api/vault').text;assert key.read_text().strip() not in body and 'password_file' not in body
    assert client.post('/api/vault/restores/bad/apply',headers=HEADERS,json={}).status_code==400

def test_sqlite_wal_snapshot_is_consistent_and_private(recovery,tmp_path):
    p,codex,con=recovery;r=vault.plan(p['id']);stage=tmp_path/'stage';vault.stage(vault.profile(p['id']),r,stage)
    item=next(x for x in r['databases'] if x['source']==str(codex/'example.sqlite'))
    with sqlite3.connect(item['snapshot_path']) as c:assert c.execute('SELECT value FROM sample').fetchone()[0]=='saved'
    con.execute("INSERT INTO sample VALUES('later')");con.commit()
    with sqlite3.connect(item['snapshot_path']) as c:assert c.execute('SELECT COUNT(*) FROM sample').fetchone()[0]==1
    assert str(codex/'example.sqlite-wal') in r['excluded']

def test_real_encrypted_backup_versions_check_and_isolated_restore(recovery,env,tmp_path,real_restic):
    p,codex,_=recovery;pid=p['id']
    package=env/'linked-skill';package.mkdir();(package/'SKILL.md').write_text('original linked skill')
    alias=codex/'skills/a-alias';alias.symlink_to(package,target_is_directory=True)
    vault.initialize(pid)
    result=vault.backup(lambda *a:None,pid);sid=result['id'];s=vault.snapshot(sid)
    assert result['status']=='complete'
    assert vault.verify(lambda *a:None,sid,True)['status']=='passed'
    assert vault.drill(lambda *a:None,sid)['status']=='passed'
    assert len(vault.restore_plan(sid,[str(codex)])['database_mappings'])==1
    database_plan=vault.restore_plan(sid,[str(codex/'example.sqlite')])
    assert database_plan['database_mappings'][0]['source']==str(codex/'example.sqlite')
    database_restore=vault.restore(lambda *a:None,database_plan['id'])
    restored_db=Path(database_restore['target'])/database_plan['paths'][0].lstrip('/')
    with sqlite3.connect(restored_db) as c:assert c.execute('SELECT value FROM sample').fetchone()[0]=='saved'
    with pytest.raises(ValueError):vault.restore(lambda *a:None,vault.restore_plan(sid,[str(alias/'SKILL.md')])['id'])
    # Original credential data is recoverable but never sent to the web catalog.
    plan=vault.restore_plan(sid,[str(codex/'auth.json')]);r=vault.restore(lambda *a:None,plan['id'])
    target=Path(r['target'])/str(codex/'auth.json').lstrip('/')
    assert target.read_bytes()==(codex/'auth.json').read_bytes()
    assert 'SECRET-RAW-AUTH' not in json.dumps(vault.export_metadata())
    (env/'README.md').write_text('new version')
    second=vault.backup(lambda *a:None,pid);assert second['snapshot']!=result['snapshot']
    r=vault.restore(lambda *a:None,vault.restore_plan(sid,[str(env/'README.md')])['id'])
    assert (Path(r['target'])/str(env/'README.md').lstrip('/')).read_text()=='uncommitted document'
    assert (env/'README.md').read_text()=='new version'
    missing=vault.restore_plan(sid,[str(env/'README.md'),str(env/'absent')])
    with pytest.raises(ValueError,match='部分选定'):vault.restore(lambda *a:None,missing['id'])
    with pytest.raises(ValueError,match='已执行'):vault.restore(lambda *a:None,plan['id'])
    with pytest.raises(ValueError,match='超出'):vault.restore_plan(sid,['/etc/passwd'])
    rc,events,errors=vault.run(vault.profile(pid),['ls',result['snapshot'],str(db.DATA/'private')]);assert rc==0
    assert not any('vault-'+pid+'.key' in json.dumps(x) for x in events)

def test_partial_backup_never_claims_complete(recovery,monkeypatch):
    p,_,_=recovery
    monkeypatch.setattr(vault,'run',lambda *a,**kw:(3,[{'message_type':'summary','snapshot_id':'a'*64}],['unreadable file']))
    with pytest.raises(ValueError,match='未完整'):vault.backup(lambda *a:None,p['id'])
    s=vault.overview()['snapshots'][0];assert s['status']=='partial' and s['body']['exit_code']==3

def test_failed_database_snapshot_does_not_publish(recovery,monkeypatch):
    p,_,_=recovery
    def fail(*a):raise ValueError('database busy')
    monkeypatch.setattr(vault,'stage',fail)
    with pytest.raises(ValueError,match='busy'):vault.backup(lambda *a:None,p['id'])
    assert vault.overview()['snapshots'][0]['status']=='failed'

def test_knowledge_config_redaction_versions_and_dependencies(recovery,monkeypatch):
    _,codex,_=recovery
    monkeypatch.setattr(knowledge,'candidates',lambda:iter([(codex/'config.toml','configuration'),(codex/'skills/drawing/SKILL.md','skill')]))
    assert knowledge.scan()['items']==2
    data=knowledge.listing();assert data['total']==2
    config=next(x for x in data['items'] if x['kind']=='configuration');item=knowledge.detail(config['id'])
    assert 'SECRET-CONFIG' not in json.dumps(item) and 'SECRET-COMMAND' not in json.dumps(item)
    skill=next(x for x in data['items'] if x['kind']=='skill');item=knowledge.detail(skill['id']);assert item['body']['references'][0]['exists']
    assert item['body']['resource_count']==2
    path=codex/'skills/drawing/SKILL.md';original=path.read_text();path.write_text(original+'\nNew instructions\n');knowledge.scan()
    item=knowledge.detail(skill['id']);assert len(item['versions'])==2 and '+New instructions' in item['diff']
    path.write_text(original);knowledge.scan()
    assert '-New instructions' in knowledge.detail(skill['id'])['diff']

def test_metadata_sync_is_readonly_and_rejects_foreign_ownership(recovery,monkeypatch):
    p,_,_=recovery;data=vault.export_metadata();assert 'password_file' not in json.dumps(data)
    with db.connect() as c:c.execute("UPDATE settings SET value=? WHERE key='node_id'",('b'*32,))
    vault.receive_metadata(data)
    assert vault.overview()['profiles'][0]['local'] is False
    with pytest.raises(ValueError,match='来源节点'):vault.profile(p['id'])
    with db.connect() as c:c.execute('UPDATE vault_profiles SET node_id=? WHERE id=?',('b'*32,p['id']))
    with pytest.raises(ValueError,match='身份冲突'):vault.receive_metadata(data)

def test_reject_repository_under_source_and_loose_key(env,tmp_path):
    r=vault.configure('bad',str(env/'backup'),[str(env)],include_projects=False,include_catalog=False)
    with pytest.raises(ValueError,match='来源目录'):vault.plan(r['id'])
    key=tmp_path/'key';key.write_text('unsafe');key.chmod(0o644)
    with pytest.raises(ValueError,match='0600'):vault.configure('bad',str(tmp_path/'repo'),password_file=key)

def test_interrupted_receipt_does_not_interrupt_live_cli_backup(recovery):
    p,_,_=recovery;profile=vault.profile(p['id'])
    vault.save_snapshot('interrupted',profile,'running',{},'2026-10-17')
    with vault.lock(p['id']):assert vault.overview()['snapshots'][0]['status']=='running'
    assert vault.overview()['snapshots'][0]['status']=='interrupted'

def test_restore_failure_is_preserved_as_drill_evidence(recovery,monkeypatch):
    p,_,_=recovery;profile=vault.profile(p['id'])
    vault.save_snapshot('failed-drill',profile,'complete',{'restic_snapshot':'a'*64,'restore_samples':[{'path':'/sample'}]},'2026-10-17')
    monkeypatch.setattr(vault,'restore_plan',lambda *a:{'id':'plan'})
    def fail(*a):raise ValueError('unavailable archive')
    monkeypatch.setattr(vault,'restore',fail)
    with pytest.raises(ValueError,match='unavailable'):vault.drill(lambda *a:None,'failed-drill')
    assert vault.snapshot('failed-drill')['body']['restore_drill']['status']=='failed'

def test_skill_alias_uses_canonical_backup_sample(recovery,env,tmp_path,monkeypatch):
    p,codex,_=recovery;package=env/'skill';package.mkdir();(package/'SKILL.md').write_text('linked skill')
    alias=codex/'skills/a-alias';alias.symlink_to(package,target_is_directory=True)
    report=vault.plan(p['id']);vault.stage(vault.profile(p['id']),report,tmp_path/'stage')
    assert str(package/'SKILL.md') in [x['path'] for x in report['restore_samples']]
    assert str(alias/'SKILL.md') not in [x['path'] for x in report['restore_samples']]
    monkeypatch.setattr(knowledge,'roots',lambda:(codex,[codex/'skills'],[{'path':str(env)}]))
    knowledge.scan();item=next(x for x in knowledge.listing()['items'] if x['path']==str(alias/'SKILL.md'))
    assert item['body']['resolved_path']==str(package/'SKILL.md')

def test_drill_rejects_reading_source_through_restored_link(recovery,env,tmp_path,monkeypatch):
    p,_,_=recovery;source=env/'original';source.write_text('source still exists')
    target=tmp_path/'restore';target.mkdir();(target/'alias').symlink_to(env,target_is_directory=True)
    sample={'path':'/alias/original','sha256':hashlib.sha256(source.read_bytes()).hexdigest()}
    vault.save_snapshot('outside',vault.profile(p['id']),'complete',{'restic_snapshot':'a'*64,'restore_samples':[sample]},'2026-10-17')
    monkeypatch.setattr(vault,'restore_plan',lambda *a:{'id':'plan'})
    monkeypatch.setattr(vault,'restore',lambda *a:{'id':'restored','target':str(target)})
    with pytest.raises(ValueError,match='隔离目录之外'):vault.drill(lambda *a:None,'outside')
    assert vault.snapshot('outside')['body']['restore_drill']['status']=='failed'
