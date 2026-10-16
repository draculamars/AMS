"""Project-wide structure, document search, evidence-backed links and index history."""
import ast
import gzip
import hashlib
import json
import os
import posixpath
import re
import time
import uuid
from collections import Counter
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from . import core, db, models, git_history

DOC_EXTS={'.md','.markdown','.txt','.rst'}
PARSE_EXTS=DOC_EXTS|{'.py','.js','.ts','.tsx','.jsx','.swift','.c','.cpp','.h','.hpp','.urdf','.xacro','.obj','.mtl','.gltf','.json','.yaml','.yml','.toml'}
RUNTIME_PARTS={'SourcePackages','_deps','node_modules','Contents','products','Products','site-packages','dist-packages','CMakeFiles'}


def project(pid):
    with db.connect() as c:p=c.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone()
    if not p:raise ValueError('项目不存在')
    return dict(p)


def rows(pid):
    with db.connect() as c:return [dict(r) for r in c.execute('''SELECT a.*,
        (SELECT COUNT(*) FROM versions v WHERE v.asset_id=a.id) AS versions,
        (SELECT v.sha256 FROM versions v WHERE v.asset_id=a.id AND v.reason NOT LIKE 'source-history:%' ORDER BY v.id DESC LIMIT 1) AS archived_sha
        FROM assets a WHERE project_id=? AND (present=1 OR EXISTS(SELECT 1 FROM versions v WHERE v.asset_id=a.id)) ORDER BY relpath''',(pid,))]


def source_overrides(pid):
    with db.connect() as c:
        return {r['asset_id']:r['sha256'] for r in c.execute("SELECT v.asset_id,v.sha256 FROM versions v WHERE v.id IN (SELECT MAX(v.id) FROM versions v JOIN assets a ON a.id=v.asset_id WHERE a.project_id=? AND v.reason IN ('upload','update') GROUP BY v.asset_id)",(pid,))}


def source_for(a,root,overrides):
    sha=overrides.get(a['id']) or (a.get('archived_sha') if not a['present'] else None)
    if sha:
        source=db.blob_root()/sha[:2]/sha
        if not source.is_file():raise FileNotFoundError('归档内容缺失：'+a['relpath'])
        return source
    try:return core.safe_path(root,a['relpath'])
    except FileNotFoundError:
        # Keep the same offline-source fallback as the interactive reader.
        if not a.get('archived_sha'):raise
        source=db.blob_root()/a['archived_sha'][:2]/a['archived_sha']
        if not source.is_file():raise
        return source


def runtime(path):
    return bool(set(PurePosixPath(path).parts)&RUNTIME_PARTS) or '.benchmark' in path or '.app/' in path


def resolve(source_pid,source_path,reference,lookup,projects,dirs=None):
    ref=unquote(reference).replace('\\','/').split('#',1)[0].split('?',1)[0]
    if not ref:return None,None,'anchor'
    if '${' in ref or '$(' in ref:return None,None,'dynamic'
    if ref.startswith(('https://','http://','mailto:','data:')):return None,None,'external'
    if ref.startswith('project://'):candidates=[(source_pid,posixpath.normpath(ref[10:]))]
    elif ref.startswith(('package://','model://')):
        pkg,_,tail=ref.split('://',1)[1].partition('/');candidates=[]
        for parent in PurePosixPath(source_path).parents:candidates.extend([(source_pid,str(parent/pkg/tail)),(source_pid,str(parent/tail))])
    elif ref.startswith('/'):
        candidates=[]
        for p in projects:
            prefix=p['path'].rstrip('/')+'/'
            if ref.startswith(prefix):candidates.append((p['id'],ref[len(prefix):]))
    elif urlsplit(ref).scheme:return None,None,'external'
    else:candidates=[(source_pid,posixpath.normpath(posixpath.join(posixpath.dirname(source_path),ref))),(source_pid,posixpath.normpath(ref))]
    for pid,path in candidates:
        try:git_history.safe_relative(path)
        except ValueError:continue
        if (pid,path) in lookup:return pid,path,'resolved'
        if dirs and (pid,path) in dirs:return pid,path,'directory'
    return None,None,'missing'


def text_references(a,text):
    refs=[];ext=a['ext']
    if ext in DOC_EXTS:
        refs += [(m[1].strip('<>'),'illustrates' if m[0] else 'documents') for m in re.findall(r'(!?)\[[^\]\n]*\]\(\s*([^\s)]+)(?:\s+"[^"]*")?\s*\)',text)]
        refs += [(x,'documents') for x in re.findall(r'^\s*\[[^\]]+\]:\s*(\S+)',text,re.M)]
    if ext in {'.js','.ts','.tsx','.jsx'}:
        refs += [(x,'imports') for x in re.findall(r'(?:from\s*|import\s*\(|require\s*\()?[\"\'](\.{1,2}/[^\"\']+)[\"\']',text)]
    if ext in {'.c','.h','.cpp','.hpp'}:refs += [(x,'imports') for x in re.findall(r'#include\s*"([^"]+)"',text)]
    if ext=='.py':
        try:
            tree=ast.parse(text)
            for n in ast.walk(tree):
                if isinstance(n,ast.ImportFrom):
                    module=(n.module or '').replace('.','/')
                    prefix='../'*max(0,n.level-1) if n.level else ''
                    refs.append((prefix+module+'.py','imports'))
                    if n.level and not module:refs += [(prefix+x.name+'.py','imports') for x in n.names]
                elif isinstance(n,ast.Import):refs += [(x.name.replace('.','/')+'.py','imports') for x in n.names]
        except SyntaxError:pass
    return refs


def index_one(pid,progress=lambda *a:None,rescan=True):
    p=project(pid)
    from . import node
    if not node.location(pid)['local_available']:raise ValueError('本节点保存的是归档映射或离线目录，请在来源节点更新后同步')
    if not Path(p['path']).is_dir():raise ValueError('此主机没有原项目目录。请在来源节点更新索引并同步，或绑定本节点目录。')
    with db.connect() as c:c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('watch_dirty:'+pid,'1'))
    if rescan:core.scan_project(pid)
    repo_count=git_history.discover(pid)
    assets=rows(pid);overrides=source_overrides(pid)
    with db.connect() as c:
        projects=[dict(x) for x in c.execute('SELECT id,path FROM projects')]
        lookup={(x['project_id'],x['relpath']):x['id'] for x in c.execute('SELECT project_id,relpath,id FROM assets WHERE present=1 OR EXISTS(SELECT 1 FROM versions WHERE asset_id=assets.id)')}
        previous=c.execute('SELECT * FROM project_index_runs WHERE project_id=? ORDER BY created_at DESC LIMIT 1',(pid,)).fetchone()
    # Directory lookups are kept separate from file lookups; no fuzzy same-name dependency assertions.
    dirs=set()
    for source_pid,source_path in lookup:
        parent=posixpath.dirname(source_path)
        while parent and (source_pid,parent) not in dirs:
            dirs.add((source_pid,parent));parent=posixpath.dirname(parent)
    with db.connect() as c:
        dirs.update((r['project_id'],r['path']) for r in c.execute("SELECT project_id,path FROM project_directories WHERE path<>''"))
    links=[];documents=[];coverage=Counter();parsed=0;doc_bytes=0
    def resolve_fast(source,ref):
        value=unquote(ref).split('#',1)[0].split('?',1)[0]
        if not value:return None,None,'anchor'
        if not value.startswith(('project://','package://','model://','/')) and not urlsplit(value).scheme:
            for candidate in [posixpath.normpath(posixpath.join(posixpath.dirname(source),value)),posixpath.normpath(value)]:
                if (pid,candidate) in lookup:return pid,candidate,'resolved'
                if (pid,candidate) in dirs:return pid,candidate,'directory'
                if PurePosixPath(candidate).suffix=='':
                    for suffix in ['.js','.ts','.tsx','.jsx','/index.js','/index.ts']:
                        if (pid,candidate+suffix) in lookup:return pid,candidate+suffix,'resolved'
            return None,None,'missing'
        return resolve(pid,source,ref,lookup,projects,dirs)
    for i,a in enumerate(assets):
        ext=a['ext']
        if ext not in PARSE_EXTS:coverage['格式仅索引']+=1;continue
        if runtime(a['relpath']):coverage['构建副本仅索引']+=1;continue
        if parsed>=10000:coverage['超出单批关系解析上限']+=1;continue
        if a['size']>1024*1024:coverage['大文件仅索引']+=1;continue
        try:
            source=source_for(a,p['path'],overrides)
            with source.open('rb') as f:data=f.read(1024*1024+1)
            if b'\0' in data[:8192]:coverage['二进制仅索引']+=1;continue
            text=data.decode('utf-8',errors='replace');refs=text_references(a,text);parsed+=1
            if ext in DOC_EXTS and doc_bytes<32*1024**2:
                body=text[:16384];doc_bytes+=len(body.encode());title=next((x.lstrip('# ').strip() for x in text.splitlines() if x.startswith('#')),a['name'])[:250]
                documents.append((pid,a['relpath'],title,body,int(len(text)>len(body))))
            if ext in {'.py','.urdf','.xacro','.obj','.mtl','.gltf','.json'}:
                try:refs+=models.references(a)
                except Exception:
                    coverage['部分引用无法静态解析']+=1
                if a.get('_analysis_note'):coverage['含动态路径的脚本']+=1
            for ref,kind in refs:
                target_pid,target_path,status=resolve_fast(a['relpath'],ref)
                # Standard-library and external package imports are not missing local files.
                if kind=='imports' and status=='missing':continue
                if status=='anchor':continue
                ident=hashlib.sha256(json.dumps([pid,a['relpath'],ref,kind],ensure_ascii=False).encode()).hexdigest()[:32]
                links.append((ident,pid,a['relpath'],target_pid,target_path,ref,kind,status,'文件内容解析',0))
        except (OSError,ValueError):coverage['源文件不可读取']+=1
        if i%250==0:progress(i,len(assets),p['name']+' · 解析目录、文档和引用')
    captured=[{'path':a['relpath'],'size':a['size'],'mtime':a['mtime'],'kind':a['kind'],'ext':a['ext'],'git_state':a['git_state'],'archived_sha':a['archived_sha']} for a in assets if a['present'] or a['managed']]
    old={x['path']:x for x in read_run(previous['id'])['files']} if previous else {}
    current={a['path']:a for a in captured}
    added=sorted(current.keys()-old.keys());removed=sorted(old.keys()-current.keys())
    changed=sorted(path for path in current.keys()&old.keys() if (current[path]['size'],current[path]['mtime'])!=(old[path]['size'],old[path]['mtime']))
    ident=uuid.uuid4().hex;created=core.now();folder=db.DATA/'project-index';folder.mkdir(exist_ok=True)
    name=ident+'.json.gz';target=folder/name
    with gzip.open(target,'wt',encoding='utf-8') as f:json.dump({'files':captured,'added':added,'removed':removed,'changed':changed},f,ensure_ascii=False)
    state={'files':len(captured),'bytes':sum(a['size'] for a in captured),'parsed_files':parsed,'relations':len({x[0] for x in links}),'documents':len(documents),'repositories':repo_count,'coverage':dict(coverage),'added':len(added),'removed':len(removed),'changed':len(changed),'previous_run':previous['id'] if previous else None,'host':__import__('asset_manager.node',fromlist=['identity']).identity()['id'],'source_path':p['path'],'latest_run':ident,'index_semantics':'目录与元数据采样；历史内容来自 Git 对象或实际归档版本，未保存的旧内容不可恢复。'}
    with db.connect() as c:
        c.execute('DELETE FROM project_relations WHERE source_project=? AND manual=0',(pid,))
        c.executemany('INSERT OR REPLACE INTO project_relations VALUES(?,?,?,?,?,?,?,?,?,?)',links)
        c.execute('DELETE FROM project_documents WHERE project_id=?',(pid,));c.executemany('INSERT INTO project_documents VALUES(?,?,?,?,?)',documents)
        c.execute('INSERT INTO project_index_runs VALUES(?,?,?,?,?)',(ident,pid,created,json.dumps(state,ensure_ascii=False),name))
        c.execute('INSERT OR REPLACE INTO project_index_state VALUES(?,?,?)',(pid,json.dumps(state,ensure_ascii=False),created))
        c.execute('DELETE FROM settings WHERE key=?',('watch_dirty:'+pid,))
        db.audit(c,'project_index',{'project_id':pid,**state})
    return state


def index_all(progress,pid=None):
    with db.connect() as c:ids=[r[0] for r in c.execute("SELECT p.id FROM projects p LEFT JOIN project_locations l ON l.project_id=p.id WHERE (l.mode IS NULL OR l.mode='workspace')"+(' AND p.id=?' if pid else ''),(pid,) if pid else ())]
    result=[]
    for i,p in enumerate(ids):
        progress(i,len(ids),'索引工程 '+project(p)['name'])
        try:result.append({'project_id':p,**index_one(p,lambda n,t,d:progress(i,len(ids),d))})
        except Exception as e:result.append({'project_id':p,'error':str(e)})
    progress(len(ids),len(ids),'工程索引完成');return {'projects':result,'failed':[x for x in result if x.get('error')]}


def read_run(ident):
    with db.connect() as c:r=c.execute('SELECT * FROM project_index_runs WHERE id=?',(ident,)).fetchone()
    if not r:raise ValueError('索引版本不存在')
    name=Path(r['manifest']).name
    with gzip.open(db.DATA/'project-index'/name,'rt',encoding='utf-8') as f:data=json.load(f)
    return {'id':r['id'],'project_id':r['project_id'],'created_at':r['created_at'],'summary':json.loads(r['body']),**data}


def tree(pid,path='',page=1,limit=150,q='',kind='',run_id='',scope='current'):
    project(pid);path=git_history.safe_relative(path,True)
    if page<1 or not 1<=limit<=500:raise ValueError('分页参数无效')
    prefix=path+'/' if path else ''
    if run_id:
        run=read_run(run_id)
        if run['project_id']!=pid:raise ValueError('索引版本与项目不匹配')
        files=[{**a,'relpath':a['path'],'id':None,'name':PurePosixPath(a['path']).name,'versions':0,'present':False} for a in run['files']]
    else:
        files=rows(pid)
        if scope=='current':
            from .node import current_paths
            paths=current_paths(pid);files=[a for a in files if a['relpath'] in paths]
        elif scope!='all':raise ValueError('文件范围无效')
    entries={};matching=0
    for a in files:
        if not a['relpath'].startswith(prefix):continue
        if q and q.casefold() not in (a['relpath']+' '+a.get('tags','')+' '+a.get('note','')).casefold():continue
        if kind and a['kind']!=kind:continue
        relative=a['relpath'][len(prefix):];part,sep,tail=relative.partition('/')
        if not part:continue
        matching+=1
        if sep:
            item=entries.setdefault(part,{'type':'directory','name':part,'path':prefix+part,'files':0,'size':0,'kinds':{}});item['files']+=1;item['size']+=a['size'];item['kinds'][a['kind']]=item['kinds'].get(a['kind'],0)+1
        else:entries[part]={'type':'file','name':part,'path':a['relpath'],'asset_id':a['id'],'size':a['size'],'ext':a['ext'],'kind':a['kind'],'git_state':a['git_state'],'versions':a['versions'],'present':bool(a['present']),'mtime':a['mtime'],'archived_sha':a.get('archived_sha')}
    if not q and not kind and not run_id:
        with db.connect() as c:
            for r in c.execute('SELECT path FROM project_directories WHERE project_id=? AND substr(path,1,?)=?',(pid,len(prefix),prefix)):
                relative=r[0][len(prefix):];part=relative.split('/')[0]
                if part:entries.setdefault(part,{'type':'directory','name':part,'path':prefix+part,'files':0,'size':0,'kinds':{}})
    items=sorted(entries.values(),key=lambda a:(a['type']!='directory',a['name'].casefold()))
    return {'items':items[(page-1)*limit:page*limit],'total':len(items),'files':matching,'page':page,'path':path,'run':run_id or None}


def overview(pid):
    p=project(pid)
    from . import node
    loc=node.location(pid)
    with db.connect() as c:
        s=c.execute('SELECT * FROM project_index_state WHERE project_id=?',(pid,)).fetchone()
        repos=[{**dict(r),'metadata':json.loads(r['body'])} for r in c.execute('SELECT pr.*,r.body,r.archive_path,r.archive_at,r.indexed_at FROM project_repositories pr JOIN repositories r ON r.id=pr.repository_id WHERE pr.project_id=? ORDER BY CASE pr.role WHEN \'主仓库\' THEN 0 WHEN \'子仓库\' THEN 1 ELSE 2 END,pr.prefix',(pid,))]
        for r in repos:r.pop('body');r['available']=Path(r['checkout_root']).is_dir() or bool(r['archive_path'] and Path(r['archive_path']).is_dir())
        for r in repos:r['archives']=[dict(x) for x in c.execute('SELECT fingerprint,created_at FROM repository_archives WHERE repository_id=? ORDER BY created_at DESC',(r['repository_id'],))]
        counts=dict(c.execute('SELECT COUNT(*) files,COALESCE(SUM(size),0) bytes FROM assets a WHERE project_id=? AND '+models.available_sql(),(pid,)).fetchone())
        kinds=[dict(r) for r in c.execute('SELECT kind,COUNT(*) count FROM assets a WHERE project_id=? AND '+models.available_sql()+' GROUP BY kind',(pid,))]
        docs=[dict(r) for r in c.execute('SELECT path,title,truncated FROM project_documents WHERE project_id=? ORDER BY CASE WHEN lower(path) LIKE \'%readme%\' THEN 0 ELSE 1 END,path LIMIT 8',(pid,))]
        related=c.execute('SELECT COUNT(*) FROM project_relations WHERE source_project=?',(pid,)).fetchone()[0]
    with db.connect() as c:
        counts['current_files']=c.execute('SELECT COUNT(*) FROM assets a WHERE project_id=? AND '+node.current_sql(),(pid,)).fetchone()[0]
        counts['development_turns']=c.execute('SELECT COUNT(*) FROM development_turns WHERE project_id=?',(pid,)).fetchone()[0]
    model_groups=models.list_projects(project=pid)['items']
    iterations=[]
    for g in model_groups:
        if re.search(r'(^|[_/.-])(v|r)\d',g['root'],re.I):iterations.append({k:g[k] for k in ['id','title','root','modified','model_count']})
    return {'project':{**p,'git':json.loads(p['git_json'])},'local_available':loc['local_available'],'location':loc,'index':{**json.loads(s['body']),'indexed_at':s['indexed_at']} if s else None,'repositories':repos,'counts':counts,'kinds':kinds,'documents':docs,'model_projects':model_groups,'iterations':iterations,'relations':related}


def list_projects():
    with db.connect() as c:ids=[r[0] for r in c.execute('SELECT id FROM projects ORDER BY name')]
    return {'items':[overview(pid) for pid in ids],'sync_configured':bool(db.setting('sync_target')),'host':__import__('asset_manager.node',fromlist=['identity']).identity()['label'],'generated_at':core.now()}


def documents(pid,q='',page=1,limit=40):
    if page<1 or not 1<=limit<=100:raise ValueError('分页参数无效')
    where="a.project_id=? AND a.kind='document' AND "+models.available_sql();args=[pid]
    # Runtime documentation remains visible in the full file tree.
    where+=" AND a.relpath NOT LIKE '%/site-packages/%' AND a.relpath NOT LIKE '%/SourcePackages/%' AND a.relpath NOT LIKE '%/CMakeFiles/%'"
    if q:where+=' AND (a.relpath LIKE ? OR d.title LIKE ? OR d.body LIKE ?)';args += ['%'+q+'%']*3
    joins=' FROM assets a LEFT JOIN project_documents d ON d.project_id=a.project_id AND d.path=a.relpath WHERE '+where
    with db.connect() as c:
        total=c.execute('SELECT COUNT(*)'+joins,args).fetchone()[0]
        items=[dict(r) for r in c.execute("SELECT a.relpath path,COALESCE(d.title,a.name) title,d.truncated,COALESCE(substr(d.body,1,260),'按名称和路径检索；打开文件预览内容。') excerpt,a.id asset_id,a.ext"+joins+" ORDER BY CASE WHEN lower(a.name) LIKE 'readme%' THEN 0 WHEN d.body IS NOT NULL THEN 1 ELSE 2 END,a.relpath LIMIT ? OFFSET ?",args+[limit,(page-1)*limit])]
    return {'items':items,'total':total,'page':page,'coverage':'文档名称与路径可检索；文本正文检索覆盖每份已解析文档的前 16,384 字符。PDF 可直接阅读，其他专业格式保留原件下载；运行环境文档在工程目录中展示。'}


def relations(pid,path='',page=1,limit=100,status=''):
    if page<1 or not 1<=limit<=200:raise ValueError('分页参数无效')
    where='(r.source_project=? OR r.target_project=?)';args=[pid,pid]
    if path:where+=' AND ((r.source_project=? AND r.source_path=?) OR (r.target_project=? AND r.target_path=?))';args += [pid,path,pid,path]
    if status:where+=' AND r.status=?';args.append(status)
    with db.connect() as c:
        total=c.execute('SELECT COUNT(*) FROM project_relations r WHERE '+where,args).fetchone()[0]
        items=[dict(r) for r in c.execute('SELECT r.*,s.id source_asset_id,t.id target_asset_id FROM project_relations r LEFT JOIN assets s ON s.project_id=r.source_project AND s.relpath=r.source_path LEFT JOIN assets t ON t.project_id=r.target_project AND t.relpath=r.target_path WHERE '+where+' ORDER BY r.manual DESC,r.source_path,r.reference LIMIT ? OFFSET ?',args+[limit,(page-1)*limit])]
    return {'items':items,'total':total,'page':page}


def add_relation(pid,body):
    source=core.asset_row(int(body['source_id']));target=core.asset_row(int(body['target_id']))
    if source['project_id']!=pid:raise ValueError('起点必须属于当前项目')
    kind=body.get('kind','depends_on')
    if kind not in {'depends_on','derived_from','documents','tests','supersedes'}:raise ValueError('关系类型无效')
    ident=hashlib.sha256(json.dumps([pid,source['relpath'],target['project_id'],target['relpath'],kind]).encode()).hexdigest()[:32]
    with db.connect() as c:
        c.execute('INSERT OR REPLACE INTO project_relations VALUES(?,?,?,?,?,?,?,?,?,1)',(ident,pid,source['relpath'],target['project_id'],target['relpath'],target['relpath'],kind,'confirmed',str(body.get('note') or '用户确认')[:1000]));db.audit(c,'project_relation',{'id':ident,'project':pid})
    return {'id':ident}


def graph(pid,path=''):
    with db.connect() as c:
        records=[dict(r) for r in c.execute('SELECT * FROM project_relations WHERE (source_project=? OR target_project=?) AND target_path IS NOT NULL LIMIT 30000',(pid,pid))]
    nodes={};edges=Counter()
    for r in records:
        if path and not (r['source_path']==path or r['target_path']==path):continue
        def node(pr,p):
            label=p if path else (p.split('/')[0] if '/' in p else p);key=pr+'\0'+label
            nodes[key]={'id':key,'label':label,'project_id':pr,'path':p if path else label,'type':'directory' if not path and '/' in p else core.EXT_KIND.get(PurePosixPath(label).suffix,'file')};return key
        a=node(r['source_project'],r['source_path']);b=node(r['target_project'],r['target_path'])
        if a!=b:edges[(a,b,r['kind'])]+=1
    ordered=sorted(edges.items(),key=lambda x:x[1],reverse=True)[:160];keep={k for (a,b,kind),n in ordered for k in [a,b]}
    return {'nodes':[v for k,v in nodes.items() if k in keep][:120],'edges':[{'source':a,'target':b,'kind':kind,'count':n} for (a,b,kind),n in ordered],'grouped':not bool(path),'total_relations':len(records),'note':'总览按一级目录聚合；选择文件可查看直接关系。仅展示已解析或已确认的连接。'}


def timeline(pid,page=1,limit=60):
    if page<1 or not 1<=limit<=200:raise ValueError('分页参数无效')
    with db.connect() as c:
        runs=[{'type':'index','id':r['id'],'created_at':r['created_at'],'summary':json.loads(r['body'])} for r in c.execute('SELECT * FROM project_index_runs WHERE project_id=?',(pid,))]
        versions=[{**dict(r),'type':'asset'} for r in c.execute('SELECT v.id,v.created_at,v.reason,v.sha256,v.size,a.relpath path,a.id asset_id FROM versions v JOIN assets a ON a.id=v.asset_id WHERE a.project_id=? ORDER BY v.id DESC LIMIT 500',(pid,))]
        snaps=[]
        for r in c.execute('SELECT * FROM model_snapshots'):
            body=json.loads(r['body'])
            if body['project_id']==pid:snaps.append({'type':'engineering','id':r['id'],'model_id':r['model_id'],'created_at':r['created_at'],'title':body['title'],'files':len(body['files'])})
    items=sorted(runs+versions+snaps,key=lambda a:a['created_at'],reverse=True)
    return {'items':items[(page-1)*limit:page*limit],'total':len(items),'page':page,'note':'Git 提交在“Git 历史”中独立查看；索引采样不等于内容备份。资产事件显示最近 500 条，单文件页保留全部版本。'}


def file_detail(pid,path):
    path=git_history.safe_relative(path)
    with db.connect() as c:a=c.execute('SELECT * FROM assets WHERE project_id=? AND relpath=?',(pid,path)).fetchone()
    if not a:raise FileNotFoundError('文件未纳入当前目录')
    a=dict(a);p=project(pid)
    with db.connect() as c:
        versions=[dict(r) for r in c.execute('SELECT * FROM versions WHERE asset_id=? ORDER BY id DESC',(a['id'],))]
        repo=c.execute('SELECT * FROM project_repositories WHERE project_id=? AND (?=prefix OR substr(?,1,length(prefix)+1)=prefix||\'/\' OR prefix=\'\') ORDER BY length(prefix) DESC LIMIT 1',(pid,path,path)).fetchone()
    return {'asset':a,'versions':versions,'repository':dict(repo) if repo else None,'relations':relations(pid,path,limit=100),'project':{'id':pid,'name':p['name']}}


def project_connections():
    with db.connect() as c:
        projects=[dict(r) for r in c.execute('SELECT id,name FROM projects ORDER BY name')]
        linked=[dict(r) for r in c.execute("SELECT a.project_id source,b.project_id target,COUNT(DISTINCT a.repository_id) count FROM project_repositories a JOIN project_repositories b ON a.repository_id=b.repository_id AND a.project_id<b.project_id GROUP BY a.project_id,b.project_id")]
        references=[dict(r) for r in c.execute("SELECT source_project source,target_project target,COUNT(*) count FROM project_relations WHERE target_project IS NOT NULL AND source_project<>target_project GROUP BY source_project,target_project")]
    return {'nodes':projects,'edges':[{**r,'kind':'shared_repository'} for r in linked]+[{**r,'kind':'cross_reference'} for r in references]}
