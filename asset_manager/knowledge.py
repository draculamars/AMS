"""Versioned, redacted knowledge inventory. Never execute a skill or recorded tool."""
import difflib
import hashlib
import json
import os
import re
import tomllib
from pathlib import Path
from . import core, db, node, provenance, vault

SKIP={'.git','node_modules','__pycache__','.venv','worktrees','sessions','archived_sessions','attachments','browser','tmp','.tmp'}

def roots():
    root=provenance.source_root();result=[]
    if root and root.is_dir():
        for name in ['skills','plugins','rules','memories','automations']:
            if (root/name).is_dir():result.append(root/name)
    personal=Path.home()/'.agents/skills'
    if personal.is_dir():result.append(personal)
    with db.connect() as c:projects=[dict(r) for r in c.execute("SELECT p.* FROM projects p LEFT JOIN project_locations l ON l.project_id=p.id WHERE l.mode IS NULL OR l.mode='workspace'")]
    for p in projects:
        base=Path(p['path'])
        for name in ['.agents/skills','.codex/skills','.codex/rules']:
            if (base/name).is_dir():result.append(base/name)
    return root,result,projects

def candidates():
    root,folders,projects=roots();seen=set()
    allowed=[Path(p['path']).resolve() for p in projects]+([root.resolve()] if root else [])
    for folder in folders:
        if folder.name=='skills':
            for alias in sorted(folder.iterdir()):
                if alias.is_symlink() and alias.is_dir() and any(alias.resolve().is_relative_to(p) for p in allowed):
                    entry=alias/'SKILL.md'
                    if entry.is_file() and str(entry) not in seen:seen.add(str(entry));yield entry,'skill'
        for base,dirs,files in os.walk(folder,followlinks=False):
            dirs[:]=sorted(d for d in dirs if d not in SKIP and not (Path(base)/d).is_symlink())
            for name in sorted(files):
                path=Path(base)/name
                if path.is_symlink():continue
                kind='skill' if name=='SKILL.md' else 'plugin' if name=='plugin.json' and '.codex-plugin' in path.parts else 'memory' if 'memories' in path.parts and path.suffix in {'.md','.txt'} else 'rule' if 'rules' in path.parts and path.suffix in {'.md','.rules','.txt'} else 'automation' if name=='automation.toml' else None
                if kind and str(path) not in seen:seen.add(str(path));yield path,kind
    configs=[]
    if root:configs += [root/'config.toml',root/'AGENTS.md']+list(root.glob('*.config.toml'))
    for p in projects:configs += [Path(p['path'])/'AGENTS.md',Path(p['path'])/'AGENTS.override.md',Path(p['path'])/'.codex/config.toml']
    for path in configs:
        if path.is_file() and not path.is_symlink() and str(path) not in seen:
            seen.add(str(path));yield path,'configuration' if path.suffix=='.toml' else 'instruction'

def describe(path,kind):
    if path.stat().st_size>2*1024**2:raise ValueError('知识入口超过 2 MiB，原件仍可备份')
    raw=path.read_bytes();text=raw.decode('utf-8',errors='replace');sha=hashlib.sha256(raw).hexdigest()
    title=path.parent.name if kind=='skill' else path.name
    body={'resolved_path':str(path.resolve()),'source_node':node.identity(),'bytes':len(raw),'preview_truncated':len(text)>16000,'resources':[],
          'coverage':'展示为脱敏预览；完整原件通过加密恢复点保存。依赖只记录明确引用，不推断已执行。'}
    if kind=='configuration':
        parsed=tomllib.loads(text)
        # Only keys and enabled states, never config/env values or command arguments.
        body['sections']=sorted(parsed)
        body['tools']=[{'name':str(k),'enabled':v.get('enabled',True) if isinstance(v,dict) else True} for k,v in parsed.get('mcp_servers',{}).items()]
        body['plugins']=[{'name':str(k),'enabled':v.get('enabled',True) if isinstance(v,dict) else True} for k,v in parsed.get('plugins',{}).items()]
        text='配置项：'+', '.join(body['sections'])+'\nMCP：'+', '.join(x['name'] for x in body['tools'])+'\n插件：'+', '.join(x['name'] for x in body['plugins'])
    elif kind=='automation':
        parsed=tomllib.loads(text);title=str(parsed.get('name',path.parent.name))
    elif kind=='instruction':
        title=path.parent.name+' / '+path.name
    elif kind=='plugin':
        parsed=json.loads(text);title=str(parsed.get('name',title));body['version']=str(parsed.get('version','未声明'))
        text=json.dumps({k:parsed[k] for k in ['name','version','description','skills'] if k in parsed},ensure_ascii=False,indent=2)
    elif kind=='skill':
        found=re.search(r'^name:\s*(.+)$',text,re.M)
        if found:title=found.group(1).strip('"\'')
        count=0
        for base,dirs,files in os.walk(path.parent,followlinks=False):
            dirs[:]=[d for d in dirs if d not in SKIP and not (Path(base)/d).is_symlink()]
            for name in files:
                f=Path(base)/name
                if f.is_symlink() or core.secret(f):continue
                count+=1
                if len(body['resources'])<200:body['resources'].append({'path':f.relative_to(path.parent).as_posix(),'bytes':f.stat().st_size})
        body['resource_count']=count
    body['text']=provenance.clean(text)
    body['references']=[]
    for target in re.findall(r'\]\(([^)]+)\)',text):
        if target.startswith(('http:','https:','skill:')):continue
        relative=target.split('#')[0]
        if not relative or core.secret(relative):continue
        p=(path.parent/relative).resolve()
        body['references'].append({'reference':relative,'exists':p.exists(),'inside_package':p.is_relative_to(path.parent.resolve()),'kind':'explicit_link'})
    return title,sha,body

def scan(progress=lambda *a:None):
    vault.init();origin=node.identity()['id'];count=0;errors=[];seen=[]
    for path,kind in candidates():
        try:
            title,sha,body=describe(path,kind)
            ident=hashlib.sha256((origin+':'+str(path)).encode()).hexdigest()[:32]
            seen.append(ident);now=core.now()
            with db.connect() as c:
                c.execute('INSERT OR REPLACE INTO knowledge_items VALUES(?,?,?,?,?,?,?,?,1)',(ident,origin,kind,title,str(path),sha,json.dumps(body,ensure_ascii=False),now))
                c.execute('INSERT OR IGNORE INTO knowledge_versions VALUES(?,?,?,?)',(ident,sha,json.dumps(body,ensure_ascii=False),now))
            count+=1;progress(count,0,'知识目录 · '+title)
        except (OSError,ValueError) as e:errors.append({'path':str(path),'error':str(e)})
    with db.connect() as c:
        # A failed partial scan must not retire entries that may still exist.
        if not errors:
            c.execute('UPDATE knowledge_items SET present=0 WHERE node_id=?',(origin,))
            c.executemany('UPDATE knowledge_items SET present=1 WHERE id=?',[(x,) for x in seen])
        db.audit(c,'knowledge_scan',{'items':count,'errors':errors[:50]})
    return {'items':count,'errors':errors,'node':node.identity()}

def listing(q='',kind='',page=1):
    vault.init()
    if not 1<=page<=10000:raise ValueError('分页参数无效')
    sql=' FROM knowledge_items WHERE present=1';args=[]
    if q:sql+=' AND (name LIKE ? OR path LIKE ? OR body LIKE ?)';args += ['%'+q[:200]+'%']*3
    if kind:sql+=' AND kind=?';args.append(kind)
    with db.connect() as c:
        total=c.execute('SELECT COUNT(*)'+sql,args).fetchone()[0]
        items=[dict(r) for r in c.execute('SELECT *'+sql+' ORDER BY kind,name LIMIT 40 OFFSET ?',args+[(page-1)*40])]
        counts={r[0]:r[1] for r in c.execute('SELECT kind,COUNT(*) FROM knowledge_items WHERE present=1 GROUP BY kind')}
    for r in items:
        r['body']=json.loads(r['body']);r['body'].pop('text',None);r['body'].pop('resources',None)
    return {'items':items,'total':total,'page':page,'counts':counts}

def detail(ident):
    with db.connect() as c:
        r=c.execute('SELECT * FROM knowledge_items WHERE id=?',(ident,)).fetchone()
        versions=[dict(x) for x in c.execute('SELECT * FROM knowledge_versions WHERE item_id=? ORDER BY created_at DESC',(ident,))]
    if not r:raise ValueError('知识条目不存在')
    r=dict(r);r['body']=json.loads(r['body']);r['versions']=versions
    for x in versions:x['body']=json.loads(x['body'])
    if len(versions)>1:
        previous=next(x for x in versions if x['sha256']!=r['sha256'])
        r['diff']='\n'.join(difflib.unified_diff(previous['body'].get('text','').splitlines(),r['body'].get('text','').splitlines(),fromfile='previous distinct preview',tofile='current preview'))
    return r

def export():
    origin=node.identity()['id']
    with db.connect() as c:
        items=[dict(r) for r in c.execute('SELECT * FROM knowledge_items WHERE node_id=?',(origin,))]
        versions=[dict(r) for r in c.execute('SELECT v.* FROM knowledge_versions v JOIN knowledge_items i ON i.id=v.item_id WHERE i.node_id=?',(origin,))]
    return {'items':items,'versions':versions}

def receive(c,origin,data):
    accepted=set()
    for r in data.get('items',[]):
        if r['node_id']!=origin:raise ValueError('知识条目来源不匹配')
        old=c.execute('SELECT node_id FROM knowledge_items WHERE id=?',(r['id'],)).fetchone()
        if old and old[0]!=origin:raise ValueError('知识条目身份冲突')
        c.execute('INSERT OR REPLACE INTO knowledge_items VALUES(?,?,?,?,?,?,?,?,?)',tuple(r[k] for k in ['id','node_id','kind','name','path','sha256','body','updated_at','present']))
        accepted.add(r['id'])
    for r in data.get('versions',[]):
        if r['item_id'] not in accepted:raise ValueError('知识版本范围不匹配')
        c.execute('INSERT OR IGNORE INTO knowledge_versions VALUES(?,?,?,?)',tuple(r[k] for k in ['item_id','sha256','body','created_at']))
