"""Read-only Codex history adapter. Only public messages and explicit tool events.

The local projection is not a stable public API. Schema checks fail visibly; never
open an auth file, execute a recorded command, or ingest reasoning items.
"""
from contextlib import contextmanager
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote
from . import core, db

TYPES = ('userMessage', 'agentMessage', 'fileChange', 'commandExecution')
LIMIT = 16000

def clean(value, limit=LIMIT):
    text = str(value or '')
    text = re.sub(r'<(?:environment_context|in-app-browser-context|permissions instructions)[\s\S]*?</(?:environment_context|in-app-browser-context|permissions)>', '', text)
    text = re.sub(r'(?i)\b(api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[\"\']?[^\s\"\']+', r'\1=[REDACTED]', text)
    text = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,})', '[REDACTED]', text)
    return text.strip()[:limit]

def iso(value):
    return datetime.fromtimestamp(float(value or 0), timezone.utc).isoformat()

def source_root():
    value = db.setting('codex_source_root', os.environ.get('ASSET_CODEX_HOME', str(Path.home()/'.codex')))
    return Path(value).expanduser().resolve() if value else None

@contextmanager
def readonly(path):
    con = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    try: yield con
    finally: con.close()

def match_project(path, projects):
    if not path or not Path(path).is_absolute(): return None
    path = os.path.normpath(path)
    for p in sorted(projects, key=lambda x:len(x['path']), reverse=True):
        root = os.path.normpath(p['path'])
        if path == root or path.startswith(root+os.sep): return p
    return None

def parse_turn(turn, items, session, projects, known_files=None):
    """Build evidence without claiming that temporal co-occurrence is dependency."""
    request=[]; updates=[]; summary=[]; operations=[]; artifacts=[]
    def mentions(text,evidence):
        if known_files is None:return
        candidates=re.findall(r'\]\(([^)]+)\)|`([^`\n]+)`',text)
        for link,code in candidates:
            token=unquote((link or code).strip().strip('<>'))
            token=re.sub(r':\d+(?::\d+)?$','',token).split('#',1)[0]
            if not token or '://' in token or '\n' in token:continue
            absolute=os.path.normpath(token if os.path.isabs(token) else os.path.join(session['cwd'],token))
            project=match_project(absolute,projects)
            if not project or core.secret(absolute):continue
            rel=Path(absolute).relative_to(project['path']).as_posix()
            if (project['id'],rel) not in known_files:continue
            artifacts.append({'project_id':project['id'],'path':rel,'action':'mentioned','body':{'evidence':evidence,'status':'referenced','diff':'','diff_truncated':False,'added_lines':0,'removed_lines':0,'evidence_kind':'message_link','explanation':'公开会话消息明确链接或提及此路径；不单独证明文件生成、修改成功或设计派生关系。'}})
    for row in items:
        item=json.loads(row['item_json']); kind=item.get('type'); evidence=row['item_id']
        if kind=='userMessage':
            request.extend(clean(x.get('text','')) for x in item.get('content',[]) if x.get('type')=='text')
            for x in item.get('content',[]):
                if x.get('type')=='text':mentions(x.get('text',''),evidence)
        elif kind=='agentMessage':
            (summary if item.get('phase')=='final' else updates).append(clean(item.get('text','')))
            mentions(item.get('text',''),evidence)
        elif kind=='commandExecution':
            operations.append({'evidence':evidence,'command':clean(item.get('command',''),600),
                               'status':item.get('status'),'exit_code':item.get('exitCode'), 'duration_ms':item.get('durationMs')})
        elif kind=='fileChange':
            for change in item.get('changes',[]):
                path=change.get('path',''); path=os.path.normpath(path if os.path.isabs(path) else os.path.join(session['cwd'],path))
                p=match_project(path,projects)
                if not p or core.secret(path): continue
                rel=Path(path).relative_to(p['path']).as_posix()
                if any(x in core.EXCLUDE_DIRS for x in Path(rel).parts):continue
                action=change.get('kind',{});action=action.get('type','update') if isinstance(action,dict) else str(action)
                diff=clean(change.get('diff',''))
                artifacts.append({'project_id':p['id'],'path':rel,'action':action,
                    'body':{'evidence':evidence,'status':item.get('status'),'diff':diff,'evidence_kind':'file_change',
                            'diff_truncated':len(str(change.get('diff','')))>LIMIT,
                            'added_lines':sum(l.startswith('+') and not l.startswith('+++') for l in diff.splitlines()),
                            'removed_lines':sum(l.startswith('-') and not l.startswith('---') for l in diff.splitlines())}})
    body={'status':turn['status'],'request':clean('\n'.join(request)), 'summary':clean('\n'.join(summary)),
          'updates':[x for x in updates if x][-30:], 'operations':operations[-100:],
          'operations_total':len(operations),'completed_at':iso(turn['completed_at']) if turn['completed_at'] else None,
          'duration_ms':turn['duration_ms'], 'source':'codex-local-projection-v1',
          'coverage':'文件修改来自 fileChange 事件；脚本生成、外部工具修改及未保存记录可能没有文件级事件。命令结果不等同于完整测试覆盖。'}
    return body,artifacts

def ingest(progress=lambda *a:None):
    root=source_root()
    if not root or not (root/'state_5.sqlite').is_file(): return {'status':'unavailable','detail':'本节点未配置可读取的 Codex 本地记录；可接收来源节点同步的开发记录。'}
    if not (root/'thread_history_1.sqlite').is_file(): raise ValueError('Codex 会话投影数据库不存在，无法读取开发记录')
    discover_projects()
    from .node import identity
    node=identity();count=0;artifacts_count=0
    with db.connect() as c:
        projects=[dict(r) for r in c.execute('SELECT * FROM projects')]
        known_files={(r[0],r[1]) for r in c.execute('SELECT project_id,relpath FROM assets')}
        known={r['id']:json.loads(r['body']).get('source_revision') for r in c.execute('SELECT id,body FROM development_sessions')}
    with readonly(root/'state_5.sqlite') as state, readonly(root/'thread_history_1.sqlite') as history:
        for table,columns,con in [('threads',{'id','cwd','title','updated_at','git_sha'},state),('thread_items',{'item_type','item_json','turn_id','item_id'},history)]:
            found={r['name'] for r in con.execute('PRAGMA table_info('+table+')')}
            if not columns<=found:raise ValueError('Codex 本地数据格式已变化：'+table+'；请升级适配器')
        columns={r['name'] for r in state.execute('PRAGMA table_info(threads)')}
        extra=',rollout_path' if 'rollout_path' in columns else ''
        sessions=[dict(r) for r in state.execute('SELECT id,cwd,title,updated_at,git_sha,git_branch,archived'+extra+' FROM threads')]
        for s in sessions:
            p=match_project(s['cwd'],projects)
            if not p:continue
            sid=node['id']+':'+s['id']
            # Active turns can change without a new thread updated_at second.
            marker=history.execute('SELECT MAX(updated_at_ordinal),COUNT(*) FROM thread_items WHERE thread_id=?',(s['id'],)).fetchone()
            revision=f"v3:{s['updated_at']}:{marker[0]}:{marker[1]}"
            rollout=Path(s.get('rollout_path') or '/nonexistent')
            if marker[1]==0 and rollout.is_file():revision+=f":{rollout.stat().st_size}:{rollout.stat().st_mtime_ns}"
            if known.get(sid)==revision:continue
            turns=[dict(r) for r in history.execute('SELECT * FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal',(s['id'],))]
            fallback=rollout_turns(rollout,root,s['id']) if not turns and s.get('rollout_path') else []
            if fallback:turns=[t for t,_ in fallback]
            fallback_items={t['turn_id']:items for t,items in fallback}
            sb={'thread_id':s['id'],'source_node':node,'cwd':s['cwd'],'branch':s['git_branch'],'baseline_commit':s['git_sha'],
                'archived':bool(s['archived']),'source_revision':revision,'adapter':'codex-local-projection-v1'}
            with db.connect() as c:
                c.execute('INSERT OR REPLACE INTO development_sessions VALUES(?,?,?,?,?)',(sid,p['id'],clean(s['title'],300),json.dumps(sb,ensure_ascii=False),iso(s['updated_at'])))
                for t in turns:
                    tid=node['id']+':'+s['id']+':'+t['turn_id']
                    items=history.execute('SELECT item_id,item_json FROM thread_items WHERE thread_id=? AND turn_id=? AND item_type IN (?,?,?,?) ORDER BY rollout_ordinal',(s['id'],t['turn_id'],*TYPES))
                    if t['turn_id'] in fallback_items:items=fallback_items[t['turn_id']]
                    body,artifacts=parse_turn(t,items,s,projects,known_files)
                    if fallback:
                        body['source']='codex-rollout-public-v1';body['rollout_skipped_lines']=t.get('rollout_skipped_lines',0)
                    c.execute('INSERT OR REPLACE INTO development_turns VALUES(?,?,?,?,?)',(tid,sid,p['id'],iso(t['started_at']),json.dumps(body,ensure_ascii=False)))
                    c.execute('DELETE FROM development_artifacts WHERE turn_id=?',(tid,))
                    for a in artifacts:
                        aid=hashlib.sha256((tid+a['body']['evidence']+a['project_id']+a['path']).encode()).hexdigest()
                        c.execute('INSERT OR REPLACE INTO development_artifacts VALUES(?,?,?,?,?,?)',(aid,tid,a['project_id'],a['path'],a['action'],json.dumps(a['body'],ensure_ascii=False)))
                    artifacts_count+=len(artifacts)
                count+=1
            progress(count,len(sessions),'读取开发记录 · '+p['name'])
    with db.connect() as c:
        status={'status':'ready','sessions_updated':count,'artifact_events_updated':artifacts_count,'checked_at':core.now(),'adapter':'codex-local-projection-v1'}
        c.execute("INSERT OR REPLACE INTO settings VALUES('provenance_status',?)",(json.dumps(status),))
        if count:db.audit(c,'development_index',status)
    return status

def listing(pid=None,q='',page=1,limit=30,path=''):
    if page<1 or not 1<=limit<=100:raise ValueError('分页参数无效')
    where=[];args=[]
    if pid:where.append('(t.project_id=? OR EXISTS(SELECT 1 FROM development_artifacts a WHERE a.turn_id=t.id AND a.project_id=?))');args.extend([pid,pid])
    if path:where.append('EXISTS(SELECT 1 FROM development_artifacts a WHERE a.turn_id=t.id AND a.path=? AND a.project_id=?)');args.extend([path,pid])
    if q:where.append('(s.title LIKE ? OR t.body LIKE ?)');args.extend(['%'+q+'%']*2)
    sql=' FROM development_turns t JOIN development_sessions s ON s.id=t.session_id'+(' WHERE '+' AND '.join(where) if where else '')
    with db.connect() as c:
        total=c.execute('SELECT COUNT(*)'+sql,args).fetchone()[0]
        rows=[dict(r) for r in c.execute('SELECT t.*,s.title,s.body session_body'+sql+' ORDER BY t.started_at DESC LIMIT ? OFFSET ?',args+[limit,(page-1)*limit])]
        for r in rows:
            r['body']=json.loads(r['body']);r['session']=json.loads(r.pop('session_body'))
            r['artifacts']=[{**dict(a),'body':json.loads(a['body'])} for a in c.execute('SELECT * FROM development_artifacts WHERE turn_id=? ORDER BY path',(r['id'],))]
    return {'items':rows,'total':total,'page':page,'coverage':'按本地保存的会话与文件变更事件整理；时间先后表示迭代顺序，不自动认定为设计依赖。'}

def export_project(pid):
    with db.connect() as c:
        turns=[dict(r) for r in c.execute('SELECT * FROM development_turns WHERE project_id=? OR id IN (SELECT turn_id FROM development_artifacts WHERE project_id=?)',(pid,pid))]
        ids={t['session_id'] for t in turns}; tids={t['id'] for t in turns}
        return {'development_sessions':[dict(r) for r in c.execute('SELECT * FROM development_sessions') if r['id'] in ids or r['project_id']==pid],
                'development_turns':turns,'development_artifacts':[dict(r) for r in c.execute('SELECT * FROM development_artifacts') if r['turn_id'] in tids]}

def discover_projects():
    """Index all configured local Codex project roots; never create repositories."""
    root=source_root()
    if not root or not (root/'state_5.sqlite').is_file():return 0
    with readonly(root/'state_5.sqlite') as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'projects','project_roots'}<=tables:return 0
        rows=[dict(r) for r in c.execute('SELECT p.id,p.name,r.path,r.position FROM projects p JOIN project_roots r ON r.project_id=p.id')]
    with db.connect() as c:existing={r[0] for r in c.execute('SELECT path FROM projects')}
    found=[]
    for r in rows:
        path=Path(r['path']).expanduser().resolve()
        if str(path) in existing or not path.is_dir():continue
        found.append({'projectId':'codex-'+r['id']+'-'+str(r['position']),'label':r['name'],'path':str(path)})
        existing.add(str(path))
    if found:core.import_projects({'projects':found})
    return len(found)

def receive_project(data):
    """Publish development metadata independently of large engineering contents."""
    pid=data['project_id'];payload=data['development']
    schema={'development_sessions':{'id','project_id','title','body','updated_at'},
            'development_turns':{'id','session_id','project_id','started_at','body'},
            'development_artifacts':{'id','turn_id','project_id','path','action','body'}}
    with db.connect() as c:
        if not c.execute('SELECT 1 FROM projects WHERE id=?',(pid,)).fetchone():raise ValueError('请先同步工程，再同步开发记录')
        for table,columns in schema.items():
            for row in payload.get(table,[]):
                if set(row)!=columns:raise ValueError('开发记录字段无效')
                if not isinstance(json.loads(row['body']),dict):raise ValueError('开发记录内容无效')
                keys=list(row)
                c.execute('INSERT OR REPLACE INTO '+table+'('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',[row[k] for k in keys])
        state={'status':'synced','checked_at':core.now(),'source_node':data.get('source_node',{}).get('label','来源节点')}
        c.execute("INSERT OR REPLACE INTO settings VALUES('provenance_status',?)",(json.dumps(state),))
        db.audit(c,'development_sync_received',{'project_id':pid,'turns':len(payload.get('development_turns',[]))})
    return {'project_id':pid,'turns':len(payload.get('development_turns',[]))}

def sync(progress=lambda *a:None,pid=None):
    from . import node,project_sync
    cfg=project_sync.config();ingest(progress)
    with db.connect() as c:
        projects=[r[0] for r in c.execute("SELECT p.id FROM projects p JOIN project_locations l ON l.project_id=p.id WHERE l.mode='workspace'"+(' AND p.id=?' if pid else ''),(pid,) if pid else ())]
    results=[]
    for i,project in enumerate(projects):
        progress(i,len(projects),'同步开发记录')
        results.append(project_sync.remote_json(cfg,'import-development',payload={'project_id':project,'source_node':node.identity(),'development':export_project(project)}))
    progress(len(projects),len(projects),'开发记录同步完成')
    return {'projects':results}

def rollout_turns(path, root, thread_id):
    """Fallback for persisted public events not present in the SQLite projection."""
    path=Path(path).expanduser().resolve()
    if not path.is_relative_to(root) or path.suffix!='.jsonl' or not path.is_file():return []
    grouped={};current='initial';skipped=0
    def stamp(value):
        try:return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
        except (TypeError,ValueError,AttributeError):return 0
    def get(key,ts):
        if key not in grouped:grouped[key]=({'turn_id':key,'status':'in_progress','started_at':stamp(ts),'completed_at':None,'duration_ms':None},[])
        return grouped[key]
    with path.open('rb') as f:
        ordinal=0
        while True:
            line=f.readline(4*1024*1024+1)
            if not line:break
            ordinal+=1
            if len(line)>4*1024*1024:
                while line and not line.endswith(b'\n'):line=f.readline(4*1024*1024)
                skipped+=1;continue
            try:record=json.loads(line)
            except (ValueError,UnicodeDecodeError):skipped+=1;continue
            if record.get('type')!='event_msg':continue
            payload=record.get('payload',{});kind=payload.get('type');ts=record.get('timestamp')
            if payload.get('thread_id') and payload['thread_id']!=thread_id:continue
            if kind=='task_started':current=payload.get('turn_id') or str(ordinal);get(current,ts)
            elif kind in {'task_complete','task_completed','turn_aborted'}:
                turn,_=get(payload.get('turn_id') or current,ts);turn['status']='interrupted' if kind=='turn_aborted' else 'completed';turn['completed_at']=stamp(ts)
            elif kind=='item_completed':
                item=payload.get('item',{});item_type=item.get('type')
                if item_type not in TYPES:continue
                # Drop opaque tool output and all unsupported items before retaining an event.
                public={k:item.get(k) for k in ['type','id','content','text','phase','changes','status','command','exitCode','durationMs'] if k in item}
                if 'text' in public:public['text']=clean(public['text'])
                if 'command' in public:public['command']=clean(public['command'],600)
                if 'changes' in public:public['changes']=[{k:clean(v) if k=='diff' else v for k,v in change.items() if k in {'path','kind','diff'}} for change in public['changes']]
                turn,items=get(payload.get('turn_id') or current,ts)
                items.append({'item_id':item.get('id') or 'rollout-'+str(ordinal),'item_json':json.dumps(public,ensure_ascii=False)})
    result=[]
    for turn,items in grouped.values():
        if items:turn['rollout_skipped_lines']=skipped;result.append((turn,items))
    return result
