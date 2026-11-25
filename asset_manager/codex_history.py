"""Branch-aware history validation for Atlas-derived Codex profiles.

The raw migration snapshot is evidence, never an editing target. Ordinals identify
records; byte offsets are derived from the *rewritten* log, not copied between
different byte streams. All scans are bounded independently of log size.
"""
import collections
import hashlib
import json
import os
import re
import sqlite3
import time
import shutil
import uuid
import contextlib
import io
from pathlib import Path

UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
ORDINAL = re.compile(rb'^\{[^\n]{0,180}?"ordinal"\s*:\s*(\d+)\s*,')
PATH_KEYS = {'cwd', 'path', 'root', 'roots', 'writable_roots', 'image_path',
    'file_path', 'workspace', 'worktree', 'attachment_path', 'local_path',
    'rollout_path', 'rootPaths', 'writableRoots', 'filePath', 'localPath',
    'common_dir', 'checkout_root', 'repo_root', 'runtime_workspace_roots',
    'move_path', 'movePath', 'scriptPath'}


def mapped_paths(value, source_home, target_home, aliases=None, key=''):
    """Map typed references only. Prose, commands, patches and outputs stay intact."""
    def path(v):
        if v in (aliases or {}):
            return aliases[v]
        if v == source_home or v.startswith(source_home + '/'):
            return target_home + v[len(source_home):]
        return v
    if isinstance(value, dict):
        return {(path(k) if key == 'changes' else k): (v if k == 'arguments' else mapped_paths(v, source_home, target_home, aliases, k))
                for k, v in value.items()}
    if isinstance(value, list):
        return [mapped_paths(v, source_home, target_home, aliases, key) for v in value]
    if isinstance(value, str):
        if key in PATH_KEYS:
            return path(value)
        if key in {'uri', 'url', 'image_url'} and value.startswith('file://'):
            return 'file://' + path(value[7:])
    return value


def connect(path):
    c = sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    return c


@contextlib.contextmanager
def open_log(path):
    path=Path(path)
    with path.open('rb') as raw:
        if path.suffix!='.zst':
            yield raw
        else:
            try:import zstandard
            except ImportError:raise ValueError('接收 Python 缺少 zstandard；请安装 Atlas requirements.lock 或配置 remote_python')
            with zstandard.ZstdDecompressor().stream_reader(raw) as stream:
                with io.BufferedReader(stream) as buffered:yield buffered


def expand_compressed(codex_home, archive):
    """Derive writable plain logs; retain compressed originals outside the profile."""
    home,archive=Path(codex_home),Path(archive)
    expanded=[]
    for directory in ['sessions','archived_sessions']:
        for path in sorted((home/directory).rglob('*.jsonl.zst')):
            if path.is_symlink() or not path.resolve().is_relative_to(home.resolve()):raise ValueError('压缩日志链接越界')
            plain=path.with_suffix('');relative=path.relative_to(home)
            existing=plain.exists()
            if not existing:
                temp=plain.with_name(plain.name+'.expand-'+uuid.uuid4().hex)
                try:
                    with open_log(path) as src,temp.open('xb') as dst:shutil.copyfileobj(src,dst,1024**2)
                    shutil.copystat(path,temp);os.replace(temp,plain)
                finally:temp.unlink(missing_ok=True)
            retained=archive/relative;retained.parent.mkdir(parents=True,exist_ok=True)
            if retained.exists():raise ValueError('已有压缩历史保留副本')
            os.rename(path,retained)
            expanded.append({'path':str(plain),'retained_original':str(retained),'used_existing_plain':existing,
                'compressed_bytes':retained.stat().st_size,'plain_bytes':plain.stat().st_size})
    return expanded


def catalog(codex_home):
    """Resolve physical branch IDs through log metadata, not state ID equality."""
    home = Path(codex_home)
    logs, errors = {}, []
    for directory in ('sessions', 'archived_sessions'):
        for p in sorted(p for p in (home / directory).rglob('*') if p.name.endswith(('.jsonl','.jsonl.zst'))):
            if p.is_symlink() or not p.resolve().is_relative_to(home.resolve()):
                errors.append({'path': str(p), 'reason': 'external_log_link'})
                continue
            ids = UUID.findall(p.name)
            with open_log(p) as f:
                raw = f.readline(4 * 1024**2 + 1)
            try:
                meta = json.loads(raw)
                logical = meta['payload']['id']
                if meta['type'] != 'session_meta':
                    raise ValueError('not session metadata')
            except (KeyError, ValueError, TypeError):
                errors.append({'path': str(p), 'reason': 'invalid_session_metadata'})
                continue
            ident = ids[-1] if ids else logical
            if ident in logs:
                if p.suffix=='.zst' and str(p.with_suffix(''))==logs[ident]['path']:
                    continue  # Native prefers its expanded, subsequently appended log.
                errors.append({'history_id': ident, 'reason': 'ambiguous_branch_logs'})
                continue
            logs[ident] = {'history_id': ident, 'logical_id': logical, 'path': str(p),
                          'relative': p.relative_to(home).as_posix(), 'bytes': p.stat().st_size,
                          'history_base': meta['payload'].get('history_base'),
                          'attached_thread': bool(meta['payload'].get('attached_thread'))}
    with connect(home / 'state_5.sqlite') as c:
        registered = {r['id']:dict(r) for r in c.execute('SELECT * FROM threads')}
    with connect(home / 'thread_history_1.sqlite') as c:
        history_ids = {r[0] for r in c.execute('SELECT DISTINCT thread_id FROM thread_turns')}
    owners = {i: logs[i]['logical_id'] if i in logs else i for i in history_ids}
    branches = collections.defaultdict(list)
    visible={}
    for logical,row in registered.items():
        ids=UUID.findall(Path(row.get('rollout_path') or '').name)
        head=ids[-1] if ids and ids[-1] in logs else logical
        segments=[];seen=set();upper=None
        while head:
            if head in seen:
                errors.append({'history_id':head,'reason':'history_base_cycle'});break
            seen.add(head)
            segments.append({'history_id':head,'end_ordinal_exclusive':upper})
            if head in history_ids:
                branches[logical].append(head);owners[head]=logical
            if logs.get(head,{}).get('attached_thread'):
                # Attached chats inherit model context, but expose their own
                # turns. The parent is independently checked under its ID.
                break
            base=logs.get(head,{}).get('history_base')
            if not base:break
            head,upper=base['thread_id'],base['end_ordinal_exclusive']
        if logical in branches:visible[logical]=segments
    return {'logs': logs, 'branches': dict(branches), 'owners': owners,
            'visible_segments':visible,
            'unregistered_history_ids': sorted(i for i, owner in owners.items() if owner not in registered),
            'missing_logs': sorted(history_ids - logs.keys()), 'errors': errors}


def base_offsets(codex_home, repair=False):
    """History-base byte bounds live in child JSONL metadata, outside SQLite."""
    inventory=catalog(codex_home);logs=inventory['logs'];done=set();visiting=set()
    changes,missing,errors=[],[],[]
    def visit(ident):
        if ident in done:return
        if ident in visiting:raise ValueError('历史分支引用循环')
        visiting.add(ident);log=logs[ident];base=log.get('history_base')
        if base:
            parent=base['thread_id'];upper=base['end_ordinal_exclusive']
            if parent not in logs:
                missing.append({'history_id':ident,'parent':parent,'reason':'source_history_base_log_unavailable'})
            else:
                visit(parent)
                found,scan=coordinates(logs[parent]['path'],{upper,upper-1})
                after=found[upper][0] if upper in found else (found[upper-1][1] if upper-1 in found else None)
                if after is None or scan['errors']:
                    errors.append({'history_id':ident,'reason':'history_base_boundary_unresolved'})
                elif base.get('end_byte_offset')!=after:
                    changes.append({'history_id':ident,'parent':parent,'before':base.get('end_byte_offset'),'after':after})
                    if repair:
                        p=Path(log['path']);temp=p.with_name(p.name+'.base-'+uuid.uuid4().hex)
                        try:
                            with p.open('rb') as src,temp.open('xb') as dst:
                                first=json.loads(src.readline(4*1024**2))
                                first['payload']['history_base']['end_byte_offset']=after
                                dst.write((json.dumps(first,ensure_ascii=False,separators=(',',':'))+'\n').encode())
                                shutil.copyfileobj(src,dst,1024**2)
                            shutil.copystat(p,temp);os.replace(temp,p)
                        finally:temp.unlink(missing_ok=True)
        visiting.remove(ident);done.add(ident)
    for ident in logs:visit(ident)
    return {'status':'failed' if errors or missing or (changes and not repair) else 'passed',
            'changes':changes,'source_missing_base_logs':missing,'errors':errors}


def coordinates(path, wanted):
    """Read only small prefixes; skip large image/tool records in bounded chunks."""
    found, end, previous, last_end, records, errors, warnings = {}, 0, None, 0, 0, [], []
    with open_log(path) as f:
        while True:
            start = f.tell()
            prefix = f.readline(512)
            if not prefix:
                break
            tail = prefix
            while not tail.endswith(b'\n'):
                tail = f.readline(1024**2)
                if not tail:
                    errors.append({'offset': start, 'reason': 'incomplete_record'})
                    break
            end = f.tell()
            match = ORDINAL.match(prefix)
            if not match:
                errors.append({'offset': start, 'reason': 'missing_ordinal'})
                continue
            ordinal = int(match[1])
            if previous is not None and ordinal <= previous:
                # Codex can append a replacement compaction with the same ordinal.
                # Do not discard it or confuse it with a unique indexed boundary.
                issue = {'offset': start, 'ordinal': ordinal, 'reason': 'non_monotonic_ordinal'}
                (errors if ordinal in wanted or ordinal < previous else warnings).append(issue)
            if ordinal in wanted:
                found[ordinal] = (start, end)
            previous, last_end = ordinal, end
            records += 1
    return found, {'last_ordinal': previous, 'end': last_end, 'records': records, 'errors': errors, 'warnings': warnings}


def offset_plan(codex_home):
    home = Path(codex_home)
    inventory = catalog(home)
    errors, changes, inspected, warnings = list(inventory['errors']), [], 0, []
    errors.extend({'history_id':i,'reason':'missing_physical_log'} for i in inventory['missing_logs'])
    errors.extend({'history_id':i,'reason':'unregistered_history_not_covered'} for i in inventory['unregistered_history_ids'])
    with connect(home / 'thread_history_1.sqlite') as c:
        columns = {r[1] for r in c.execute('PRAGMA table_info(thread_turns)')}
        if not {'rollout_ordinal', 'rollout_byte_offset', 'rollout_end_ordinal', 'rollout_end_byte_offset'} <= columns:
            raise ValueError('历史 schema 不支持完整偏移验收')
        for ident, log in inventory['logs'].items():
            turns = [dict(r) for r in c.execute('SELECT * FROM thread_turns WHERE thread_id=?', (ident,))]
            projection = c.execute('SELECT * FROM thread_history_projection_state WHERE thread_id=?', (ident,)).fetchone()
            if not turns and not projection:
                continue
            wanted = {r[k] for r in turns for k in ('rollout_ordinal', 'rollout_end_ordinal') if r[k] is not None}
            if projection:
                wanted |= {projection['next_rollout_ordinal'], projection['next_rollout_ordinal'] - 1}
            found, scan = coordinates(log['path'], wanted)
            errors.extend({'history_id': ident, **e} for e in scan['errors'])
            warnings.extend({'history_id': ident, **e} for e in scan['warnings'])
            for row in turns:
                for col, ordinal, side in [('rollout_byte_offset', row['rollout_ordinal'], 0),
                    ('rollout_end_byte_offset', row['rollout_end_ordinal'], 1)]:
                    if ordinal is None and row[col] is None:
                        continue
                    if ordinal not in found:
                        errors.append({'history_id': ident, 'turn_id': row['turn_id'], 'field': col, 'reason': 'ordinal_not_in_log'})
                        continue
                    inspected += 1
                    after = found[ordinal][side]
                    if row[col] != after:
                        changes.append({'table': 'thread_turns', 'history_id': ident, 'turn_id': row['turn_id'],
                                        'field': col, 'before': row[col], 'after': after})
            if projection:
                ordinal = projection['next_rollout_ordinal']
                after = found[ordinal][0] if ordinal in found else (
                    found[ordinal - 1][1] if ordinal - 1 in found else None)
                if after is None:
                    errors.append({'history_id': ident, 'reason': 'projection_ordinal_not_in_log'})
                else:
                    inspected += 1
                    before = projection['next_rollout_byte_offset']
                    if before != after:
                        changes.append({'table': 'thread_history_projection_state', 'history_id': ident,
                                        'field': 'next_rollout_byte_offset', 'before': before, 'after': after})
    bases=base_offsets(home)
    return {'status': 'failed' if errors or changes or bases['status']!='passed' else 'passed', 'schema_version': 1,
            'history_base_offsets':bases,
            'checked_fields': inspected, 'changes': changes, 'errors': errors, 'warnings': warnings,
            'branches': inventory['branches'], 'missing_logs': inventory['missing_logs'],
            'unregistered_history_ids': inventory['unregistered_history_ids'], 'checked_at': time.time()}


def repair_offsets(codex_home):
    """Caller owns an isolated, backed-up profile with no active writers."""
    if any(p for name in ("sessions", "archived_sessions") for p in (Path(codex_home)/name).rglob("*.jsonl.zst")):
        raise ValueError("请先在隔离副本展开压缩日志，再重建偏移")
    bases=base_offsets(codex_home,repair=True)
    if bases['status']!='passed':raise ValueError('历史分支截止位置不能可靠重建')
    report = offset_plan(codex_home)
    report['history_base_offsets']=bases
    if report['errors']:
        raise ValueError('日志不能可靠重建偏移：' + json.dumps(report['errors'][:5], ensure_ascii=False))
    with sqlite3.connect(Path(codex_home) / 'thread_history_1.sqlite') as c:
        c.execute('BEGIN IMMEDIATE')
        for row in report['changes']:
            if row['table'] == 'thread_turns':
                c.execute('UPDATE thread_turns SET ' + row['field'] + '=? WHERE thread_id=? AND turn_id=?',
                          (row['after'], row['history_id'], row['turn_id']))
            else:
                c.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=? WHERE thread_id=?',
                          (row['after'], row['history_id']))
        if c.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('重建偏移后的历史数据库检查失败')
    return {**report, 'status': 'repaired', 'repaired_fields': len(report['changes'])}


def repair_item_paths(codex_home, source_home, target_home, aliases=None):
    changed = collections.Counter()
    with sqlite3.connect(Path(codex_home) / 'thread_history_1.sqlite') as c:
        c.execute('BEGIN IMMEDIATE')
        rows = c.execute("SELECT thread_id,turn_id,item_id,item_type,item_json FROM thread_items WHERE item_type IN ('commandExecution','fileChange','imageView','userMessage')")
        for thread, turn, item, kind, raw in rows:
            original = json.loads(raw)
            mapped = mapped_paths(original, source_home, target_home, aliases)
            if mapped != original:
                c.execute('UPDATE thread_items SET item_json=? WHERE thread_id=? AND turn_id=? AND item_id=?',
                    (json.dumps(mapped, ensure_ascii=False, separators=(',', ':')), thread, turn, item))
                changed[kind] += 1
    return dict(changed)


def subset_difference(expected, actual, prefix=''):
    """Native versions may add fields, but no source field may silently differ."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key, value in expected.items():
            if key not in actual:
                return prefix + '/' + key + ':missing'
            error = subset_difference(value, actual[key], prefix + '/' + key)
            if error:
                return error
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return prefix + ':length'
        for i, (a, b) in enumerate(zip(expected, actual)):
            error = subset_difference(a, b, prefix + '/' + str(i))
            if error:
                return error
    elif expected != actual:
        return prefix or ':value'
    return None


def preservation(before, after, source_home, target_home, aliases=None):
    """Prove no target row was lost or altered outside the declared repairs."""
    errors, counts = [], {}
    with connect(Path(before) / 'thread_history_1.sqlite') as old, connect(Path(after) / 'thread_history_1.sqlite') as new:
        for table in ('thread_turns', 'thread_items', 'thread_history_projection_state', 'thread_realtime_items'):
            info = list(old.execute('PRAGMA table_info(' + table + ')'))
            if not info:
                continue
            keys = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
            order = ','.join(keys)
            left = old.execute('SELECT * FROM ' + table + ' ORDER BY ' + order)
            right = iter(new.execute('SELECT * FROM ' + table + ' ORDER BY ' + order))
            counts[table] = 0
            for raw in left:
                actual = next(right, None)
                expected = dict(raw)
                counts[table] += 1
                if actual is None:
                    errors.append({'table': table, 'reason': 'row_missing'})
                    break
                actual = dict(actual)
                for key in ('rollout_byte_offset', 'rollout_end_byte_offset', 'next_rollout_byte_offset'):
                    expected.pop(key, None); actual.pop(key, None)
                if 'item_json' in expected:
                    parsed = json.loads(expected['item_json'])
                    expected['item_json'] = mapped_paths(parsed, source_home, target_home, aliases) if parsed.get('type') in {
                        'commandExecution','fileChange','imageView','userMessage'} else parsed
                    actual['item_json'] = json.loads(actual['item_json'])
                if expected != actual:
                    if len(errors) < 40:
                        errors.append({'table': table, 'key': [raw[k] for k in keys],
                                       'field': subset_difference(expected, actual)})
            if next(right, None) is not None:
                errors.append({'table': table, 'reason': 'unexpected_added_row'})
    return {'status': 'failed' if errors else 'passed', 'checked_rows': counts, 'errors': errors,
            'scope': 'every_preexisting_target_row_only_declared_typed_paths_and_offsets_may_change'}


def log_signature(home):
    """Fast optimistic guard; copies and full validation retain the actual bytes."""
    home = Path(home)
    return {p.relative_to(home).as_posix(): [p.stat().st_size, p.stat().st_mtime_ns]
            for directory in ('sessions', 'archived_sessions')
            for p in (home / directory).rglob('*') if p.name.endswith(('.jsonl','.jsonl.zst')) and not p.is_symlink()}


def upgrade_compressed_guard(previous, home, runroot):
    """Upgrade a pre-zstd preparation guard only against retained original bytes."""
    home,runroot=Path(home),Path(runroot);current=log_signature(home)
    if any(current.get(k)!=v for k,v in previous.items()):
        raise ValueError('原有日志准备后变化，不能升级检查基线')
    additions=set(current)-set(previous)
    retained={}
    for archive in runroot.glob('compressed-originals-*'):
        for p in archive.rglob('*.jsonl.zst'):
            retained.setdefault(p.relative_to(archive).as_posix(),[]).append(p)
    for relative in additions:
        if not relative.endswith('.jsonl.zst') or not any(file_sha(home/relative)==file_sha(p) for p in retained.get(relative,[])):
            raise ValueError('新增日志无法与准备时原件核对：'+relative)
    return current,{'compressed_originals_sha256_checked':len(additions),'no_new_conversation_accepted':True}


def terminal_coverage(codex_home):
    """A completion boundary is a set of closed turns, never wall time alone."""
    with connect(Path(codex_home)/'thread_history_1.sqlite') as c:
        counts = {(r[0],r[1]):r[2] for r in c.execute('SELECT thread_id,turn_id,count(*) FROM thread_items GROUP BY thread_id,turn_id')}
        return {r['thread_id']+'/'+r['turn_id']:[r['status'],r['completed_at'],r['final_agent_item_id'],counts.get((r['thread_id'],r['turn_id']),0)]
                for r in c.execute('SELECT * FROM thread_turns') if r['completed_at'] is not None}


def file_sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024**2),b''):
            h.update(b)
    return h.hexdigest()


def capture_finished(codex_home, target_coverage, output):
    """Export closed history tails from one read transaction, trim logs to it.

    An ongoing migration chat is explicitly pending. Its future final reply is
    not claimed captured merely because a file hash passed earlier.
    """
    home, output=Path(codex_home),Path(output)
    output.mkdir(parents=True,exist_ok=False,mode=0o700)
    inventory=catalog(home)
    report={'schema_version':1,'source_home':str(home.parent),'captured_at':time.time(),
            'scope':'closed_turns_only','branches':[],'pending_turns':[],'files':{}}
    with connect(home/'thread_history_1.sqlite') as c, connect(home/'state_5.sqlite') as state:
        c.execute('BEGIN')
        counts={(r[0],r[1]):r[2] for r in c.execute('SELECT thread_id,turn_id,count(*) FROM thread_items GROUP BY thread_id,turn_id')}
        turns=[dict(r) for r in c.execute('SELECT * FROM thread_turns')]
        changed=set()
        for r in turns:
            if r['completed_at'] is None:
                report['pending_turns'].append({k:r[k] for k in ['thread_id','turn_id','status','started_at']})
                continue
            value=[r['status'],r['completed_at'],r['final_agent_item_id'],counts.get((r['thread_id'],r['turn_id']),0)]
            if target_coverage.get(r['thread_id']+'/'+r['turn_id'])!=value:
                changed.add(r['thread_id'])
        for ident in sorted(changed):
            if ident not in inventory['logs']:
                raise ValueError('有新增结束轮次但缺少来源日志：'+ident)
            log=inventory['logs'][ident]
            closed=[r for r in turns if r['thread_id']==ident and r['completed_at'] is not None and r['rollout_end_byte_offset'] is not None]
            if not closed:
                raise ValueError('无法确定已结束轮次日志边界：'+ident)
            last=max(closed,key=lambda r:r['rollout_end_ordinal'])
            cutoff=last['rollout_end_byte_offset']; ordinal=last['rollout_end_ordinal']
            selected=[r for r in turns if r['thread_id']==ident and r['rollout_ordinal']<=ordinal]
            turn_ids={r['turn_id'] for r in selected}
            relative='logs/'+Path(log['relative']).name.removesuffix('.zst')
            dest=output/relative;dest.parent.mkdir(exist_ok=True)
            with open_log(log['path']) as src,dest.open('xb') as dst:
                remaining=cutoff
                while remaining:
                    block=src.read(min(1024**2,remaining))
                    if not block:raise ValueError('来源日志短于数据库边界')
                    dst.write(block);remaining-=len(block)
            with dest.open('rb') as f:
                f.seek(-1,os.SEEK_END)
                if f.read(1)!=b'\n':raise ValueError('来源截止位置不是完整记录边界')
            rows_name='rows-'+ident+'.jsonl'
            with (output/rows_name).open('x') as f:
                def write(table,row):
                    f.write(json.dumps({'table':table,'row':dict(row)},ensure_ascii=False,separators=(',',':'))+'\n')
                for r in selected:write('thread_turns',r)
                for r in c.execute('SELECT * FROM thread_items WHERE thread_id=?',(ident,)):
                    if r['turn_id'] in turn_ids:write('thread_items',r)
                write('thread_history_projection_state',{'thread_id':ident,'next_rollout_ordinal':ordinal+1,'next_rollout_byte_offset':cutoff})
            row=state.execute('SELECT id,rollout_path,cwd FROM threads WHERE id=?',(log['logical_id'],)).fetchone()
            if not row:raise ValueError('新增轮次没有可关联的来源对话')
            report['branches'].append({'history_id':ident,'logical_id':log['logical_id'],'relative':log['relative'].removesuffix('.zst'),
                'rows_file':rows_name,'log_file':relative,'cutoff_bytes':cutoff,'last_ordinal':ordinal,
                'turns':len(selected),'latest_source_branch':Path(row['rollout_path']).name.removesuffix('.zst')==Path(log['path']).name.removesuffix('.zst')})
            for name in [rows_name,relative]:report['files'][name]=file_sha(output/name)
    (output/'manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return report


def merge_finished(codex_home, package, target_home, aliases=None):
    """Apply verified closed tails to an isolated profile; reject destination forks."""
    home,package=Path(codex_home),Path(package)
    manifest=json.loads((package/'manifest.json').read_text());source_home=manifest['source_home']
    for name,sha in manifest['files'].items():
        path=package/name
        if not path.resolve().is_relative_to(package.resolve()) or path.is_symlink() or file_sha(path)!=sha:
            raise ValueError('尾差文件校验失败：'+name)
    aliases=dict(aliases or {})
    report={'status':'merged','branches':[],'pending_turns':manifest['pending_turns'],
            'captured_at':manifest['captured_at'],'scope':manifest['scope'],'state_paths':{}}
    with sqlite3.connect(home/'thread_history_1.sqlite') as c, sqlite3.connect(home/'state_5.sqlite') as state:
        c.row_factory=sqlite3.Row;c.execute('BEGIN IMMEDIATE')
        for branch in manifest['branches']:
            ident=branch['history_id'];logical=branch['logical_id']
            if not state.execute('SELECT 1 FROM threads WHERE id=?',(logical,)).fetchone():
                raise ValueError('尾差出现尚未迁入的对话，需要刷新迁移方案：'+logical)
            def entries():
                with (package/branch['rows_file']).open() as f:
                    for line in f:
                        yield json.loads(line)
            turn_rows={e['row']['turn_id']:e['row'] for e in entries() if e['table']=='thread_turns'}
            prior_turns={r['turn_id']:dict(r) for r in c.execute('SELECT * FROM thread_turns WHERE thread_id=?',(ident,))}
            # Preserve already recovered attachment references when every other
            # source field agrees; do not resurrect expired temporary paths.
            for entry in entries():
                row=entry['row']
                if entry['table']!='thread_items' or row.get('item_type') not in {'imageView','userMessage'}:continue
                old=c.execute('SELECT item_json FROM thread_items WHERE thread_id=? AND turn_id=? AND item_id=?',
                              (ident,row['turn_id'],row['item_id'])).fetchone()
                if not old:continue
                a,b=json.loads(row['item_json']),json.loads(old[0])
                if a.get('type')=='imageView':pairs=[(a,b)]
                else:pairs=list(zip(a.get('content',[]),b.get('content',[])))
                for source,target in pairs:
                    if isinstance(source.get('path'),str) and isinstance(target.get('path'),str) and Path(target['path']).is_file():
                        if {k:v for k,v in source.items() if k!='path'}=={k:v for k,v in target.items() if k!='path'}:
                            aliases[source['path']]=target['path']
            destination=home/branch['relative']
            if not destination.resolve().is_relative_to(home.resolve()) or destination.is_symlink():
                raise ValueError('尾差目标日志越界')
            destination.parent.mkdir(parents=True,exist_ok=True)
            temp=destination.with_name(destination.name+'.delta-'+uuid.uuid4().hex)
            target=destination.open('rb') if destination.exists() else None
            extras=[]
            def record(stream):
                raw=stream.readline(128*1024**2+1) if stream else b''
                if len(raw)>128*1024**2:raise ValueError('尾差单条记录超限')
                return (raw,json.loads(raw)) if raw else (b'',None)
            try:
                raw_old,old=record(target)
                def comparable(value):
                    value=mapped_paths(value,source_home,target_home,aliases)
                    if value and value.get('type')=='session_meta':
                        # This is a derived byte coordinate in the parent log.
                        # Parent identity and the ordinal boundary remain exact.
                        base=value.get('payload',{}).get('history_base')
                        if isinstance(base,dict):base.pop('end_byte_offset',None)
                    return value
                with (package/branch['log_file']).open('rb') as src,temp.open('xb') as out:
                    while True:
                        raw,new=record(src)
                        if not raw:break
                        normalized=mapped_paths(new,source_home,target_home,aliases)
                        while old is not None and comparable(old)!=comparable(new):
                            if old.get('payload',{}).get('type')!='thread_settings_applied':
                                raise ValueError('目标存在分叉的历史内容，拒绝覆盖：'+ident+' ordinal='+str(old.get('ordinal')))
                            extras.append(old);raw_old,old=record(target)
                        if old is not None:raw_old,old=record(target)
                        out.write((json.dumps(normalized,ensure_ascii=False,separators=(',',':'))+'\n').encode())
                    while old is not None:
                        if old.get('payload',{}).get('type')!='thread_settings_applied':
                            raise ValueError('目标有来源截止点之后的对话内容，需合并分叉')
                        extras.append(old);raw_old,old=record(target)
                    for index,item in enumerate(extras):
                        item['ordinal']=branch['last_ordinal']+1+index
                        out.write((json.dumps(item,ensure_ascii=False,separators=(',',':'))+'\n').encode())
                source_turns=set(turn_rows)
                old_turns={r[0] for r in c.execute('SELECT turn_id FROM thread_turns WHERE thread_id=?',(ident,))}
                if old_turns-source_turns:raise ValueError('目标有来源未包含的轮次，拒绝覆盖')
                row_count=0
                for entry in entries():
                    row_count+=1
                    table,row=entry['table'],dict(entry['row'])
                    if table not in {'thread_turns','thread_items','thread_history_projection_state'}:raise ValueError('尾差包含未知表')
                    if row['thread_id']!=ident:raise ValueError('尾差分支标识不一致')
                    schema=list(c.execute('PRAGMA table_info('+table+')'));cols={r[1] for r in schema}
                    if set(row)!=cols:raise ValueError('尾差 schema 不兼容')
                    keys=[r[1] for r in sorted(schema,key=lambda r:r[5]) if r[5]]
                    if table=='thread_items':
                        item=json.loads(row['item_json'])
                        row['item_json']=json.dumps(mapped_paths(item,source_home,target_home,aliases) if item.get('type') in {'commandExecution','fileChange','imageView','userMessage'} else item,ensure_ascii=False,separators=(',',':'))
                    if table=='thread_turns' and row['status'] not in {'completed','failed','interrupted'}:row['status']='interrupted'
                    where=' AND '.join(k+'=?' for k in keys)
                    oldrow=c.execute('SELECT * FROM '+table+' WHERE '+where,[row[k] for k in keys]).fetchone()
                    if oldrow and table!='thread_history_projection_state':
                        want,got=dict(row),dict(oldrow)
                        for field in ['rollout_byte_offset','rollout_end_byte_offset']:want.pop(field,None);got.pop(field,None)
                        if table=='thread_items':want['item_json']=json.loads(want['item_json']);got['item_json']=json.loads(got['item_json'])
                        old_parent=prior_turns.get(row['turn_id'])
                        if want!=got and old_parent and old_parent['completed_at'] is not None:
                            raise ValueError('目标已结束内容与来源不同，拒绝覆盖：'+str([row[k] for k in keys]))
                    c.execute('INSERT OR REPLACE INTO '+table+'('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',list(row.values()))
                os.replace(temp,destination)
                if branch['latest_source_branch']:
                    state.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(destination),logical))
                    report['state_paths'][logical]=branch['relative']
                report['branches'].append({'history_id':ident,'turns':len(source_turns),'rows':row_count,'preserved_target_settings':len(extras)})
            finally:
                if target:target.close()
                temp.unlink(missing_ok=True)
        c.commit();state.commit()
    report['offsets']=repair_offsets(home)
    return report


def native_full_history(client, codex_home, progress=None):
    """Page every registered logical chat, including its retained history branches."""
    inventory = catalog(codex_home)
    errors, summaries, total_items, total_turns, context_placeholders = [], [], 0, 0, []
    errors.extend({'history_id':i,'reason':'unregistered_history_not_covered'} for i in inventory['unregistered_history_ids'])
    with connect(Path(codex_home) / 'thread_history_1.sqlite') as c:
        c.execute('BEGIN')
        for logical, branch_ids in sorted(inventory['branches'].items()):
            clauses=[];parameters=[]
            for segment in inventory['visible_segments'][logical]:
                clause='thread_id=?';parameters.append(segment['history_id'])
                if segment['end_ordinal_exclusive'] is not None:
                    clause+=' AND rollout_ordinal<?';parameters.append(segment['end_ordinal_exclusive'])
                clauses.append('('+clause+')')
            predicate='('+' OR '.join(clauses)+')'
            turns = {r['turn_id']: dict(r) for r in c.execute(
                'SELECT * FROM thread_turns WHERE '+predicate, parameters)}
            head=inventory['logs'].get(inventory['visible_segments'][logical][0]['history_id'],{})
            if head.get('attached_thread') and head.get('history_base'):
                parent=head['history_base']['thread_id']
                for tid,row in list(turns.items()):
                    if row['status']!='interrupted' or row.get('first_user_item_id') or row.get('final_agent_item_id'):
                        continue
                    own=c.execute('SELECT count(*) FROM thread_items WHERE thread_id=? AND turn_id=?',(row['thread_id'],tid)).fetchone()[0]
                    inherited=c.execute('SELECT 1 FROM thread_turns WHERE thread_id=? AND turn_id=?',(parent,tid)).fetchone()
                    if not own and inherited:
                        context_placeholders.append({'thread_id':logical,'turn_id':tid,'parent':parent,
                            'reason':'attached_parent_abort_without_child_content','retained_in_database':True})
                        del turns[tid]
            # Never materialize all item bodies: large histories exceed RAM.
            expected_count = c.execute('SELECT count(*) FROM thread_items WHERE '+predicate, parameters).fetchone()[0]
            actual_turns, seen_items, cursor, cursors = {}, set(), None, set()
            try:
                while True:
                    result = client.request('thread/turns/list', {'threadId': logical, 'limit': 100,
                        'cursor': cursor, 'itemsView': 'notLoaded'}, timeout=120)
                    for turn in result.get('data', []):
                        if turn['id'] in actual_turns:
                            raise ValueError('原生轮次分页重复')
                        actual_turns[turn['id']] = turn
                    cursor = result.get('nextCursor')
                    if not cursor:
                        break
                    if cursor in cursors:
                        raise ValueError('原生轮次游标循环')
                    cursors.add(cursor)
                if actual_turns.keys() != turns.keys():
                    raise ValueError('原生轮次集合不一致：expected=%s actual=%s missing=%s' % (
                        len(turns), len(actual_turns), sorted(turns.keys()-actual_turns.keys())[:3]))
                for ident, row in turns.items():
                    native = actual_turns[ident]
                    if native.get('status') != row['status'] or native.get('completedAt') != row['completed_at']:
                        raise ValueError('原生轮次结束状态不一致：' + ident)
                cursor, cursors = None, set()
                while True:
                    result = client.request('thread/items/list', {'threadId': logical, 'limit': 20, 'cursor': cursor}, timeout=120)
                    for record in result.get('data', []):
                        item, turn_id = record.get('item', record), record.get('turnId')
                        key = (turn_id, item.get('id'))
                        if key in seen_items:
                            raise ValueError('原生内容分页重复')
                        seen_items.add(key)
                        expected = c.execute('SELECT item_json FROM thread_items WHERE '+predicate+' AND turn_id=? AND item_id=?',
                                             parameters + [turn_id, item.get('id')]).fetchall()
                        if len(expected) != 1:
                            raise ValueError('原生内容无法唯一关联数据库：' + str(key))
                        difference = subset_difference(json.loads(expected[0][0]), item)
                        if difference:
                            raise ValueError('原生内容字段不一致：%s %s' % (key, difference))
                    cursor = result.get('nextCursor')
                    if not cursor:
                        break
                    if cursor in cursors:
                        raise ValueError('原生内容游标循环')
                    cursors.add(cursor)
                    if progress:
                        progress({'thread_id': logical, 'threads_checked': len(summaries),
                                  'items_checked': total_items + len(seen_items)})
                if len(seen_items) != expected_count:
                    raise ValueError('原生内容总数不一致：expected=%s actual=%s' % (expected_count, len(seen_items)))
                summaries.append({'id': logical, 'branches': branch_ids, 'turns': len(turns), 'items': len(seen_items)})
                total_items += len(seen_items)
                total_turns += len(turns)
            except (ValueError, KeyError, TypeError) as exc:
                errors.append({'id': logical, 'error': str(exc)[:1200]})
    return {'status': 'failed' if errors or inventory['errors'] else 'passed', 'schema_version': 1,
            'scope': 'all_registered_branches_all_turns_all_item_source_fields',
            'threads': summaries, 'turns_checked': total_turns, 'items_checked': total_items,
            'context_placeholders':context_placeholders,
            'errors': errors + inventory['errors'], 'unregistered_history_ids': inventory['unregistered_history_ids'],
            'model_turns_started': False, 'checked_at': time.time()}
