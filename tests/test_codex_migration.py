import io
import json
import os
import sqlite3
import sys
import subprocess
from pathlib import Path
import pytest
from test_assets import env, client, HEADERS
from asset_manager import codex_migration as migration, migration_agent as agent
from asset_manager import codex_profile as profile


def test_ssh_arguments_reject_option_and_shell_injection():
    for host in ['-oProxyCommand=evil', 'user@host;id', 'user@$(id)', 'host name', 'user@host\nfoo']:
        with pytest.raises(ValueError):
            migration.ssh_arguments({'host': host})
    args = migration.ssh_arguments({'host': 'user@lab-mac.local', 'port': 2222})
    assert '-oStrictHostKeyChecking=yes' in args
    assert args[-1] == 'user@lab-mac.local'


def test_sqlite_snapshot_includes_wal_and_preserves_source(tmp_path):
    path = tmp_path / 'live.sqlite'
    c = sqlite3.connect(path)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE messages(value TEXT)')
    c.execute("INSERT INTO messages VALUES('saved')")
    c.commit()
    target = tmp_path / 'snapshot.sqlite'
    info = migration.snapshot_database(path, target)
    c.execute("INSERT INTO messages VALUES('later')")
    c.commit()
    with sqlite3.connect(target) as snapshot:
        assert snapshot.execute('SELECT value FROM messages').fetchall() == [('saved',)]
    assert info['sha256'] == agent.digest(target)
    assert c.execute('SELECT count(*) FROM messages').fetchone()[0] == 2
    c.close()


def test_agent_refuses_existing_nonempty_and_wrong_job(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / 'MigrationInbox/job'
    root.mkdir(parents=True)
    (root / 'original.txt').write_text('user content')
    with pytest.raises(ValueError):
        agent.prepare({'root': str(root), 'id': 'a', 'source_home': '/old'})
    assert (root / 'original.txt').read_text() == 'user content'
    empty = tmp_path / 'MigrationInbox/empty'
    agent.prepare({'root': str(empty), 'id': 'a', 'source_home': '/old'})
    with pytest.raises(ValueError):
        agent.prepare({'root': str(empty), 'id': 'b', 'source_home': '/old'})


def test_full_verification_detects_content_corruption_and_symlink_change(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / 'MigrationInbox/job'
    agent.prepare({'root': str(root), 'id': 'a', 'source_home': '/old'})
    data = root / 'incoming/one.txt'; data.write_text('hello'); data.chmod(0o600)
    link = root / 'incoming/link'; link.symlink_to('one.txt')
    rows = [{'path': 'one.txt', 'type': 'file', 'size': 5, 'sha256': agent.digest(data), 'mode': 0o600},
            {'path': 'link', 'type': 'symlink', 'target': 'one.txt', 'mode': 0o777}]
    (root / 'evidence/source-manifest.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    assert agent.verify({'root': str(root)})['status'] == 'passed'
    data.write_text('HELLO')
    result = agent.verify({'root': str(root)})
    assert result['status'] == 'failed' and result['errors'][0]['path'] == 'one.txt'
    link.unlink(); link.symlink_to('/missing')
    assert len(agent.verify({'root': str(root)})['errors']) == 2


def test_manifest_parent_symlink_escape_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / 'MigrationInbox/job'
    agent.prepare({'root': str(root), 'id': 'a', 'source_home': '/old'})
    (root / 'incoming/escape').symlink_to(tmp_path)
    (root / 'evidence/source-manifest.jsonl').write_text(json.dumps({'path': 'escape/private', 'type': 'file', 'size': 0, 'mode': 0o600, 'sha256': ''})+'\n')
    with pytest.raises(ValueError, match='越界'):
        agent.verify({'root': str(root)})


def test_manifest_preserves_git_untracked_and_link_semantics(env, tmp_path):
    home = tmp_path / 'source'; root = home / 'project'; root.mkdir(parents=True)
    (root / '.git').mkdir(); (root / '.git/HEAD').write_text('ref: refs/heads/main\n')
    (root / 'untracked.txt').write_text('unsaved work')
    (root / 'link').symlink_to('untracked.txt')
    (root / 'runtime.lock').write_text('transient')
    ident = 'a' * 32
    migration.folder(ident).mkdir(parents=True)
    plan = {'id': ident, 'source_home': str(home), 'roots': [{'source': str(root)}],
            'excluded': [{'path': str(root / 'runtime.lock')}], 'database_snapshots': []}
    output = migration.make_manifest(plan)
    rows = [json.loads(x) for x in output.read_text().splitlines()]
    assert {'project/.git/HEAD', 'project/untracked.txt', 'project/link'} <= {r['path'] for r in rows}
    assert 'project/runtime.lock' not in {r['path'] for r in rows}
    assert (root / 'untracked.txt').read_text() == 'unsaved work'


def test_plan_start_requires_review_and_auth(client, monkeypatch):
    called = []
    monkeypatch.setattr(migration, 'start', lambda ident: called.append(ident) or {'started': True})
    ident = 'a' * 32
    assert client.post('/api/codex-migrations/'+ident+'/start', json={'reviewed_plan_id':ident}).status_code == 403
    assert client.post('/api/codex-migrations/'+ident+'/start', headers=HEADERS,json={}).status_code == 400
    assert not called
    assert client.post('/api/codex-migrations/'+ident+'/start', headers=HEADERS,json={'reviewed_plan_id':ident}).status_code == 200
    assert called == [ident]


def profile_db(path, ident, cwd, text='original words /old/user/project'):
    with sqlite3.connect(path) as c:
        c.executescript('CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,rollout_path TEXT,title TEXT,sandbox_policy TEXT);'
                        'CREATE TABLE remote_control_enrollments(id TEXT PRIMARY KEY);')
        c.execute('INSERT INTO threads VALUES(?,?,?,?,?)', (ident,cwd,cwd+'/rollout.jsonl',text,json.dumps({'writable_roots':[cwd]})))
        c.execute("INSERT INTO remote_control_enrollments VALUES('original-device')")


def test_profile_union_keeps_target_and_remaps_only_structured_fields(tmp_path):
    src,dest,out=[tmp_path/n for n in ['source.db','target.db','merged.db']]
    profile_db(src,'source','/old/user/project');profile_db(dest,'target','/new/user/existing')
    before=agent.digest(dest)
    report=profile.merge_database(src,dest,out,['threads'],'/old/user','/new/user')
    assert report['threads']['total']==2
    assert agent.digest(dest)==before
    with sqlite3.connect(out) as c:
        row=c.execute("SELECT * FROM threads WHERE id='source'").fetchone()
        assert row[1:3]==('/new/user/project','/new/user/project/rollout.jsonl')
        assert row[3]=='original words /old/user/project'
        assert json.loads(row[4])['writable_roots']==['/new/user/project']
        assert c.execute('SELECT count(*) FROM remote_control_enrollments').fetchone()[0]==1


def test_divergent_thread_identity_fails_without_modifying_destination(tmp_path):
    src,dest,out=[tmp_path/n for n in ['source.db','target.db','merged.db']]
    profile_db(src,'same','/old/user/project','source text')
    profile_db(dest,'same','/new/user/project','target text')
    before=agent.digest(dest)
    with pytest.raises(ValueError,match='同标识'):
        profile.merge_database(src,dest,out,['threads'],'/old/user','/new/user')
    assert not out.exists()
    assert agent.digest(dest)==before


def test_new_target_does_not_activate_source_remote_enrollment(tmp_path):
    src=tmp_path/'source.db';out=tmp_path/'out.db'
    profile_db(src,'source','/old/user/project')
    profile.merge_database(src,tmp_path/'absent.db',out,['threads'],'/old/user','/new/user')
    with sqlite3.connect(out) as c:
        assert c.execute('SELECT count(*) FROM remote_control_enrollments').fetchone()[0]==0


def test_schema_lineage_mismatch_refuses_import(tmp_path):
    src,dest,out=[tmp_path/n for n in ['source.db','target.db','merged.db']]
    for p,checksum in [(src,b'abc'),(dest,b'def')]:
        profile_db(p,p.stem,'/old/user/project')
        with sqlite3.connect(p) as c:
            c.execute('CREATE TABLE _sqlx_migrations(version INTEGER PRIMARY KEY,checksum BLOB)')
            c.execute('INSERT INTO _sqlx_migrations VALUES(1,?)',(checksum,))
    with pytest.raises(ValueError,match='谱系'):
        profile.merge_database(src,dest,out,['threads'],'/old/user','/new/user')
    assert not out.exists()


def test_paths_and_git_worktree_repair_leave_historical_prose_unchanged(tmp_path):
    root=tmp_path/'project';root.mkdir()
    (root/'.git').write_text('gitdir: /old/user/repo/.git/worktrees/tree\n')
    (root/'link').symlink_to('/old/user/data/model.step')
    (root/'README.md').write_text('Historical path /old/user/repo')
    result=profile.repair_references([root],'/old/user','/new/user')
    assert len(result['changed'])==2
    assert (root/'.git').read_text()=='gitdir: /new/user/repo/.git/worktrees/tree\n'
    assert os.readlink(root/'link')=='/new/user/data/model.step'
    assert (root/'README.md').read_text()=='Historical path /old/user/repo'


def test_global_state_retains_device_and_queued_actions(tmp_path):
    source={'saved-workspace-roots':['/old/user/project'],'queued-follow-ups':['source action'],
            'electron-local-remote-control-installation-id':'source'}
    target={'saved-workspace-roots':['/new/user/own'],'queued-follow-ups':['target action'],
            'electron-local-remote-control-installation-id':'target'}
    result=profile.merge_global_state(source,target,'/old/user','/new/user')
    assert result['saved-workspace-roots']==['/new/user/own','/new/user/project']
    assert result['queued-follow-ups']==['target action']
    assert result['electron-local-remote-control-installation-id']=='target'


@pytest.mark.skipif(sys.platform!='darwin' and not hasattr(os,'setxattr'),reason='filesystem xattrs unavailable')
def test_readonly_git_object_extended_attributes_restore(tmp_path,monkeypatch):
    import base64
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    root=tmp_path/'MigrationInbox/job'
    agent.prepare({'root':str(root),'id':'a','source_home':'/old'})
    data=root/'incoming/object';data.write_bytes(b'git content');data.chmod(0o444)
    name='user.atlas_test'
    row={'path':'object','type':'file','size':data.stat().st_size,'sha256':agent.digest(data),
         'mode':0o444,'xattrs':{name:base64.b64encode(b'metadata').decode()}}
    manifest=root/'evidence/source-manifest.jsonl';manifest.write_text(json.dumps(row)+'\n')
    result=agent.verify({'root':str(root),'restore_xattrs':True,'manifest_sha256':agent.digest(manifest)})
    assert result['status']=='passed'
    assert data.stat().st_mode & 0o777==0o444
    assert agent.get_attribute(data,name)==b'metadata'


def test_config_roundtrip_retains_target_choices_and_maps_source_tools():
    import tomllib
    source={'model':'source-model','projects':{'/old/user/project':{'trust_level':'trusted'}},
            'mcp_servers':{'local':{'command':'/old/user/tools/server','args':['--data','/old/user/data']}}}
    target={'model':'target-model','mcp_servers':{'other':{'command':'/target/tool'}}}
    merged,conflicts=profile.merge_config(source,target,'/old/user','/new/user')
    assert merged['model']=='target-model'
    assert merged['mcp_servers']['local']['args'][1]=='/new/user/data'
    assert '/new/user/project' in merged['projects']
    assert conflicts==['model']
    assert tomllib.loads(profile.toml_text(merged))==merged


def test_extra_receiver_files_are_preserved_outside_active_snapshot(tmp_path,monkeypatch):
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    root=tmp_path/'MigrationInbox/job'
    agent.prepare({'root':str(root),'id':'a','source_home':'/old'})
    data=root/'incoming/expected';data.write_text('yes');data.chmod(0o600)
    extra=root/'incoming/old-cache';extra.mkdir();(extra/'untouched').write_text('prior copy')
    manifest=root/'evidence/source-manifest.jsonl'
    manifest.write_text(json.dumps({'path':'expected','type':'file','size':3,'mode':0o600,'sha256':agent.digest(data)})+'\n')
    result=agent.verify({'root':str(root),'quarantine_extras':True})
    assert result['status']=='passed' and not extra.exists()
    location=Path(result['incoming_extras_preserved']['location'])
    assert (location/'old-cache/untouched').read_text()=='prior copy'


@pytest.mark.parametrize('fail_switch',[False,True])
def test_activation_switch_preserves_original_and_rolls_back_partial_moves(tmp_path,monkeypatch,fail_switch):
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    monkeypatch.setitem(sys.modules,'atlas_codex_profile',profile)
    monkeypatch.setattr(agent.sys,'platform','darwin')
    monkeypatch.setattr(agent,'run',lambda *args,**kwargs:'')
    current=tmp_path/'.codex';current.mkdir();(current/'original').write_text('target')
    root=tmp_path/'MigrationInbox/job'
    agent.prepare({'root':str(root),'id':'a','source_home':'/old'})
    prepared=root/'prepared/.codex';prepared.mkdir(parents=True);(prepared/'imported').write_text('source')
    project=root/'prepared/home/project';project.mkdir(parents=True);(project/'code').write_text('source project')
    manifest=root/'evidence/source-manifest.jsonl';manifest.write_text('')
    agent.atomic_json(root/'evidence/activation.json',{'status':'prepared','manifest_sha256':agent.digest(manifest)})
    if fail_switch:
        rename=os.rename
        def failing_rename(source,dest):
            if Path(source)==prepared and Path(dest)==current:raise OSError('simulated switch failure')
            return rename(source,dest)
        monkeypatch.setattr(agent.os,'rename',failing_rename)
        with pytest.raises(OSError,match='simulated'):
            agent.activate({'root':str(root),'allow_app_restart':True,'roots':[{'relative':'project'}]})
        assert (current/'original').read_text()=='target'
        assert (project/'code').read_text()=='source project'
        assert not (tmp_path/'project').exists()
    else:
        result=agent.activate({'root':str(root),'allow_app_restart':True,'roots':[{'relative':'project'}]})
        assert result['status']=='activated_pending_acceptance'
        assert (current/'imported').read_text()=='source'
        assert (root/'destination-original-codex/original').read_text()=='target'
        assert (tmp_path/'project/code').read_text()=='source project'


def test_atlas_rebinding_changes_current_paths_identity_and_invalidates_sessions(env,tmp_path):
    from asset_manager import db
    target=tmp_path/'destination';(target/'project').mkdir(parents=True)
    with db.connect() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES('node_id','source-node')")
        c.execute("INSERT OR REPLACE INTO settings VALUES('session:old-cookie','9999999999')")
        c.execute("INSERT INTO projects(id,name,path) VALUES('migrated-project','Project','/source/user/project')")
        c.execute("INSERT INTO project_locations VALUES('migrated-project','source-node','workspace',NULL,'/source/user/project','before')")
        c.execute("INSERT INTO audits(action,detail) VALUES('historical','/source/user/project')")
    report=profile.rebase_atlas(db.DATA,'/source/user',str(target))
    with db.connect() as c:
        assert c.execute("SELECT path FROM projects WHERE id='migrated-project'").fetchone()[0]==str(target/'project')
        assert not c.execute("SELECT * FROM settings WHERE key='session:old-cookie'").fetchone()
        assert c.execute("SELECT value FROM settings WHERE key='node_id'").fetchone()[0]!='source-node'
        assert c.execute("SELECT source_node FROM project_locations WHERE project_id='migrated-project'").fetchone()[0]=='source-node'
        assert c.execute("SELECT detail FROM audits WHERE action='historical'").fetchone()[0]=='/source/user/project'
    assert report['updated']['projects.path']>=1


def test_historical_path_alias_keeps_artifact_links_without_rewriting_messages():
    from asset_manager import path_aliases, provenance, project_index
    projects=path_aliases.project_roots([{'id':'p','path':'/new/user/project','name':'Project'}],
        [{'source':'/old/user','target':'/new/user'}])
    original='See [design](/old/user/project/design.md)'
    items=[{'item_id':'i','item_json':json.dumps({'type':'agentMessage','phase':'final','text':original})}]
    turn={'turn_id':'t','status':'completed','started_at':1,'completed_at':2,'duration_ms':1}
    body,artifacts=provenance.parse_turn(turn,items,{'cwd':'/new/user/project'},projects,{('p','design.md')})
    assert artifacts[0]['path']=='design.md' and artifacts[0]['project_id']=='p'
    assert original in json.dumps(body,ensure_ascii=False)
    assert project_index.resolve('p','README.md','/old/user/project/design.md',{('p','design.md'):1},projects)==('p','design.md','resolved')


def test_git_acceptance_detects_ref_change_and_recovers_recorded_missing_worktree(tmp_path):
    home=tmp_path/'target';root=home/'MigrationInbox/job';repo=home/'project'
    repo.mkdir(parents=True);(home/'.codex').mkdir()
    def git(*args):
        return subprocess.check_output(['git','-c','user.name=Atlas Test','-c','user.email=test@example.invalid',
            '-c','core.hooksPath=/dev/null','-C',str(repo),*args],stderr=subprocess.DEVNULL).decode().strip()
    git('init','-b','main');(repo/'code.txt').write_text('original');git('add','.');git('commit','-m','fixture')
    sha=git('rev-parse','HEAD')
    snapshot=root/'incoming/project';profile.clone(repo,snapshot)
    (root/'evidence').mkdir();(root/'evidence/source-manifest.jsonl').write_text(json.dumps({'path':'project/.git','type':'directory'})+'\n')
    missing=home/'.codex/worktrees/old/project'
    with sqlite3.connect(home/'.codex/state_5.sqlite') as c:
        c.executescript('CREATE TABLE project_roots(path TEXT);CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,git_sha TEXT);')
        c.execute('INSERT INTO project_roots VALUES(?)',(str(repo),))
        c.execute('INSERT INTO threads VALUES(?,?,?)',('historical',str(missing),sha))
    before=agent.digest(snapshot/'.git/refs/heads/main')
    result=profile.workspace_acceptance(root,'/old/user',str(home))
    assert result['status']=='needs_attention' and len(result['missing_thread_workspaces'])==1
    recovered=profile.recover_historical_worktree(home,'historical',repo)
    assert recovered['commit']==sha and not recovered['uncommitted_source_state_recovered']
    assert (missing/'code.txt').read_text()=='original'
    assert profile.workspace_acceptance(root,'/old/user',str(home))['status']=='passed'
    assert agent.digest(snapshot/'.git/refs/heads/main')==before
    with pytest.raises(ValueError,match='已存在'):
        profile.recover_historical_worktree(home,'historical',repo)
    git('branch','unrelated-new-ref')
    result=profile.workspace_acceptance(root,'/old/user',str(home))
    assert result['status']=='needs_attention' and result['git_errors'][0]['refs_equal'] is False


def test_receiver_reads_receipts_locally_and_completion_requires_all_layers(env,tmp_path,monkeypatch):
    ident='e'*32;root=tmp_path/'MigrationInbox/job'
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    agent.prepare({'root':str(root),'id':ident,'source_home':'/old'})
    plan={'id':ident,'role':'receiver','root':str(root),'status':'received'}
    migration.save(plan)
    monkeypatch.setattr(migration,'remote',lambda *a,**kw:pytest.fail('Receiver must read local evidence'))
    agent.atomic_json(root/'evidence/verification.json',{'status':'passed'})
    assert migration.reports(ident)['verification.json']['status']=='passed'
    with pytest.raises(ValueError,match='native-acceptance'):
        migration.complete(ident)
