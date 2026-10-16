"""Node-local identity, explicit source locations and bounded reconciliation."""
import gzip
import hashlib
import json
import os
import platform
import socket
import threading
import uuid
from pathlib import Path
from . import core, db

STOP = threading.Event()
WORKER = None

def identity():
    with db.connect() as c:
        row=c.execute("SELECT value FROM settings WHERE key='node_id'").fetchone()
        if not row:
            c.execute("INSERT OR IGNORE INTO settings VALUES('node_id',?)",(uuid.uuid4().hex,))
            row=c.execute("SELECT value FROM settings WHERE key='node_id'").fetchone()
        ident=row[0]
        label=c.execute("SELECT value FROM settings WHERE key='node_label'").fetchone()
    return {'id':ident,'label':os.environ.get('ASSET_NODE_NAME') or (label[0] if label else socket.gethostname()),'platform':platform.system()}

def initialize():
    node=identity()
    with db.connect() as c:
        projects=[dict(r) for r in c.execute('SELECT p.* FROM projects p LEFT JOIN project_locations l ON l.project_id=p.id WHERE l.project_id IS NULL')]
    for p in projects:
        local=Path(p['path']).is_dir();mode='workspace' if local else 'archive'
        paths=None
        if not local:
            with db.connect() as c:r=c.execute('SELECT manifest FROM project_index_runs WHERE project_id=? ORDER BY created_at DESC LIMIT 1',(p['id'],)).fetchone()
            if r:
                try:
                    with gzip.open(db.DATA/'project-index'/Path(r[0]).name,'rt') as f:paths={x['path'] for x in json.load(f)['files']}
                except (OSError,ValueError):pass
        with db.connect() as c:
            c.execute('INSERT OR IGNORE INTO project_locations VALUES(?,?,?,?,?,?)',(p['id'],node['id'],mode,None,p['path'],core.now()))
            if not local and paths is not None:
                for r in c.execute("SELECT a.relpath,v.sha256,v.size FROM assets a JOIN versions v ON v.id=(SELECT MAX(id) FROM versions WHERE asset_id=a.id AND reason='snapshot') WHERE a.project_id=?",(p['id'],)).fetchall():
                    if r['relpath'] in paths:c.execute('INSERT OR IGNORE INTO mirror_files VALUES(?,?,?,?)',(p['id'],r['relpath'],r['sha256'],r['size']))

def location(pid):
    with db.connect() as c:
        row=c.execute('SELECT * FROM project_locations WHERE project_id=?',(pid,)).fetchone()
        p=c.execute('SELECT path FROM projects WHERE id=?',(pid,)).fetchone()
    if not p:raise ValueError('项目不存在')
    result=dict(row) if row else {'project_id':pid,'mode':'workspace','source_path':p['path']}
    result['local_available']=result['mode']=='workspace' and Path(p['path']).is_dir()
    result['storage_path']=p['path'] if result['mode']=='workspace' else str(db.blob_root())
    return result

def current_sql(alias='a'):
    return f"({alias}.present=1 OR {alias}.managed=1 OR EXISTS(SELECT 1 FROM mirror_files mf WHERE mf.project_id={alias}.project_id AND mf.path={alias}.relpath))"

def current_paths(pid):
    with db.connect() as c:return {r[0] for r in c.execute('SELECT a.relpath FROM assets a WHERE a.project_id=? AND '+current_sql(),(pid,))}

def revision():
    with db.connect() as c:
        row=c.execute('SELECT MAX(id) revision FROM audits').fetchone()
        status=c.execute("SELECT value FROM settings WHERE key='watch_status'").fetchone()
        development=c.execute("SELECT value FROM settings WHERE key='provenance_status'").fetchone()
    return {'revision':row[0] or 0,'node':identity(),'watch':json.loads(status[0]) if status else {'status':'disabled'},'development':json.loads(development[0]) if development else {'status':'pending'},'version':__import__('asset_manager').__version__}

def set_status(value):
    with db.connect() as c:c.execute("INSERT OR REPLACE INTO settings VALUES('watch_status',?)",(json.dumps(value,ensure_ascii=False),))

def filesystem_state(p):
    root=Path(p['path']);files={};directories=set();excluded=[db.DATA,db.blob_root(),core.LOCAL_BUILD_DATA]
    def onerror(error):raise error
    for directory,dirs,names in os.walk(root,followlinks=False,onerror=onerror):
        base=Path(directory);directories.add(base.relative_to(root).as_posix())
        dirs[:]=sorted(d for d in dirs if d not in core.EXCLUDE_DIRS and not (base/d).is_symlink() and not any((base/d).is_relative_to(x) for x in excluded))
        for name in names:
            path=base/name
            if name=='.git' or core.secret(path) or path.is_symlink():continue
            if not path.is_file():continue
            st=path.stat();files[path.relative_to(root).as_posix()]=(st.st_size,st.st_mtime)
    return files,directories

def reconcile(pid):
    from . import project_index
    p=project_index.project(pid)
    if not location(pid)['local_available']:return {'skipped':'nonlocal'}
    files,dirs=filesystem_state(p)
    with db.connect() as c:
        old={r['relpath']:(r['size'],r['mtime']) for r in c.execute('SELECT relpath,size,mtime FROM assets WHERE project_id=? AND present=1 AND managed=0',(pid,))}
        old_dirs={r[0] or '.' for r in c.execute('SELECT path FROM project_directories WHERE project_id=?',(pid,))}
        refs=[r[0] for r in c.execute('SELECT checkout_root FROM project_repositories WHERE project_id=?',(pid,))]
    git_state=[]
    for root in set(refs)|{p['path']}:
        index_path=core.git(root,'rev-parse','--git-path','index')
        index_file=Path(index_path.strip()) if index_path else None
        if index_file and not index_file.is_absolute():index_file=Path(root)/index_file
        index_stat=(index_file.stat().st_size,index_file.stat().st_mtime_ns) if index_file and index_file.is_file() else None
        git_state.append((root,core.git(root,'rev-parse','HEAD'),core.git(root,'for-each-ref','--format=%(objectname) %(refname)'),index_stat))
    key='watch_git:'+pid;fingerprint=hashlib.sha256(json.dumps(git_state).encode()).hexdigest()
    before=db.setting(key)
    changed=not db.setting('watch_initialized:'+pid) and not old or files!=old or dirs!=old_dirs or bool(db.setting('watch_dirty:'+pid)) or (before is not None and fingerprint!=before)
    if changed:
        with core.MUTATION_LOCK:project_index.index_one(pid)
    with db.connect() as c:
        c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('watch_initialized:'+pid,'1'))
        c.execute('DELETE FROM settings WHERE key=?',('watch_dirty:'+pid,))
        c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,fingerprint))
        c.execute('UPDATE project_locations SET refreshed_at=? WHERE project_id=?',(core.now(),pid))
    return {'project_id':pid,'changed':changed}

def watch_loop():
    from . import provenance
    import time
    cursor=0;last_history=0
    interval=max(5,int(os.environ.get('ASSET_WATCH_INTERVAL','10')))
    while not STOP.wait(interval):
        started=time.monotonic()
        try:
            initialize()
            with db.connect() as c:
                busy=c.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone()
                projects=[dict(r) for r in c.execute("SELECT p.* FROM projects p JOIN project_locations l ON l.project_id=p.id WHERE l.mode='workspace' ORDER BY p.id")]
            if busy:
                set_status({'status':'waiting','detail':'正在等待前台索引或同步任务结束','checked_at':core.now()});continue
            result={}
            if projects:
                p=projects[cursor%len(projects)];cursor+=1
                set_status({'status':'checking','project':p['name'],'checked_at':core.now()})
                result=reconcile(p['id'])
            if time.monotonic()-last_history>60:
                try:provenance.ingest()
                except Exception as e:
                    with db.connect() as c:c.execute("INSERT OR REPLACE INTO settings VALUES('provenance_status',?)",(json.dumps({'status':'error','detail':str(e),'checked_at':core.now()}),))
                last_history=time.monotonic()
            set_status({'status':'watching','checked_at':core.now(),'interval_seconds':interval,'projects_per_cycle':len(projects),'duration_seconds':round(time.monotonic()-started,2),**result})
        except Exception as e:set_status({'status':'error','detail':str(e),'checked_at':core.now()})

def start():
    global WORKER
    initialize()
    if os.environ.get('ASSET_WATCH_ENABLED','1')=='0':return
    STOP.clear();WORKER=threading.Thread(target=watch_loop,name='ams-reconcile',daemon=True);WORKER.start()

def stop():STOP.set()
