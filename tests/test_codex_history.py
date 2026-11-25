import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from asset_manager import codex_history as history, codex_profile as profile, codex_migration as migration

LOGICAL = '11111111-1111-1111-1111-111111111111'
BRANCH = '22222222-2222-2222-2222-222222222222'


def fixture(tmp_path):
    home=tmp_path/'profile'; logs=home/'sessions'; logs.mkdir(parents=True)
    with sqlite3.connect(home/'state_5.sqlite') as c:
        c.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,rollout_path TEXT)')
        c.execute('INSERT INTO threads VALUES(?,?,?)',(LOGICAL,'/old/long-user/项目',str(logs/('rollout-'+LOGICAL+'_'+BRANCH+'.jsonl'))))
    with sqlite3.connect(home/'thread_history_1.sqlite') as c:
        c.executescript('''CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,rollout_ordinal INTEGER,status TEXT,completed_at INTEGER,rollout_byte_offset INTEGER,rollout_end_ordinal INTEGER,rollout_end_byte_offset INTEGER,PRIMARY KEY(thread_id,turn_id));
            CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,next_rollout_byte_offset INTEGER,next_rollout_ordinal INTEGER);
            CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,item_json TEXT,rollout_ordinal INTEGER,PRIMARY KEY(thread_id,turn_id,item_id));''')
        for bid,turn in [(LOGICAL,'base'),(BRANCH,'new')]:
            name='rollout-'+LOGICAL+('_'+BRANCH if bid==BRANCH else '')+'.jsonl'
            records=[{'ordinal':0,'type':'session_meta','payload':{'id':LOGICAL,'cwd':'/old/long-user/项目'}},
                {'ordinal':1,'type':'event_msg','payload':{'type':'task_started'}},
                {'ordinal':2,'type':'response_item','payload':{'text':'Keep /old/long-user/项目 exactly'}},
                {'ordinal':3,'type':'event_msg','payload':{'type':'task_complete'}}]
            if bid==BRANCH:
                records[0]['payload']['history_base']={'thread_id':LOGICAL,'end_ordinal_exclusive':4,
                    'end_byte_offset':(logs/('rollout-'+LOGICAL+'.jsonl')).stat().st_size}
            lines=[(json.dumps(x,ensure_ascii=False,separators=(',',':'))+'\n').encode() for x in records]
            (logs/name).write_bytes(b''.join(lines))
            c.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?,?,?,?)',(bid,turn,1,'completed',100,len(lines[0]),3,sum(map(len,lines))))
            c.execute('INSERT INTO thread_history_projection_state VALUES(?,?,?)',(bid,sum(map(len,lines)),4))
            item={'id':turn+'-item','type':'agentMessage','text':'reply '+turn}
            c.execute('INSERT INTO thread_items VALUES(?,?,?,?,?)',(bid,turn,item['id'],json.dumps(item),2))
    return home


def test_rewrite_rebases_every_offset_and_preserves_prose_and_branch_identity(tmp_path):
    home=fixture(tmp_path)
    result=profile.repair_rollout_metadata(home,'/old/long-user','/new/u')
    assert result['offsets']['repaired_fields']==6
    audit=history.offset_plan(home)
    assert audit['status']=='passed' and audit['checked_fields']==6
    assert set(audit['branches'][LOGICAL])=={LOGICAL,BRANCH}
    assert audit['unregistered_history_ids']==[]
    for p in (home/'sessions').glob('*.jsonl'):
        rows=[json.loads(x) for x in p.read_bytes().splitlines()]
        assert rows[0]['payload']['cwd']=='/new/u/项目'
        assert rows[2]['payload']['text']=='Keep /old/long-user/项目 exactly'
    assert history.repair_offsets(home)['repaired_fields']==0


def test_old_rewrite_bug_is_detected_and_repaired_without_original_log(tmp_path):
    home=fixture(tmp_path)
    for p in (home/'sessions').glob('*.jsonl'):
        p.write_bytes(p.read_bytes().replace(b'/old/long-user',b'/new/u'))
    assert history.offset_plan(home)['status']=='failed'
    history.repair_offsets(home)
    assert history.offset_plan(home)['status']=='passed'


def test_corrupt_log_refuses_offset_updates(tmp_path):
    home=fixture(tmp_path);path=next((home/'sessions').glob('*.jsonl'))
    path.write_bytes(path.read_bytes().rstrip(b'\n'))
    before=(home/'thread_history_1.sqlite').read_bytes()
    with pytest.raises(ValueError,match='不能可靠'):
        history.repair_offsets(home)
    assert (home/'thread_history_1.sqlite').read_bytes()==before


def test_typed_move_and_file_uris_rebind_without_editing_commands_or_arguments():
    item={'changes':[{'path':'/old/a','kind':{'move_path':'/old/b'},'diff':'/old/unchanged'}],
          'runtime_workspace_roots':['/old/project'],'uri':'file:///old/image.png',
          'arguments':{'path':'/old/past-call'},'command':'cat /old/a'}
    actual=history.mapped_paths(item,'/old','/new')
    assert actual['changes'][0]['kind']['move_path']=='/new/b'
    assert actual['runtime_workspace_roots']==['/new/project']
    assert actual['uri']=='file:///new/image.png'
    assert actual['command']==item['command'] and actual['arguments']==item['arguments']
    assert actual['changes'][0]['diff']==item['changes'][0]['diff']


@pytest.mark.parametrize('problem',['','missing_branch','missing_last_item','wrong_status','wrong_body','cursor_cycle'])
def test_native_validation_reads_all_branches_pages_states_and_fields(tmp_path,problem):
    home=fixture(tmp_path)
    class Client:
        def request(self,method,params,**kwargs):
            assert params['threadId']==LOGICAL
            if method=='thread/turns/list':
                rows=[{'id':t,'status':'completed','completedAt':100} for t in ['base','new']]
                if problem=='missing_branch': rows.pop()
                if problem=='wrong_status': rows[0]['status']='interrupted'
                return {'data':rows}
            index=1 if params['cursor'] else 0
            turn=['base','new'][index]
            item={'id':turn+'-item','type':'agentMessage','text':('bad' if problem=='wrong_body' else 'reply '+turn),'newField':True}
            return {'data':[] if problem=='missing_last_item' and index else [{'turnId':turn,'item':item}],
                    'nextCursor':'next' if index==0 or problem=='cursor_cycle' else None}
    result=history.native_full_history(Client(),home)
    assert result['status']==('passed' if not problem else 'failed')
    if not problem:
        assert result['turns_checked']==result['items_checked']==2


def test_target_preservation_rejects_lost_or_changed_reply(tmp_path):
    home=fixture(tmp_path);before=tmp_path/'before';shutil.copytree(home,before)
    assert history.preservation(before,home,'/old','/new')['status']=='passed'
    with sqlite3.connect(home/'thread_history_1.sqlite') as c:
        c.execute('DELETE FROM thread_items WHERE turn_id=?',('new',))
    assert history.preservation(before,home,'/old','/new')['status']=='failed'


def test_host_alias_is_validated_and_does_not_disable_identity_check():
    args=migration.ssh_arguments({'host':'user@new-address','host_key_alias':'original-host'})
    assert '-oHostKeyAlias=original-host' in args and '-oStrictHostKeyChecking=yes' in args
    with pytest.raises(ValueError):
        migration.ssh_arguments({'host':'host','host_key_alias':'host;id'})


def test_compressed_ancestor_is_discovered_and_derived_without_losing_original(tmp_path):
    import zstandard
    home=fixture(tmp_path);plain=home/'sessions'/('rollout-'+LOGICAL+'.jsonl')
    compressed=plain.with_suffix('.jsonl.zst');original=zstandard.ZstdCompressor().compress(plain.read_bytes())
    compressed.write_bytes(original);plain.unlink()
    assert set(history.catalog(home)['branches'][LOGICAL])=={LOGICAL,BRANCH}
    result=profile.repair_rollout_metadata(home,'/old/long-user','/new/u')
    assert result['compressed_logs'][0]['plain_bytes']>0
    assert Path(result['compressed_logs'][0]['retained_original']).read_bytes()==original
    assert history.offset_plan(home)['status']=='passed'
    assert history.offset_plan(home)['history_base_offsets']['status']=='passed'


def test_attached_context_is_retained_but_not_counted_as_child_visible_turns(tmp_path):
    home=fixture(tmp_path);path=home/'sessions'/('rollout-'+LOGICAL+'_'+BRANCH+'.jsonl')
    lines=path.read_text().splitlines();meta=json.loads(lines[0]);meta['payload']['attached_thread']=True
    lines[0]=json.dumps(meta);path.write_text('\n'.join(lines)+'\n')
    assert history.catalog(home)['visible_segments'][LOGICAL]==[{'history_id':BRANCH,'end_ordinal_exclusive':None}]
    assert history.catalog(home)['logs'][BRANCH]['history_base']['thread_id']==LOGICAL


def delta_fixture(tmp_path):
    home=fixture(tmp_path)
    with sqlite3.connect(home/'thread_history_1.sqlite') as c:
        c.execute('ALTER TABLE thread_turns ADD COLUMN started_at INTEGER')
        c.execute('ALTER TABLE thread_turns ADD COLUMN final_agent_item_id TEXT')
        c.execute('ALTER TABLE thread_items ADD COLUMN item_type TEXT')
        c.execute("UPDATE thread_items SET item_type='agentMessage'")
    return home


def test_finished_delta_trims_active_tail_and_preserves_completed_target(tmp_path):
    source=delta_fixture(tmp_path);target=tmp_path/'target';shutil.copytree(source,target)
    filename='rollout-'+LOGICAL+'_'+BRANCH+'.jsonl'
    with (source/'sessions'/filename).open('a') as f:
        f.write(json.dumps({'ordinal':4,'type':'event_msg','payload':{'type':'task_started'}})+'\n')
    with sqlite3.connect(source/'thread_history_1.sqlite') as c:
        c.execute('INSERT INTO thread_turns(thread_id,turn_id,rollout_ordinal,status,started_at) VALUES(?,?,?,?,?)',
                  (BRANCH,'still-running',4,'inProgress',101))
    with sqlite3.connect(target/'thread_history_1.sqlite') as c:
        for table in ['thread_turns','thread_items','thread_history_projection_state']:
            c.execute('DELETE FROM '+table+' WHERE thread_id=?',(BRANCH,))
    (target/'sessions'/filename).unlink()
    package=tmp_path/'delta'
    result=history.capture_finished(source,history.terminal_coverage(target),package)
    assert len(result['branches'])==1 and result['pending_turns'][0]['turn_id']=='still-running'
    assert b'still-running' not in (package/result['branches'][0]['log_file']).read_bytes()
    report=history.merge_finished(target,package,str(target.parent))
    assert report['status']=='merged' and history.offset_plan(target)['status']=='passed'
    with sqlite3.connect(target/'thread_history_1.sqlite') as c:
        assert c.execute('SELECT count(*) FROM thread_turns').fetchone()[0]==2
        assert not c.execute("SELECT 1 FROM thread_turns WHERE turn_id='still-running'").fetchone()


def test_finished_delta_rejects_destination_divergence_and_tampered_package(tmp_path):
    source=delta_fixture(tmp_path);target=tmp_path/'target';shutil.copytree(source,target)
    package=tmp_path/'delta';manifest=history.capture_finished(source,{},package)
    with sqlite3.connect(target/'thread_history_1.sqlite') as c:
        c.execute('UPDATE thread_items SET item_json=? WHERE turn_id=?',(json.dumps({'id':'new-item','type':'agentMessage','text':'new target work'}),'new'))
    with pytest.raises(ValueError,match='目标已结束内容'):
        history.merge_finished(target,package,str(target.parent))
    file=package/manifest['branches'][0]['rows_file'];file.write_text(file.read_text()+'\n')
    with pytest.raises(ValueError,match='校验失败'):
        history.merge_finished(target,package,str(target.parent))


def test_compressed_guard_upgrade_requires_preserved_original_and_unchanged_plain(tmp_path):
    home=tmp_path/'home';logs=home/'sessions';logs.mkdir(parents=True)
    plain=logs/'old.jsonl';plain.write_bytes(b'old')
    baseline=history.log_signature(home)
    compressed=logs/'old.jsonl.zst';compressed.write_bytes(b'compressed')
    run=tmp_path/'run';original=run/'compressed-originals-1/sessions/old.jsonl.zst'
    original.parent.mkdir(parents=True);original.write_bytes(compressed.read_bytes())
    actual,proof=history.upgrade_compressed_guard(baseline,home,run)
    assert actual==history.log_signature(home) and proof['compressed_originals_sha256_checked']==1
    compressed.write_bytes(b'new')
    with pytest.raises(ValueError,match='无法与准备时原件核对'):
        history.upgrade_compressed_guard(baseline,home,run)
    compressed.write_bytes(original.read_bytes());plain.write_bytes(b'changed')
    with pytest.raises(ValueError,match='原有日志准备后变化'):
        history.upgrade_compressed_guard(baseline,home,run)


def test_legacy_in_place_repair_refuses_before_any_io():
    from asset_manager import migration_agent
    with pytest.raises(ValueError,match='原位路径修复已停用'):
        migration.repair_paths('unused')
    with pytest.raises(ValueError,match='原位路径修复已停用'):
        migration_agent.repair_active_paths({})


def test_offset_repair_requires_expanded_isolated_logs(tmp_path):
    (tmp_path/'sessions').mkdir()
    (tmp_path/'sessions/rollout.jsonl.zst').write_bytes(b'untouched')
    with pytest.raises(ValueError,match='隔离副本展开'):
        history.repair_offsets(tmp_path)
    assert (tmp_path/'sessions/rollout.jsonl.zst').read_bytes()==b'untouched'


def test_completion_refuses_old_sampled_acceptance(monkeypatch):
    monkeypatch.setattr(migration,'read',lambda _: {})
    monkeypatch.setattr(migration,'reports',lambda _: {n:{'status':'passed'} for n in
        ('verification.json','native-acceptance.json','workspace-acceptance.json')})
    with pytest.raises(ValueError,match='旧版抽样报告不能完成迁移'):
        migration.complete('unused')


def test_missing_physical_parent_never_passes_full_integrity(tmp_path):
    home=fixture(tmp_path)
    (home/'sessions'/('rollout-'+LOGICAL+'.jsonl')).unlink()
    result=history.offset_plan(home)
    assert result['status']=='failed' and result['missing_logs']==[LOGICAL]
    assert result['history_base_offsets']['status']=='failed'
    with pytest.raises(ValueError,match='截止位置不能可靠重建'):
        history.repair_offsets(home)
