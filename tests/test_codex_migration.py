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
from asset_manager import codex_attachments as attachments
from asset_manager import migration_tools


def test_ssh_arguments_reject_option_and_shell_injection():
    for host in ['-oProxyCommand=evil', 'user@host;id', 'user@$(id)', 'host name', 'user@host\nfoo']:
        with pytest.raises(ValueError):
            migration.ssh_arguments({'host': host})
    args = migration.ssh_arguments({'host': 'user@lab-mac.local', 'port': 2222})
    assert '-oStrictHostKeyChecking=yes' in args
    assert args[-1] == 'user@lab-mac.local'


def test_deployment_sends_verified_release_and_receiver_plan(env, monkeypatch):
    plan = {'id':'a'*32, 'status':'native_verified', 'atlas':{'catalog':'AssetVault/catalog'},
            'root':'/target/incoming', 'roots':[]}
    migration.save(plan)
    def package(args, **kw):
        Path(args[-1]).write_bytes(b'verified-release')
        return subprocess.CompletedProcess(args, 0, b'', b'')
    sent = []
    monkeypatch.setattr(migration.subprocess, 'run', package)
    monkeypatch.setattr(migration, 'rsync', lambda *args: sent.append(args))
    def remote(plan, action, timeout=90, **body):
        assert action == 'deploy_atlas' and body['source_plan'] == plan
        assert body['release_sha256'] == agent.digest(sent[0][1])
        assert body['port'] == 8765
        return {'status':'running'}
    monkeypatch.setattr(migration, 'remote', remote)
    assert migration.deploy_atlas(plan['id'])['status'] == 'running'
    assert migration.read(plan['id'])['atlas_deployment']['status'] == 'running'


def test_staging_cannot_overlap_any_activation_destination(tmp_path):
    roots = [{'relative': '.codex'}, {'relative': 'Documents/project'}]
    for relative in ['.codex/import', 'Documents/project/incoming', 'Documents']:
        with pytest.raises(ValueError, match='重叠'):
            agent.validate_staging_root(tmp_path / relative, tmp_path, roots)
    agent.validate_staging_root(tmp_path / 'MigrationInbox/job', tmp_path, roots)
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.mkdir()
    (tmp_path / 'linked').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='外部链接'):
        agent.validate_staging_root(tmp_path / 'MigrationInbox/job', tmp_path,
                                    [{'relative': 'linked/project'}], resolve_paths=True)


def test_runtime_discovery_follows_user_launcher_package_without_login_profiles(tmp_path):
    codex = tmp_path / '.codex'; codex.mkdir()
    launchers = tmp_path / '.local/bin'; launchers.mkdir(parents=True)
    package = tmp_path / '.local/share/example-cli'; package.mkdir(parents=True)
    (package / 'cli').write_text('runtime')
    (launchers / 'example').symlink_to(package / 'cli')
    login = tmp_path / '.example-account'; login.mkdir()
    node = tmp_path / '.nvm'; node.mkdir()
    roots = migration.source_roots(tmp_path, codex)
    assert launchers in roots and package in roots and node in roots
    assert login not in roots and tmp_path not in roots


def test_service_node_selection_uses_captured_nvm_default(tmp_path, monkeypatch):
    monkeypatch.setattr(migration.shutil, 'which', lambda _: '/outside/node')
    for version in ['v20.20.0', 'v26.8.2']:
        executable = tmp_path / '.nvm/versions/node' / version / 'bin/node'
        executable.parent.mkdir(parents=True); executable.write_text('fixture')
    alias = tmp_path / '.nvm/alias/default'; alias.parent.mkdir(); alias.write_text('20\n')
    assert migration.user_node_bin(tmp_path, [tmp_path / '.nvm']) == '.nvm/versions/node/v20.20.0/bin'
    assert migration.user_node_bin(tmp_path, []) is None


def test_expired_temporary_cwd_is_recorded_without_ignoring_project_gaps(tmp_path, monkeypatch):
    home = tmp_path / 'home'; (home / '.codex').mkdir(parents=True)
    scratch = tmp_path / 'scratch'; scratch.mkdir()
    monkeypatch.setattr(migration.tempfile, 'gettempdir', lambda: str(scratch))
    with sqlite3.connect(home / '.codex/state_5.sqlite') as c:
        c.execute('CREATE TABLE threads(id TEXT,cwd TEXT,project_id TEXT)')
        c.executemany('INSERT INTO threads VALUES(?,?,?)', [('temporary', str(scratch / 'expired'), None),
            ('registered', str(scratch / 'project'), 'project'), ('missing', str(home / 'missing'), None)])
    records = migration.missing_transient_workspaces(home)
    assert [r['id'] for r in records] == ['temporary']
    root = tmp_path / 'snapshot'; (root / 'evidence').mkdir(parents=True)
    (root / 'evidence/source-manifest.jsonl').write_text('')
    with sqlite3.connect(home / '.codex/state_5.sqlite') as c:
        c.execute('ALTER TABLE threads ADD COLUMN git_sha TEXT')
        c.execute('CREATE TABLE project_roots(path TEXT)')
    result = profile.workspace_acceptance(root, '/source', str(home), records)
    assert result['status'] == 'needs_attention'
    assert {r['id'] for r in result['missing_thread_workspaces']} == {'registered', 'missing'}
    assert result['source_missing_transient_workspaces'][0]['id'] == 'temporary'


def test_tool_supplement_preserves_configuration_and_refuses_repeat(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / 'MigrationInbox/tools'
    agent.prepare({'root': str(root), 'id': 'tools', 'source_home': '/old'})
    executable = root / 'incoming/tools/bin/node'
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nprintf "fixture-version\\n"\n'); executable.chmod(0o755)
    manifest = root / 'evidence/source-manifest.jsonl'; manifest.write_text(json.dumps({'path': 'tools/bin/node', 'type': 'file'}) + '\n')
    agent.atomic_json(root / 'evidence/verification.json', {'status': 'passed', 'manifest_sha256': agent.digest(manifest)})
    config = tmp_path / '.codex/config.toml'; config.parent.mkdir()
    original = '[shell_environment_policy.set]\nPATH="/custom/bin"\n[mcp_servers.keep]\ncommand="/keep"\n'
    config.write_text(original)
    body = {'root': str(root), 'id': 'tools', 'roots': ['tools'], 'node_bin': 'tools/bin',
            'probes': [{'name': 'node', 'relative': 'tools/bin/node', 'args': ['--version']}]}
    report = migration_tools.install(body, agent, profile)
    assert report['status'] == 'passed' and report['checks'][0]['output'] == 'fixture-version\n'
    assert (root / 'evidence/target-config-before-tools.toml').read_text() == original
    assert profile.load_toml(config)['mcp_servers']['keep']['command'] == '/keep'
    assert profile.load_toml(config)['shell_environment_policy']['set']['PATH'].startswith('/custom/bin:')
    with pytest.raises(ValueError, match='目标目录已存在'):
        migration_tools.install(body, agent, profile)
    assert executable.read_text() == (tmp_path / 'tools/bin/node').read_text()


def test_tool_supplement_rejects_unverified_content_before_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / 'MigrationInbox/tools'
    agent.prepare({'root': str(root), 'id': 'tools', 'source_home': '/old'})
    agent.atomic_json(root / 'evidence/verification.json', {'status': 'failed'})
    with pytest.raises(ValueError, match='未通过完整校验'):
        migration_tools.install({'root': str(root), 'id': 'tools'}, agent, profile)
    assert not (tmp_path / 'tools').exists()


def test_readonly_prepared_git_reference_is_repaired_with_mode_retained(tmp_path):
    import stat
    path = tmp_path / 'project/.git/objects/info/alternates'
    path.parent.mkdir(parents=True)
    path.write_text('/old/project/.git/objects\n'); path.chmod(0o444)
    report = profile.repair_references([tmp_path], '/old', '/new')
    assert path.read_text() == '/new/project/.git/objects\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert report['changed'][0]['kind'] == 'git_alternates'


@pytest.mark.parametrize('required', ['3.14.3', '3.14.4'])
def test_python_rebinding_requires_exact_version_and_keeps_retry_evidence(tmp_path, monkeypatch, required):
    import platform
    root = tmp_path / 'migration'; home = tmp_path / 'target'; home.mkdir()
    runtime = Path('project/.runtime/python/bin/python3.14')
    link = Path('project/.venv/bin/python3.14')
    staged_runtime = root / 'prepared/home' / runtime
    staged_runtime.parent.mkdir(parents=True)
    staged_runtime.write_bytes(b'\xcf\xfa\xed\xfe' + b'fixture')
    staged_link = root / 'prepared/home' / link; staged_link.parent.mkdir(parents=True)
    staged_link.symlink_to('/missing/source-python/bin/python3.14')
    config = staged_link.parent.parent / 'pyvenv.cfg'
    config.write_text('home = /missing/source-python/bin\nversion = ' + required + '\n'); config.chmod(0o444)
    manifest = root / 'evidence/source-manifest.jsonl'; manifest.parent.mkdir()
    manifest.write_text(json.dumps({'path': str(runtime), 'type': 'file'}) + '\n' + json.dumps({'path': str(link), 'type': 'symlink'}) + '\n')
    def identity(path):
        path = Path(path)
        return {'version': [3,14,3] if '.runtime' in path.parts else [3,9,6], 'executable': str(path),
                'base_prefix': str(path.parent.parent), 'prefix': str(path.parent.parent),
                'architecture': platform.machine(), 'soabi': 'cpython-314-darwin', 'free_threaded': False}
    monkeypatch.setattr(migration_tools, 'python_identity', identity)
    report = migration_tools.repair_python_links(root, home, agent, profile)
    if required == '3.14.4':
        assert report['status'] == 'needs_attention' and not report['changes']
        assert os.readlink(staged_link) == '/missing/source-python/bin/python3.14'
    else:
        assert report['status'] == 'prepared' and os.readlink(staged_link) == str(home / runtime)
        assert len(migration_tools.repair_python_links(root, home, agent, profile)['changes']) == 1
        assert str(home / runtime.parent) in config.read_text()
    assert config.stat().st_mode & 0o777 == 0o444


@pytest.mark.parametrize('empty', ['', 'turns', 'items', 'skills', 'resume', 'read', 'list'])
def test_native_acceptance_requires_actual_history_and_resume(tmp_path, monkeypatch, empty):
    codex = tmp_path / '.codex'; codex.mkdir()
    cwd = tmp_path / 'project'; cwd.mkdir()
    cli = tmp_path / 'codex-cli'; cli.write_text('fixture')
    monkeypatch.setattr(profile.shutil, 'which', lambda _: str(cli))
    with sqlite3.connect(codex / 'state_5.sqlite') as c:
        c.execute('CREATE TABLE threads(id TEXT,attached_thread_parent_id TEXT,source TEXT)')
        c.execute('INSERT INTO threads VALUES(?,NULL,?)', ('one', json.dumps({'subagent': {'thread_spawn': {'parent_thread_id': 'parent'}}})))
    with sqlite3.connect(codex / 'thread_history_1.sqlite') as c:
        c.execute('CREATE TABLE thread_turns(thread_id TEXT)')
        c.execute("INSERT INTO thread_turns VALUES('one')")
        c.execute("INSERT INTO thread_turns VALUES('deleted-source-thread')")
        c.execute('CREATE TABLE thread_items(thread_id TEXT)')
        c.execute("INSERT INTO thread_items VALUES('one')")
    class Client:
        def __init__(self, *args): pass
        def close(self): pass
        def request(self, method, params, **kw):
            if method == 'thread/list': return {'data': [{'id': 'one'}] if params.get('parentThreadId') == 'parent' and empty != 'list' else []}
            if method == 'thread/read': return {'thread': {'id': 'wrong' if empty == 'read' else 'one', 'cwd': str(cwd)}}
            assert params.get('threadId') != 'deleted-source-thread'
            if method == 'thread/turns/list': return {'data': [] if empty == 'turns' else [{'id': 'turn'}]}
            if method == 'thread/items/list': return {'data': [] if empty == 'items' else [{'type': 'userMessage'}]}
            if method == 'thread/resume': return {'thread': {'id': 'wrong' if empty == 'resume' else 'one'}, 'cwd': str(cwd)}
            if method == 'skills/list': return {'data': [] if empty == 'skills' else [{'cwd': str(cwd), 'skills': []}]}
            pytest.fail('Unexpected native request: ' + method)
    monkeypatch.setattr(profile, 'NativeCodex', Client)
    result = profile.native_acceptance(codex, tmp_path, ['one'], [])
    assert result['status'] == ('failed' if empty else 'passed')
    assert result['model_turns_started'] is False
    assert result['unregistered_history_ids'] == ['deleted-source-thread']
    assert result['parent_groups_checked'] == 1


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


def test_rollout_rebinding_survives_index_rebuild_without_rewriting_messages(tmp_path):
    codex = tmp_path / '.codex'; logs = codex / 'sessions'; logs.mkdir(parents=True)
    path = logs / 'rollout.jsonl'
    original = b'{"type":"response_item","payload":{"text":"/old/user/project","cwd":"/old/user/project"}}\n'
    huge = b'{"type":"response_item","payload":"' + b'x' * 1024 + b'"}\n'
    metadata = {'type': 'session_meta', 'payload': {'cwd': '/old/user/project', 'instructions': '/old/user/keep-prose'}}
    context = {'type': 'turn_context', 'payload': {'cwd': '/old/user/project'}}
    path.write_bytes(json.dumps(metadata).encode()+b'\n'+original+huge+json.dumps(context).encode()+b'\n')
    path.chmod(0o444)
    with sqlite3.connect(codex / 'state_5.sqlite') as c:
        c.execute('CREATE TABLE threads(id TEXT,cwd TEXT,rollout_path TEXT)')
        c.execute('INSERT INTO threads VALUES(?,?,?)', ('one','/old/user/project','/old/user/.codex/sessions/rollout.jsonl'))
    result = profile.repair_rollout_metadata(codex, '/old/user', '/new/user', max_record_bytes=256)
    content = path.read_bytes(); lines = content.splitlines()
    assert json.loads(lines[0])['payload'] == {'cwd':'/new/user/project','instructions':'/old/user/keep-prose'}
    assert json.loads(lines[-1])['payload']['cwd'] == '/new/user/project'
    assert original in content and huge in content
    assert result['state_rows'] == 1 and result['files'][0]['records'] == 2
    assert result['warnings'][0]['reason'] == 'record_size_limit'
    assert path.stat().st_mode & 0o777 == 0o444
    with sqlite3.connect(codex / 'state_5.sqlite') as c:
        assert c.execute('SELECT cwd FROM threads').fetchone()[0] == '/new/user/project'


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
        c.execute("UPDATE projects SET git_json=? WHERE id='migrated-project'", (json.dumps({'root':'/source/user/project','common_dir':'/source/user/project/.git','note':'Historical /source/user/project'}),))
        c.execute("INSERT INTO project_locations VALUES('migrated-project','source-node','workspace',NULL,'/source/user/project','before')")
        c.execute("INSERT INTO audits(action,detail) VALUES('historical','/source/user/project')")
    extra=target/'AI/additional';extra.mkdir(parents=True);(extra/'README.md').write_text('Previously unregistered project')
    empty=target/'Documents/Codex/empty';empty.mkdir(parents=True)
    report=profile.rebase_atlas(db.DATA,'/source/user',str(target),[{'relative':'AI/additional'},{'relative':'Documents/Codex/empty'}])
    with db.connect() as c:
        assert c.execute("SELECT path FROM projects WHERE id='migrated-project'").fetchone()[0]==str(target/'project')
        metadata = json.loads(c.execute("SELECT git_json FROM projects WHERE id='migrated-project'").fetchone()[0])
        assert metadata['root'] == str(target/'project') and metadata['common_dir'] == str(target/'project/.git')
        assert metadata['note'] == 'Historical /source/user/project'
        assert not c.execute("SELECT * FROM settings WHERE key='session:old-cookie'").fetchone()
        assert c.execute("SELECT value FROM settings WHERE key='node_id'").fetchone()[0]!='source-node'
        assert c.execute("SELECT source_node FROM project_locations WHERE project_id='migrated-project'").fetchone()[0]=='source-node'
        assert c.execute("SELECT detail FROM audits WHERE action='historical'").fetchone()[0]=='/source/user/project'
        assert c.execute('SELECT path FROM projects WHERE path=?',(str(extra),)).fetchone()
        assert not c.execute('SELECT path FROM projects WHERE path=?',(str(empty),)).fetchone()
    assert report['updated']['projects.path']>=1
    assert len(report['imported_workspaces'])==1


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


def test_discovery_includes_historical_working_projects_and_model_weights(tmp_path):
    home=tmp_path/'home';codex=home/'.codex';codex.mkdir(parents=True)
    project=home/'AI/robot/release';project.mkdir(parents=True)
    cache=home/'.cache/huggingface/hub';cache.mkdir(parents=True)
    browser=home/'Library/Application Support/Codex';browser.mkdir(parents=True)
    with sqlite3.connect(codex/'thread_history_1.sqlite') as c:
        c.execute('CREATE TABLE thread_items(item_type TEXT,item_json TEXT)')
        for path in [home,home/'Documents',project,home/'AI/deleted',browser]:
            c.execute('INSERT INTO thread_items VALUES(?,?)',('commandExecution',json.dumps({'cwd':str(path)})))
    discovery={};roots=migration.source_roots(home,codex,discovery)
    assert project in roots and cache in roots
    assert home not in roots and home/'Documents' not in roots and browser not in roots
    assert str(home/'AI/deleted') in discovery['missing']


def test_live_writer_capture_preserves_source_and_distinguishes_runtime_locks(tmp_path,monkeypatch):
    from types import SimpleNamespace
    project=tmp_path/'project';project.mkdir()
    log=project/'runtime.log';log.write_text('before\n');log.chmod(0o640)
    lock=project/'agent.lock';lock.write_text('123')
    read_only=project/'model.json';read_only.write_text('{}')
    output='p123\nf1\nau\ntREG\nn'+str(log)+'\nf2\naw\ntREG\nn'+str(lock)+'\nf3\nar\ntREG\nn'+str(read_only)+'\nf4\nau\ntREG\nn/private/protected/system.db\n'
    with monkeypatch.context() as patch:
        patch.setattr(migration.shutil,'which',lambda _: '/usr/sbin/lsof')
        patch.setattr(migration.subprocess,'run',lambda *a,**kw:SimpleNamespace(stdout=output))
        patch.setattr(Path,'is_file',lambda p: True if p.is_relative_to(project) else pytest.fail('Must not inspect files outside selected roots'))
        plan={'roots':[{'source':str(project)}],'excluded':[]}
        migration.include_active_files(plan)
    assert plan['frozen_files']==[str(log)]
    assert migration.filtered(plan,lock) and not migration.filtered(plan,read_only)
    snapshot=tmp_path/'snapshot';info=migration.snapshot_file(log,snapshot,preserve_mode=True)
    with log.open('a') as f:f.write('after\n')
    assert snapshot.read_text()=='before\n' and log.read_text()=='before\nafter\n'
    assert info['mode']==0o640 and info['sha256']==agent.digest(snapshot)


def test_python_environment_references_rebase_without_rewriting_code_or_readme(tmp_path):
    root=tmp_path/'project';env=root/'.venv';(env/'bin').mkdir(parents=True)
    (env/'pyvenv.cfg').write_text('home = /old/user/python/bin\nversion_info = 3.12\n')
    entry=env/'bin/tool';entry.write_text('#!/old/user/project/.venv/bin/python\nprint("Historical /old/user")\n');entry.chmod(0o755)
    packages=env/'lib/python3.12/site-packages';packages.mkdir(parents=True)
    (packages/'editable.pth').write_text('/old/user/project/src\nimport unrelated_loader\n')
    profile.repair_references([root],'/old/user','/new/user')
    assert (env/'pyvenv.cfg').read_text().startswith('home = /new/user/python/bin\n')
    assert entry.read_text()=='#!/new/user/project/.venv/bin/python\nprint("Historical /old/user")\n'
    assert entry.stat().st_mode & 0o777==0o755
    assert (packages/'editable.pth').read_text()=='/new/user/project/src\nimport unrelated_loader\n'


def attachment_fixture(tmp_path):
    root=tmp_path/'job';codex=root/'incoming/.codex';(codex/'sessions').mkdir(parents=True);(root/'evidence').mkdir()
    with sqlite3.connect(codex/'thread_history_1.sqlite') as c:
        c.execute('CREATE TABLE thread_items(item_type TEXT,item_json TEXT)')
        c.execute('INSERT INTO thread_items VALUES(?,?)',('userMessage',json.dumps({'type':'userMessage','content':[{'type':'localImage','path':'/tmp/missing.png'}]})))
    return root,codex/'sessions/rollout.jsonl'


def image_record(data,path='/tmp/missing.png'):
    import base64
    return {'type':'compacted','payload':{'replacement_history':[{'type':'message','role':'user','content':[
        {'type':'input_text','text':'<image name=[Image #1] path="'+path+'">'},
        {'type':'input_image','image_url':'data:image/png;base64,'+base64.b64encode(data).decode()},
        {'type':'input_text','text':'</image>'}]}]}}


def test_embedded_attachment_recovery_is_evidenced_bounded_and_keeps_originals(tmp_path):
    root,rollout=attachment_fixture(tmp_path);data=b'\x89PNG\r\n\x1a\nfixture'
    rollout.write_bytes(b'x'*2048+b'\n'+json.dumps(image_record(data)).encode()+b'\n')
    before=agent.digest(rollout)
    result=attachments.recover(root,'/old/user','/new/user',max_record_bytes=1024)
    assert result['recovered_paths']==1 and result['unresolved_user_images']==0
    assert result['records_skipped'][0]['reason']=='record_size_limit'
    assert agent.digest(rollout)==before
    item=next(iter(result['files'].values()))
    assert Path(item['source']).read_bytes()==data and item['line']==2
    original={'type':'userMessage','content':[{'type':'text','text':'Original /old/user/path'}, {'type':'localImage','path':'/tmp/missing.png'}]}
    mapped=attachments.relocate_item(original,'/old/user','/new/user',result['aliases'])
    assert mapped['content'][0]==original['content'][0]
    assert mapped['content'][1]['path'].startswith('/new/user/.codex/attachments/atlas-recovered/')
    assert original['content'][1]['path']=='/tmp/missing.png'
    tool={'type':'mcpToolCall','arguments':{'path':'/old/user/file'}}
    assert attachments.relocate_item(tool,'/old/user','/new/user')==tool


def test_recovery_does_not_guess_between_conflicting_images_or_text_mentions(tmp_path):
    root,rollout=attachment_fixture(tmp_path)
    records=[image_record(b'first'),image_record(b'second')]
    rollout.write_text(''.join(json.dumps(r)+'\n' for r in records))
    result=attachments.recover(root,'/old/user','/new/user')
    assert result['recovered_paths']==0 and result['unresolved_user_images']==1
    assert result['ambiguous_paths']==['/tmp/missing.png'] and len(result['files'])==2
    wrong=image_record(b'data');wrong['payload']['replacement_history'][0]['content'][0]['text']='Example /tmp/missing.png'
    assert list(attachments.embedded_images(wrong))==[]


def test_exclusion_boundaries_and_resume_reuse_complete_snapshots(env,monkeypatch):
    from types import SimpleNamespace
    plan={'excluded':[{'path':'/source/cache'}]}
    assert migration.filtered(plan,Path('/source/cache/a'))
    assert not migration.filtered(plan,Path('/source/cache-other/a'))
    plan['excluded'].append({'path':'/source/new'})
    assert migration.filtered(plan,Path('/source/new'))
    ident='f'*32
    plan.update(id=ident,status='paused',phase='manifest',captured_at=1,source_home=str(Path.home()),
                database_snapshots=[{'path':'.codex/state_5.sqlite','sha256':'saved'}])
    migration.save(plan)
    monkeypatch.setattr(migration.subprocess,'Popen',lambda *a,**kw:SimpleNamespace(pid=99999))
    migration.start(ident)
    saved=migration.read(ident)
    assert saved['reuse_snapshots'] and saved['skip_initial_copy']
    assert saved['database_snapshots']==plan['database_snapshots']


def test_receiver_refreshes_local_receipts_without_ssh(env,monkeypatch):
    ident='c'*32;root=env/'receiver';(root/'evidence').mkdir(parents=True)
    (root/'ATLAS-MIGRATION.json').write_text(json.dumps({'id':ident}))
    monkeypatch.setattr(agent,'checked_root',lambda value:Path(value))
    migration.save({'id':ident,'role':'receiver','status':'received','root':str(root),
        'native_acceptance':{'status':'old_sampled'}})
    (root/'evidence/native-acceptance.json').write_text(json.dumps({'status':'passed','full_history':{'items_checked':267193}}))
    (root/'evidence/completion.json').write_text(json.dumps({'status':'complete'}))
    current=migration.read(ident)
    assert current['native_acceptance']['full_history']['items_checked']==267193
    assert current['status']=='received' and '最新回执' in current['detail']
    assert migration.listing()[0]['detail']==current['detail']
    (root/'ATLAS-MIGRATION.json').write_text(json.dumps({'id':'wrong'}))
    with pytest.raises(ValueError,match='不匹配'):migration.read(ident)
