"""Incremental Source node -> archive sync; original repositories are never written or pushed."""
import base64
import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from . import core, db, git_history, project_index


def config():
    value=db.setting('sync_target')
    if not value:raise ValueError('未配置归档服务器')
    return validate_config(json.loads(value))

def validate_config(cfg):
    if not re.fullmatch(r'[A-Za-z0-9_.@:-]+',cfg['host']) or cfg['host'].startswith('-'):raise ValueError('SSH 目标无效')
    if not cfg['root'].startswith('/') or '..' in Path(cfg['root']).parts:raise ValueError('归档根目录无效')
    return cfg


def ssh_args(cfg,mode,*args):
    root=cfg['root'];python=root+'/app/.venv/bin/python'
    cmd='cd '+shlex.quote(root+'/app')+' && ASSET_DATA_DIR='+shlex.quote(root+'/catalog')+' ASSET_BLOB_DIR='+shlex.quote(root+'/archive')+' '+shlex.quote(python)+' -m asset_manager.project_sync '+shlex.quote(mode)
    cmd+=''.join(' '+shlex.quote(str(x)) for x in args)
    return ['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=4',cfg['host'],cmd]


def remote_json(cfg,mode,*args,payload=None):
    data=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None
    r=subprocess.run(ssh_args(cfg,mode,*args),input=data,capture_output=True,timeout=600)
    if r.returncode:raise ValueError('归档节点 同步失败：'+r.stderr.decode(errors='replace')[-1200:])
    return json.loads(r.stdout)


def remote_inventory(pid):
    with db.connect() as c:
        rows=c.execute("SELECT a.relpath,a.size,a.mtime,(SELECT sha256 FROM versions v WHERE v.asset_id=a.id AND reason='snapshot' ORDER BY id DESC LIMIT 1) sha FROM assets a WHERE project_id=?",(pid,)).fetchall()
        runs=[r[0] for r in c.execute('SELECT id FROM project_index_runs WHERE project_id=?',(pid,))]
    folder=db.DATA/'git-history'/'objects';keys=[p.name for p in folder.iterdir() if p.is_dir() and (p/'ATLAS-GIT.json').is_file()] if folder.exists() else []
    objects={}
    for parent in db.blob_root().iterdir():
        if parent.is_dir() and re.fullmatch('[0-9a-f]{2}',parent.name):
            for f in parent.iterdir():
                if re.fullmatch('[0-9a-f]{64}',f.name) and f.is_file():objects[f.name]=f.stat().st_size
    return {'assets':{r['relpath']:{'size':r['size'],'mtime':r['mtime'],'sha':r['sha']} for r in rows},'git_keys':keys,'runs':runs,'objects':objects}


def send_git(cfg,repo):
    meta=json.loads(repo['body']);tips=sorted({r['oid'] for r in meta['refs']}|set(meta['heads']))
    if not tips:return None
    receiver=subprocess.Popen(ssh_args(cfg,'receive-git',meta['fingerprint']),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    pack=None
    try:
        receiver.stdin.write(json.dumps(meta).encode()+b'\n');receiver.stdin.flush()
        options=['--revs'];inputs=tips
        if meta.get('missing_count'):
            lines=git_history.run(repo['source_root'],'rev-list','--objects','--all',*meta['heads'],'--missing=print','--no-object-names').decode().splitlines()
            actual=sorted(x[1:] for x in lines if x.startswith('?'))
            if actual!=meta['missing_objects']:raise ValueError('部分克隆对象已发生变化，请重新索引同步')
            inputs=[x for x in lines if not x.startswith('?')];options=[]
        pack=subprocess.Popen(git_history.command(repo['source_root'],'-c','pack.threads=2','pack-objects','--stdout',*options,'--delta-base-offset'),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=git_history.environment())
        pack.stdin.write(('\n'.join(inputs)+'\n').encode());pack.stdin.close()
        shutil.copyfileobj(pack.stdout,receiver.stdin,1024*1024);pack.stdout.close()
        errors=pack.stderr.read();rc=pack.wait()
        receiver.stdin.close();receiver.stdin=None
        out,err=receiver.communicate(timeout=600)
        if rc or receiver.returncode:raise ValueError('历史对象传输失败：'+(errors+err).decode(errors='replace')[-1500:])
        return json.loads(out)
    except BaseException:
        if pack and pack.poll() is None:pack.terminate();pack.wait()
        if receiver.poll() is None:receiver.terminate();receiver.wait()
        raise


def receive_git(key):
    if not re.fullmatch('[a-f0-9]{64}',key):raise ValueError('无效历史快照标识')
    line=sys.stdin.buffer.readline(8*1024**2)
    if not line.endswith(b'\n'):raise ValueError('历史元数据过大')
    meta=json.loads(line);identity={k:meta[k] for k in ['refs','heads']}|{'shallow':meta['shallow_boundaries']}
    if meta.get('missing_count'):identity['missing_objects']=meta['missing_objects']
    expected=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    if expected!=key:raise ValueError('历史快照指纹不一致')
    root=db.DATA/'git-history'/'objects';root.mkdir(parents=True,exist_ok=True)
    dest=root/key
    if dest.exists():
        # Drain sender safely; only already-verified immutable snapshots are reused.
        while sys.stdin.buffer.read(1024*1024):pass
        return {'key':key,'reused':True}
    work=root/('.incoming-'+uuid.uuid4().hex);work.mkdir()
    try:
        r=subprocess.run(['git','init','--bare','--object-format='+meta.get('object_format','sha1'),str(work)],capture_output=True,check=True)
        boundaries=meta.get('shallow_boundaries',[])
        if any(not git_history.HEX.fullmatch(x) for x in boundaries+meta['heads']):raise ValueError('Git 对象标识无效')
        if boundaries:(work/'shallow').write_text('\n'.join(boundaries)+'\n')
        proc=subprocess.Popen(['git','--git-dir='+str(work),'index-pack','--stdin','--fix-thin','--keep=atlas-history'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            shutil.copyfileobj(sys.stdin.buffer,proc.stdin,1024*1024);proc.stdin.close();proc.stdin=None
            output,error=proc.communicate(timeout=600)
            if proc.returncode:raise ValueError('Git 对象校验失败：'+error.decode(errors='replace')[-800:])
        finally:
            if proc.poll() is None:proc.terminate();proc.wait()
        if meta.get('missing_count'):
            for packfile in (work/'objects/pack').glob('*.pack'):packfile.with_suffix('.promisor').write_text('')
            subprocess.run(['git','--git-dir='+str(work),'config','remote.atlas-incomplete.promisor','true'],check=True)
        commands=[]
        for ref in meta['refs']:
            if not git_history.HEX.fullmatch(ref['oid']) or not ref['name'].startswith('refs/') or subprocess.run(['git','check-ref-format',ref['name']],capture_output=True).returncode:raise ValueError('Git 引用名称无效')
            commands.append('update '+ref['name']+' '+ref['oid'])
        commands += ['update refs/atlas/head-'+str(i)+' '+sha for i,sha in enumerate(meta['heads'])]
        subprocess.run(['git','--git-dir='+str(work),'update-ref','--stdin'],input=('\n'.join(commands)+'\n').encode(),capture_output=True,check=True)
        check=subprocess.run(['git','--git-dir='+str(work),'fsck','--connectivity-only','--no-reflogs'],capture_output=True,timeout=600)
        if check.returncode:raise ValueError('Git 历史连通性校验失败：'+check.stderr.decode(errors='replace')[-800:])
        (work/'ATLAS-GIT.json').write_text(json.dumps(meta,ensure_ascii=False))
        os.replace(work,dest)
        return {'key':key,'commits':meta['commits'],'shallow':meta['shallow'],'verified':True}
    finally:
        if work.exists():shutil.rmtree(work)


def send_objects(cfg,objects):
    if not objects:return {'received':0}
    root=cfg['root'];cmd=shlex.quote(root+'/app/.venv/bin/python')+' '+shlex.quote(root+'/app/scripts/remote-receiver.py')+' receive '+shlex.quote(root+'/archive')
    args=ssh_args(cfg,'unused');args[-1]=cmd
    proc=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdin,mode='w|') as tar:
            for o in objects:
                p=Path(o['source']);st=p.stat()
                if (st.st_size,st.st_mtime_ns)!=(o['size'],o['mtime_ns']):raise ValueError('传输前文件已变化，请重试：'+o['path'])
                info=tarfile.TarInfo(o['sha256'][:2]+'/'+o['sha256']);info.size=o['size'];info.mode=0o600
                with p.open('rb') as f:tar.addfile(info,f)
        proc.stdin.close();proc.stdin=None;out,err=proc.communicate(timeout=600)
        if proc.returncode:raise ValueError('归档传输失败：'+err.decode(errors='replace')[-800:])
        return json.loads(out)
    except BaseException:
        if proc.poll() is None:proc.terminate();proc.wait()
        raise


def payload(pid,inventory,progress):
    assets=project_index.rows(pid);output=[];objects={};errors=[]
    root=project_index.project(pid)['path'];overrides=project_index.source_overrides(pid)
    for i,a in enumerate(assets):
        if not (a['present'] or a['managed'] or a['versions']):continue
        try:
            source=project_index.source_for(a,root,overrides);before=source.stat();old=inventory['assets'].get(a['relpath'],{})
            sha=old.get('sha') if old.get('size')==before.st_size and old.get('mtime')==before.st_mtime and old.get('sha') in inventory['objects'] else None
            if not sha:
                with source.open('rb') as f:sha=hashlib.file_digest(f,'sha256').hexdigest()
            after=source.stat()
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('文件在读取时变化')
            output.append({**a,'size':after.st_size,'mtime':after.st_mtime,'sha256':sha,'captured_at':core.now()})
            if inventory['objects'].get(sha)!=after.st_size:objects[sha]={'sha256':sha,'source':str(source),'size':after.st_size,'mtime_ns':after.st_mtime_ns,'path':a['relpath']}
        except (OSError,ValueError) as e:errors.append({'path':a['relpath'],'error':str(e)})
        if i%500==0:progress(i,len(assets),'检查文件指纹与归档对象')
    if errors:raise ValueError('部分源文件无法读取，未发布此次同步：'+json.dumps(errors[:8],ensure_ascii=False))
    with db.connect() as c:
        p=dict(c.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone())
        tables={
          'project_directories':[dict(r) for r in c.execute('SELECT * FROM project_directories WHERE project_id=?',(pid,))],
          'project_repositories':[dict(r) for r in c.execute('SELECT * FROM project_repositories WHERE project_id=?',(pid,))],
          'project_documents':[dict(r) for r in c.execute('SELECT * FROM project_documents WHERE project_id=?',(pid,))],
          'project_relations':[dict(r) for r in c.execute('SELECT * FROM project_relations WHERE source_project=?',(pid,))],
          'project_index_state':[dict(r) for r in c.execute('SELECT * FROM project_index_state WHERE project_id=?',(pid,))],
          'repositories':[dict(r) for r in c.execute('SELECT r.* FROM repositories r JOIN project_repositories pr ON pr.repository_id=r.id WHERE pr.project_id=?',(pid,))]}
        versions=[dict(r) for r in c.execute('SELECT a.relpath,v.sha256,v.size,v.created_at,v.reason FROM versions v JOIN assets a ON a.id=v.asset_id WHERE a.project_id=? ORDER BY v.id',(pid,))]
        runs=[dict(r) for r in c.execute('SELECT * FROM project_index_runs WHERE project_id=?',(pid,)) if r['id'] not in inventory['runs']]
    histories=[]
    for v in versions:
        source=db.blob_root()/v['sha256'][:2]/v['sha256']
        if not source.is_file():continue
        histories.append(v)
        if inventory['objects'].get(v['sha256'])!=v['size']:
            st=source.stat();objects[v['sha256']]={'sha256':v['sha256'],'source':str(source),'size':st.st_size,'mtime_ns':st.st_mtime_ns,'path':v['relpath']}
    for r in runs:r['data']=base64.b64encode((db.DATA/'project-index'/Path(r['manifest']).name).read_bytes()).decode()
    from . import node,provenance
    return {'project':p,'assets':output,'tables':tables,'runs':runs,'histories':histories,'captured_at':core.now(),'source_node':node.identity(),'development':provenance.export_project(pid)},list(objects.values())


def import_payload(data):
    from . import node
    pid=data['project']['id'];p=data['project'];now=core.now();local_node=node.identity()
    with db.connect() as c:existing=c.execute('SELECT mode FROM project_locations WHERE project_id=?',(pid,)).fetchone()
    if existing and existing[0]=='workspace':raise ValueError('目标项目已绑定本节点工作目录，不能用同步归档覆盖；请使用独立目标目录或项目标识')
    for a in data['assets']+data.get('histories',[]):
        git_history.safe_relative(a['relpath']);sha=a['sha256']
        if not re.fullmatch('[0-9a-f]{64}',sha):raise ValueError('归档哈希无效')
        obj=db.blob_root()/sha[:2]/sha
        if not obj.is_file() or obj.stat().st_size!=a['size']:raise ValueError('归档对象尚未完整接收：'+a['relpath'])
    folder=db.DATA/'project-index';folder.mkdir(exist_ok=True)
    for r in data['runs']:
        if r['project_id']!=pid or not re.fullmatch(r'[0-9a-f]{32}\.json\.gz',r['manifest']):raise ValueError('索引清单路径无效')
        content=base64.b64decode(r['data'],validate=True)
        json.loads(gzip.decompress(content));(folder/r['manifest']).write_bytes(content)
    with db.connect() as c:
        c.execute('INSERT INTO projects(id,name,path,host,git_json,scan_at,scan_error,excluded_json) VALUES(?,?,?,\'synced-source\',?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,git_json=excluded.git_json,scan_at=excluded.scan_at,excluded_json=excluded.excluded_json',
                  (pid,p['name'],p['path'],p['git_json'],p['scan_at'],'来源目录位于其他节点；此节点保存同步归档映射',p['excluded_json']))
        c.execute('INSERT OR REPLACE INTO project_locations VALUES(?,?,?,?,?,?)',(pid,local_node['id'],'archive',json.dumps(data.get('source_node',{})),p['path'],now))
        c.execute('DELETE FROM mirror_files WHERE project_id=?',(pid,))
        for a in data['assets']:
            if a.get('present') or a.get('managed'):
                c.execute('INSERT INTO mirror_files VALUES(?,?,?,?)',(pid,a['relpath'],a['sha256'],a['size']))
            c.execute('INSERT INTO assets(project_id,relpath,name,ext,kind,size,mtime,git_state,repo_root,present,managed,tags,note) VALUES(?,?,?,?,?,?,?,?,?,0,?,?,?) ON CONFLICT(project_id,relpath) DO UPDATE SET size=excluded.size,mtime=excluded.mtime,kind=excluded.kind,git_state=excluded.git_state,repo_root=excluded.repo_root,present=0',
                      (pid,a['relpath'],a['name'],a['ext'],a['kind'],a['size'],a['mtime'],a['git_state'],a['repo_root'],a['managed'],a['tags'],a['note']))
            aid=c.execute('SELECT id FROM assets WHERE project_id=? AND relpath=?',(pid,a['relpath'])).fetchone()[0]
            c.execute("INSERT OR IGNORE INTO versions(asset_id,sha256,size,created_at,reason) VALUES(?,?,?,?, 'snapshot')",(aid,a['sha256'],a['size'],a['captured_at']))
        for v in data.get('histories',[]):
            aid=c.execute('SELECT id FROM assets WHERE project_id=? AND relpath=?',(pid,v['relpath'])).fetchone()
            if not aid:continue
            # Source-side archive events remain distinct from the destination's upload workflow.
            reason='source-history:'+v['reason']
            exists=c.execute('SELECT id FROM versions WHERE asset_id=? AND sha256=? AND reason=? AND created_at=?',(aid[0],v['sha256'],reason,v['created_at'])).fetchone()
            if not exists:c.execute('INSERT INTO versions(asset_id,sha256,size,created_at,reason) VALUES(?,?,?,?,?)',(aid[0],v['sha256'],v['size'],v['created_at'],reason))
        tables=data['tables']
        for r in tables['repositories']:
            meta=json.loads(r['body']);location=db.DATA/'git-history'/'objects'/meta['fingerprint']
            archived=str(location) if (location/'ATLAS-GIT.json').is_file() else None
            if meta['commits'] and not archived:raise ValueError('仓库历史副本未就绪')
            c.execute('INSERT INTO repositories VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET source_root=excluded.source_root,common_dir=excluded.common_dir,body=excluded.body,indexed_at=excluded.indexed_at,archive_path=excluded.archive_path,archive_at=excluded.archive_at',
                      (r['id'],r['source_root'],r['common_dir'],r['body'],r['indexed_at'],archived,now if archived else None))
            if archived:c.execute('INSERT OR IGNORE INTO repository_archives VALUES(?,?,?,?,?)',(r['id'],meta['fingerprint'],r['body'],archived,now))
        for name in ['project_directories','project_repositories','project_documents','project_index_state']:
            c.execute('DELETE FROM '+name+' WHERE project_id=?',(pid,))
            for r in tables[name]:
                if r['project_id']!=pid:raise ValueError('索引项目范围不匹配')
                if name=='project_index_state':state=json.loads(r['body']);state['synced_at']=now;state['source_host']=data.get('source_node',{}).get('label','来源节点');r['body']=json.dumps(state,ensure_ascii=False)
                columns=list(r);c.execute('INSERT INTO '+name+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',[r[k] for k in columns])
        c.execute('DELETE FROM project_relations WHERE source_project=? AND manual=0',(pid,))
        for r in tables['project_relations']:
            if r['source_project']!=pid:raise ValueError('引用项目范围不匹配')
            columns=list(r);c.execute('INSERT OR IGNORE INTO project_relations('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',[r[k] for k in columns])
        for r in data['runs']:c.execute('INSERT OR IGNORE INTO project_index_runs VALUES(?,?,?,?,?)',(r['id'],pid,r['created_at'],r['body'],r['manifest']))
        allowed={'development_sessions':{'id','project_id','title','body','updated_at'},'development_turns':{'id','session_id','project_id','started_at','body'},'development_artifacts':{'id','turn_id','project_id','path','action','body'}}
        for name,columns in allowed.items():
            for row in data.get('development',{}).get(name,[]):
                if set(row)!=columns:raise ValueError('开发记录字段无效')
                keys=list(row);c.execute('INSERT OR REPLACE INTO '+name+'('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',[row[k] for k in keys])
        if data.get('development',{}).get('development_turns'):
            c.execute("INSERT OR REPLACE INTO settings VALUES('provenance_status',?)",(json.dumps({'status':'synced','checked_at':now,'source_node':data.get('source_node',{}).get('label','来源节点')}),))
        db.audit(c,'project_sync_received',{'project_id':pid,'files':len(data['assets']),'repositories':len(tables['repositories']),'synced_at':now})
    return {'project_id':pid,'files':len(data['assets']),'repositories':len(tables['repositories']),'synced_at':now}


def synchronize(progress,pid=None):
    cfg=config()
    from . import provenance
    provenance.ingest(progress)
    with db.connect() as c:ids=[r[0] for r in c.execute("SELECT p.id FROM projects p LEFT JOIN project_locations l ON l.project_id=p.id WHERE (l.mode IS NULL OR l.mode='workspace')"+(' AND p.id=?' if pid else ''),(pid,) if pid else ())]
    results=[];known_keys=set()
    for i,pid in enumerate(ids):
        name=project_index.project(pid)['name'];progress(i,len(ids),name+' · 更新本机工程索引')
        state=project_index.index_one(pid,lambda n,t,d:progress(i,len(ids),d))
        inventory=remote_json(cfg,'inventory',pid);known_keys.update(inventory['git_keys'])
        with db.connect() as c:repos=[dict(r) for r in c.execute('SELECT r.* FROM repositories r JOIN project_repositories pr ON r.id=pr.repository_id WHERE pr.project_id=?',(pid,))]
        for repo in repos:
            meta=json.loads(repo['body'])
            if meta['commits'] and meta['fingerprint'] not in known_keys:
                progress(i,len(ids),name+' · 传输 Git 历史 '+Path(repo['source_root']).name)
                send_git(cfg,repo);known_keys.add(meta['fingerprint'])
        data,objects=payload(pid,inventory,lambda n,t,d:progress(i,len(ids),name+' · '+d+f' {n}/{t}'))
        progress(i,len(ids),name+f' · 归档 {len(objects)} 个新增或变化的内容对象')
        sent=send_objects(cfg,objects)
        result=remote_json(cfg,'import',payload=data);result['transferred_objects']=sent['received'];results.append(result)
        with db.connect() as c:
            saved=c.execute('SELECT body FROM project_index_state WHERE project_id=?',(pid,)).fetchone()
            if saved:
                state=json.loads(saved['body']);state['synced_at']=result['synced_at'];state['sync_destination']=cfg['host']
                c.execute('UPDATE project_index_state SET body=? WHERE project_id=?',(json.dumps(state,ensure_ascii=False),pid))
            for repo in repos:
                if json.loads(repo['body'])['commits']:c.execute('UPDATE repositories SET archive_at=? WHERE id=?',(result['synced_at'],repo['id']))
            db.audit(c,'project_sync_sent',result)
    progress(len(ids),len(ids),'工程结构、内容、关系与已有 Git 历史同步完成')
    return {'projects':results,'destination':cfg['host'],'source_repositories_unchanged':True}


def main():
    db.init();mode=sys.argv[1]
    if mode=='inventory':result=remote_inventory(sys.argv[2])
    elif mode=='receive-git':result=receive_git(sys.argv[2])
    elif mode=='import':
        with core.MUTATION_LOCK:result=import_payload(json.load(sys.stdin))
    elif mode=='import-development':
        from . import provenance
        with core.MUTATION_LOCK:result=provenance.receive_project(json.load(sys.stdin))
    else:raise ValueError('不支持的同步操作')
    print(json.dumps(result,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
