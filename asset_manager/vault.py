"""Encrypted recovery snapshots. Restic owns encryption, deduplication and file metadata.

The web catalog contains receipts, never recovery passwords or raw session bodies.
Restores always target a fresh directory owned by Atlas; no in-place overwrite API.
"""
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from . import core, db, node, provenance

SCHEMA = '''
CREATE TABLE IF NOT EXISTS vault_profiles(id TEXT PRIMARY KEY,node_id TEXT NOT NULL,name TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vault_snapshots(id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,node_id TEXT NOT NULL,status TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vault_restores(id TEXT PRIMARY KEY,snapshot_id TEXT NOT NULL,status TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS knowledge_items(id TEXT PRIMARY KEY,node_id TEXT NOT NULL,kind TEXT NOT NULL,name TEXT NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,body TEXT NOT NULL,updated_at TEXT NOT NULL,present INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS knowledge_versions(item_id TEXT NOT NULL,sha256 TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(item_id,sha256));
'''

def init():
    with db.connect() as c:c.executescript(SCHEMA)

def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.chmod(0o600);tmp.replace(path)

def binary():
    p=os.environ.get('ATLAS_RESTIC') or shutil.which('restic') or str(db.DATA.parent/'tools/restic')
    if not Path(p).is_file():raise ValueError('尚未安装 Restic；运行 scripts/install-restic.py 或设置 ATLAS_RESTIC')
    return str(Path(p).resolve())

def configure(name, repository, roots=(), codex_root='', include_projects=True, include_catalog=True, password_file=None):
    """CLI-only setup: secrets never pass through the HTTP API."""
    init()
    if repository.startswith('sftp:'):
        if not re.fullmatch(r'sftp:[A-Za-z0-9_.@-]+:/[^\s\x00-\x1f]+',repository):raise ValueError('SFTP 地址格式应为 sftp:user@host:/absolute/path')
        if '..' in Path(repository.split(':',2)[2]).parts:raise ValueError('仓库路径无效')
    elif not Path(repository).is_absolute():raise ValueError('仓库必须为绝对路径或 SFTP 地址')
    roots=[str(Path(p).expanduser().resolve()) for p in roots]
    for root in roots:
        if not Path(root).exists() or root=='/':raise ValueError('备份根目录不存在或范围过大：'+root)
    if codex_root:
        codex_root=str(Path(codex_root).expanduser().resolve())
        if not Path(codex_root).is_dir():raise ValueError('Codex 来源目录不存在')
    ident=uuid.uuid4().hex
    if password_file:
        key=Path(password_file).expanduser().resolve()
        if not key.is_file() or key.stat().st_mode&0o077:raise ValueError('恢复密钥必须是仅当前用户可读的文件（0600）')
    else:
        key=db.DATA/'private'/('vault-'+ident+'.key');key.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        fd=os.open(key,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(secrets.token_urlsafe(48)+'\n')
    body={'repository':repository,'password_file':str(key),'roots':roots,'codex_root':codex_root,
          'include_projects':bool(include_projects),'include_catalog':bool(include_catalog),'engine':'restic','policy':'manual','read_only':False}
    with db.connect() as c:
        c.execute('INSERT INTO vault_profiles VALUES(?,?,?,?,?)',(ident,node.identity()['id'],name[:120],json.dumps(body),core.now()))
        db.audit(c,'vault_configured',{'profile_id':ident,'name':name})
    return {'id':ident,'recovery_key_file':str(key),'note':'请另存离线恢复密钥；它不会同步到目标节点或包含在备份中。'}

def profile(pid, local=True):
    init()
    with db.connect() as c:r=c.execute('SELECT * FROM vault_profiles WHERE id=?',(pid,)).fetchone()
    if not r:raise ValueError('备份配置不存在')
    r=dict(r);r['body']=json.loads(r['body'])
    if local and (r['node_id']!=node.identity()['id'] or r['body'].get('read_only')):raise ValueError('此备份由来源节点管理，请在来源节点操作')
    return r

def _env(p):
    b=p['body'];key=Path(b['password_file'])
    if not key.is_file() or key.stat().st_mode&0o077:raise ValueError('恢复密钥缺失或权限不安全')
    env={k:v for k,v in os.environ.items() if not k.startswith('RESTIC_')}
    env.update(RESTIC_REPOSITORY=b['repository'],RESTIC_PASSWORD_FILE=str(key),RESTIC_CACHE_DIR=str(db.DATA/'vault-cache'),GOMAXPROCS='2')
    return env

def command(p, *args):
    result=[binary(),'--json']
    if p['body']['repository'].startswith('sftp:'):
        result+=['-o','sftp.args=-oBatchMode=yes -oStrictHostKeyChecking=yes -oConnectTimeout=15 -oServerAliveInterval=15 -oServerAliveCountMax=4']
    return result+list(args)

def run(p, args, callback=None, timeout=None):
    proc=subprocess.Popen(command(p,*args),env=_env(p),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
    errors=[];events=[]
    def drain():
        for line in proc.stderr:
            errors.append(provenance.clean(line,1200))
            if len(errors)>60:errors.pop(0)
    reader=threading.Thread(target=drain,daemon=True);reader.start()
    try:
        for line in proc.stdout:
            try:event=json.loads(line)
            except ValueError:event={'message_type':'text','message':provenance.clean(line,1200)}
            if callback:callback(event)
            if isinstance(event,list) or (isinstance(event,dict) and event.get('message_type') not in {'status','verbose_status'}):events.append(event)
            if len(events)>100:events.pop(0)
        rc=proc.wait(timeout=timeout);reader.join(timeout=5)
        return rc,events,errors
    finally:
        if proc.poll() is None:
            import signal
            os.killpg(proc.pid,signal.SIGTERM)
            try:proc.wait(timeout=10)
            except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()

def initialize(pid):
    p=profile(pid);rc,events,errors=run(p,['cat','config'])
    if rc==0:return {'initialized':True,'existing':True}
    # Never interpret a bad password or a network error as an empty repository.
    message=' '.join(errors)+json.dumps(events)
    if not any(x in message.lower() for x in ['does not exist','no such file','is there a repository','unable to open config']):raise ValueError('无法打开仓库：'+message[-1500:])
    rc,events,errors=run(p,['init','--repository-version','2'])
    if rc:raise ValueError('初始化失败：'+' '.join(errors)[-1500:])
    return {'initialized':True,'existing':False}

@contextlib.contextmanager
def lock(pid):
    import fcntl
    folder=db.DATA/'vault-locks';folder.mkdir(exist_ok=True)
    with (folder/(pid+'.lock')).open('a') as f:
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('该备份正在运行，请等待完成')
        try:yield
        finally:fcntl.flock(f,fcntl.LOCK_UN)

def plan(pid):
    p=profile(pid);b=p['body'];roots=list(b['roots']);missing=[];worktrees=[]
    if b['codex_root']:roots.append(b['codex_root'])
    if b['include_projects']:
        with db.connect() as c:
            projects=[dict(r) for r in c.execute("SELECT p.* FROM projects p LEFT JOIN project_locations l ON l.project_id=p.id WHERE l.mode IS NULL OR l.mode='workspace'")]
        roots += [r['path'] for r in projects]
    # Include every managed worktree's shared Git directory, including unregistered chats.
    codex=Path(b['codex_root']) if b['codex_root'] else None
    checkouts=list(roots)
    if codex and (codex/'worktrees').is_dir():checkouts += [str(x.parent) for x in (codex/'worktrees').glob('*/*/.git')]
    for path in sorted(set(checkouts)):
        if not Path(path).is_dir():continue
        common=core.git(path,'rev-parse','--path-format=absolute','--git-common-dir')
        if common:
            roots.append(common);worktrees.append({'path':path,'common_dir':common,'head':core.git(path,'rev-parse','HEAD'),'branch':core.git(path,'symbolic-ref','--quiet','--short','HEAD')})
    if b['include_catalog']:roots += [str(db.DATA),str(db.blob_root())]
    paths=[]
    for name in sorted(set(roots),key=lambda x:len(Path(x).parts)):
        path=Path(name).expanduser().resolve()
        if not path.exists():missing.append(str(path));continue
        if not any(path.is_relative_to(x) for x in paths):paths.append(path)
    if not paths:raise ValueError('没有可备份的来源')
    repository=b['repository']
    if not repository.startswith('sftp:') and any(Path(repository).resolve().is_relative_to(x) for x in paths):raise ValueError('备份仓库不能位于来源目录内')
    excluded=[str(db.DATA/x) for x in ['private','vault-cache','vault-restores','tmp','vault-locks']]+[b['password_file']]
    stage_root=db.DATA/'vault-stage'
    if stage_root.exists():excluded += [str(x) for x in stage_root.iterdir() if x.name!=pid]
    if codex:
        excluded += [str(codex/x) for x in ['tmp','.tmp','ipc','process_manager','thread-writer-locks','project-metadata-locks','app-server-control']]
    return {'profile_id':pid,'node':node.identity(),'roots':[str(x) for x in paths],'missing':missing,'excluded':excluded,
            'worktrees':worktrees,'encrypted':True,'consistency':'已识别 SQLite 单库一致性副本；其余文件为在线遍历，非跨目录原子快照',
            'coverage':'保存所选根目录的原始文件，包括 Git 和未跟踪文件；不跟随符号链接。排除运行时目录、Atlas 恢复密钥及其他任务的暂存目录。数据库一致性副本与映射清单单独保存。'}

def literal(value):
    return ''.join('['+c+']' if c in '*?[' else c for c in str(value))

def database_sources(p):
    paths={db.DATA/'catalog.sqlite3'} if p['body']['include_catalog'] else set()
    root=Path(p['body']['codex_root']) if p['body']['codex_root'] else None
    if root and root.is_dir():
        for base,dirs,files in os.walk(root,followlinks=False):
            dirs[:]=[d for d in dirs if d not in {'worktrees','plugins','cache','tmp','.tmp','node_modules','attachments','sessions','archived_sessions','visualizations','browser'} and not (Path(base)/d).is_symlink()]
            for name in files:
                file=Path(base)/name
                if file.suffix in {'.sqlite','.sqlite3','.db'} and not file.is_symlink():
                    with file.open('rb') as f:
                        if f.read(16)==b'SQLite format 3\0':paths.add(file)
    return sorted(paths)

def stage(p, report, folder):
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    copies=[];excludes=[];samples=[]
    for src in database_sources(p):
        dst=folder/'databases'/(hashlib.sha256(str(src).encode()).hexdigest()[:16]+'-'+src.name);dst.parent.mkdir(parents=True,exist_ok=True)
        if shutil.disk_usage(folder).free < src.stat().st_size+256*1024**2:raise ValueError('本机空间不足，无法创建数据库一致性副本')
        started=time.monotonic()
        def progress(status, remaining, total):
            if time.monotonic()-started>180:raise ValueError('数据库持续繁忙，未能在 180 秒内完成一致性备份：'+str(src))
        with sqlite3.connect(src.as_uri()+'?mode=ro',uri=True,timeout=10) as origin,sqlite3.connect(dst) as target:
            origin.backup(target,pages=2048,progress=progress,sleep=.05)
            target.execute('PRAGMA journal_mode=DELETE')
            if target.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('数据库校验失败：'+str(src))
        dst.chmod(0o600)
        with dst.open('rb') as f:sha=hashlib.file_digest(f,'sha256').hexdigest()
        copies.append({'source':str(src),'snapshot_path':str(dst),'sha256':sha,'bytes':dst.stat().st_size})
        excludes += [str(src),str(src)+'-wal',str(src)+'-shm',str(src)+'-journal']
    # Small reproducible samples supplement restic's pack verification.
    candidates=[]
    codex=Path(p['body']['codex_root']) if p['body']['codex_root'] else None
    if codex:
        candidates += sorted((codex/'skills').glob('*/SKILL.md'))[:2]
        candidates += sorted((codex/'archived_sessions').glob('**/*.jsonl'))[:1]
    for root in report['roots']:
        candidates += [Path(root)/'README.md']
    small_db=next((x for x in copies if x['bytes']<20*1024**2),None)
    if small_db:candidates.append(Path(small_db['snapshot_path']))
    for path in candidates:
        if path.is_file() and not path.is_symlink() and path.stat().st_size<20*1024**2:
            before=path.stat()
            with path.open('rb') as f:sha=hashlib.file_digest(f,'sha256').hexdigest()
            if path.stat().st_mtime_ns==before.st_mtime_ns:samples.append({'path':str(path),'sha256':sha,'bytes':before.st_size})
    report.update(databases=copies,restore_samples=samples,excluded=report['excluded']+excludes)
    write_json(folder/'manifest.json',report)
    return excludes

def save_snapshot(sid,p,status,body,created):
    with db.connect() as c:
        c.execute('INSERT OR REPLACE INTO vault_snapshots VALUES(?,?,?,?,?,?)',(sid,p['id'],p['node_id'],status,json.dumps(body,ensure_ascii=False),created))

def backup(progress,pid):
    p=profile(pid);created=core.now();sid=uuid.uuid4().hex;body={'phase':'preparing','errors':[],'started_at':created}
    with lock(pid):
        save_snapshot(sid,p,'running',body,created)
        folder=db.DATA/'vault-stage'/pid/'current'
        try:
            report=plan(pid)
            if report['missing']:raise ValueError('来源目录缺失：'+', '.join(report['missing']))
            # Stable stage location lets restic deduplicate repeated database snapshots.
            if folder.exists():shutil.rmtree(folder)
            stage(p,report,folder)
            body.update(report,phase='backup',stage_path=str(folder));save_snapshot(sid,p,'running',body,created)
            excludes=folder/'exclude.txt';excludes.write_text('\n'.join(literal(x) for x in report['excluded'])+'\n')
            args=['backup','--host',p['node_id'],'--tag','atlas','--tag','profile:'+pid,'--tag','receipt:'+sid,'--exclude-file',str(excludes)]
            # Database copies are separate sources when the catalog itself is not selected.
            args += ['--']+report['roots']+[str(folder)]
            last=[0]
            def event(e):
                if not isinstance(e,dict):return
                if e.get('message_type')=='status' and time.monotonic()-last[0]>3:
                    last[0]=time.monotonic();body['progress']={k:e[k] for k in ['percent_done','total_files','files_done','total_bytes','bytes_done'] if k in e}
                    save_snapshot(sid,p,'running',body,created)
                    progress(e.get('bytes_done',0),e.get('total_bytes',0),'加密备份 · '+str(e.get('files_done',0))+' 个文件')
                if e.get('message_type')=='error' and len(body['errors'])<100:body['errors'].append(provenance.clean(json.dumps(e,ensure_ascii=False),2000))
            rc,events,errors=run(p,args,event)
            summary=next((e for e in reversed(events) if isinstance(e,dict) and e.get('message_type')=='summary'),{})
            body.update(summary=summary,errors=body['errors']+errors,finished_at=core.now(),restic_snapshot=summary.get('snapshot_id'),phase='saved')
            status='complete' if rc==0 and summary.get('snapshot_id') else 'partial' if summary.get('snapshot_id') else 'failed'
            body['exit_code']=rc;body['verification']={'status':'pending'}
            save_snapshot(sid,p,status,body,created)
            with db.connect() as c:db.audit(c,'vault_backup',{'id':sid,'status':status})
            if status!='complete':raise ValueError('备份未完整完成，请查看备份中心的错误和覆盖清单；已上传对象可在重试时复用')
            return {'id':sid,'status':status,'snapshot':summary['snapshot_id'],'files':summary.get('total_files_processed'),'bytes':summary.get('total_bytes_processed')}
        except Exception as e:
            if body.get('phase')!='saved':
                body.update(phase='failed',finished_at=core.now(),errors=body.get('errors',[])+[str(e)])
                save_snapshot(sid,p,'failed',body,created)
            raise
        finally:
            if folder.exists():shutil.rmtree(folder)

def snapshot(sid):
    with db.connect() as c:r=c.execute('SELECT * FROM vault_snapshots WHERE id=?',(sid,)).fetchone()
    if not r:raise ValueError('恢复点不存在')
    r=dict(r);r['body']=json.loads(r['body']);return r

def verify(progress,sid,full=False):
    s=snapshot(sid);p=profile(s['profile_id'])
    if not s['body'].get('restic_snapshot'):raise ValueError('此记录没有可校验快照')
    with lock(p['id']):
        progress(0,1,'检查加密仓库和内容对象')
        rc,events,errors=run(p,['check','--read-data'] if full else ['check','--read-data-subset=5%'])
        result={'status':'passed' if rc==0 else 'failed','scope':'all_repository_data' if full else 'repository_structure_and_5_percent_data','checked_at':core.now(),'errors':errors}
        s['body']['verification']=result;save_snapshot(sid,p,s['status'],s['body'],s['created_at'])
        with db.connect() as c:db.audit(c,'vault_verified',{'id':sid,**result})
        if rc:raise ValueError('仓库校验失败，请查看记录')
        return result

def restore_plan(sid,paths):
    s=snapshot(sid);p=profile(s['profile_id']);b=s['body']
    if not b.get('restic_snapshot') or s['status'] not in {'complete','partial'}:raise ValueError('恢复点不可用')
    if not isinstance(paths,list) or not 1<=len(paths)<=30:raise ValueError('请选择 1–30 个路径')
    roots=[Path(x) for x in b.get('roots',[])]+[Path(b.get('stage_path','/nonexistent'))]
    normalized=[];mappings=[];databases={x['source']:x for x in b.get('databases',[])}
    for path in paths:
        path=Path(str(path))
        if not path.is_absolute() or '..' in path.parts or not any(path.is_relative_to(r) for r in roots):raise ValueError('恢复路径超出快照范围')
        if str(path) in databases:
            item=databases[str(path)];normalized.append(item['snapshot_path']);mappings.append(item)
        else:
            normalized.append(str(path))
            for source,item in databases.items():
                if Path(source).is_relative_to(path):normalized.append(item['snapshot_path']);mappings.append(item)
    normalized=list(dict.fromkeys(normalized))
    mappings=list({x['source']:x for x in mappings}.values())
    ident=uuid.uuid4().hex;target=db.DATA/'vault-restores'/ident
    body={'paths':normalized,'requested_paths':paths,'database_mappings':mappings,'target':str(target),'profile_id':p['id'],'note':'只恢复到全新隔离目录，不覆盖原文件；Git 绝对路径和应用状态需独立修复验证。'}
    with db.connect() as c:c.execute('INSERT INTO vault_restores VALUES(?,?,?,?,?)',(ident,sid,'planned',json.dumps(body),core.now()))
    return {'id':ident,**body}

def restore(progress,rid):
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT * FROM vault_restores WHERE id=?',(rid,)).fetchone()
        if not r or r['status']!='planned':raise ValueError('恢复方案不存在或已执行')
        c.execute("UPDATE vault_restores SET status='running' WHERE id=?",(rid,))
    body=json.loads(r['body']);s=snapshot(r['snapshot_id']);p=profile(body['profile_id']);target=Path(body['target'])
    try:
        with lock(p['id']):
            if target.parent.resolve()!=(db.DATA/'vault-restores').resolve() or target.exists():raise ValueError('恢复目录已存在或不安全')
            target.mkdir(parents=True,mode=0o700)
            args=['restore',s['body']['restic_snapshot'],'--target',str(target),'--verify']
            for path in body['paths']:args+=['--include',literal(path)]
            progress(0,1,'恢复到隔离目录并校验内容')
            rc,events,errors=run(p,args)
            body.update(errors=errors,finished_at=core.now())
            status='complete' if rc==0 else 'failed'
            if rc:raise ValueError('恢复失败：'+' '.join(errors)[-1000:])
            # Ensure an incorrect include pattern cannot be reported as a useful restore.
            if not all((target/x.lstrip('/')).exists() or (target/x.lstrip('/')).is_symlink() for x in body['paths']):raise ValueError('部分选定路径不存在于恢复点，未完成全部选定内容')
            body['verified']=True
    except Exception as e:body['error']=str(e);status='failed'
    with db.connect() as c:
        c.execute('UPDATE vault_restores SET status=?,body=? WHERE id=?',(status,json.dumps(body),rid))
        db.audit(c,'vault_restore',{'id':rid,'status':status})
    if status=='failed':raise ValueError(body.get('error','恢复失败'))
    return {'id':rid,'status':status,**body}

def drill(progress,sid):
    s=snapshot(sid);samples=s['body'].get('restore_samples',[])
    if not samples:raise ValueError('该恢复点没有小型恢复样本，请手动选择恢复路径')
    evidence={'status':'failed','checked_at':core.now(),'checks':[]}
    try:
        result=restore(progress,restore_plan(sid,[x['path'] for x in samples])['id'])
        evidence.update(target=result['target'],restore_id=result['id'])
        for x in samples:
            path=Path(result['target'])/x['path'].lstrip('/')
            with path.open('rb') as f:sha=hashlib.file_digest(f,'sha256').hexdigest()
            check={'path':x['path'],'sha256_matches':sha==x['sha256']}
            if path.suffix in {'.sqlite','.sqlite3','.db'}:
                with sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True) as c:check['sqlite_quick_check']=c.execute('PRAGMA quick_check').fetchone()[0]
            evidence['checks'].append(check)
        passed=all(x['sha256_matches'] and x.get('sqlite_quick_check','ok')=='ok' for x in evidence['checks'])
        if not passed:raise ValueError('恢复样本与备份前内容不一致；来源可能在在线备份期间变化')
        evidence['status']='passed'
    except Exception as e:evidence['error']=str(e)
    s['body']['restore_drill']=evidence
    save_snapshot(sid,profile(s['profile_id']),s['status'],s['body'],s['created_at'])
    if evidence['status']!='passed':raise ValueError(evidence['error'])
    return evidence

def recover_interrupted():
    # The profile lock survives web restarts when a CLI backup is still running.
    origin=node.identity()['id']
    with db.connect() as c:rows=c.execute("SELECT * FROM vault_snapshots WHERE node_id=? AND status='running'",(origin,)).fetchall()
    for r in rows:
        try:
            with lock(r['profile_id']):
                body=json.loads(r['body']);body.update(phase='interrupted',finished_at=core.now())
                body['errors']=body.get('errors',[])+['备份进程已结束，未发布完整恢复点；重试会复用已经上传的加密对象。']
                with db.connect() as c:c.execute("UPDATE vault_snapshots SET status='interrupted',body=? WHERE id=? AND status='running'",(json.dumps(body,ensure_ascii=False),r['id']))
        except ValueError:pass

def overview():
    init();recover_interrupted();local=node.identity()['id']
    with db.connect() as c:
        profiles=[dict(r) for r in c.execute('SELECT * FROM vault_profiles ORDER BY created_at DESC')]
        snapshots=[dict(r) for r in c.execute('SELECT * FROM vault_snapshots ORDER BY created_at DESC LIMIT 100')]
        restores=[dict(r) for r in c.execute('SELECT * FROM vault_restores ORDER BY created_at DESC LIMIT 50')]
    for r in profiles:
        b=json.loads(r['body']);b.pop('password_file',None);r['body']=b;r['local']=r['node_id']==local and not b.get('read_only')
    for r in snapshots+restores:r['body']=json.loads(r['body'])
    return {'profiles':profiles,'snapshots':snapshots,'restores':restores,'node':node.identity(),
            'note':'备份文件端到端加密；目标节点只接收可浏览的清单。恢复密钥留在来源节点，需另存离线副本。'}

def export_metadata():
    data=overview();data['schema']=1
    for p in data['profiles']:p['body']['read_only']=True
    data['restores']=[]
    from . import knowledge
    data['knowledge']=knowledge.export()
    return data

def receive_metadata(data):
    init();origin=data['node']['id']
    if origin==node.identity()['id'] or not re.fullmatch('[a-f0-9]{32}',origin):raise ValueError('来源节点身份无效')
    if data.get('schema')!=1:raise ValueError('备份清单版本不支持')
    with db.connect() as c:
        for p in data.get('profiles',[]):
            if p['node_id']!=origin:continue
            old=c.execute('SELECT node_id FROM vault_profiles WHERE id=?',(p['id'],)).fetchone()
            if old and old[0]!=origin:raise ValueError('备份配置身份冲突')
            b=p['body'];b.pop('password_file',None);b['read_only']=True
            c.execute('INSERT OR REPLACE INTO vault_profiles VALUES(?,?,?,?,?)',(p['id'],origin,p['name'],json.dumps(b),p['created_at']))
        for s in data.get('snapshots',[]):
            if s['node_id']!=origin:continue
            owner=c.execute('SELECT node_id FROM vault_profiles WHERE id=?',(s['profile_id'],)).fetchone()
            if not owner or owner[0]!=origin:raise ValueError('恢复点配置范围无效')
            old=c.execute('SELECT node_id FROM vault_snapshots WHERE id=?',(s['id'],)).fetchone()
            if old and old[0]!=origin:raise ValueError('恢复点身份冲突')
            c.execute('INSERT OR REPLACE INTO vault_snapshots VALUES(?,?,?,?,?,?)',(s['id'],s['profile_id'],origin,s['status'],json.dumps(s['body']),s['created_at']))
        from . import knowledge
        knowledge.receive(c,origin,data.get('knowledge',{}))
        db.audit(c,'vault_metadata_received',{'node_id':origin,'snapshots':len(data.get('snapshots',[]))})
    return {'received':True,'source_node':origin}

def sync_metadata(progress=lambda *a:None):
    from . import project_sync
    return project_sync.remote_json(project_sync.config(),'import-vault',payload=export_metadata())
