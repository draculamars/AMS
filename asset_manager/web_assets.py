"""Versioned web publications over immutable archive objects. No application execution."""
import gzip
import hashlib
import hmac
import json
import mimetypes
import posixpath
import re
import secrets
import time
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from . import core, db, node

SCHEMA = '''
CREATE TABLE IF NOT EXISTS web_bundles(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,created_at TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS web_entries(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,path TEXT NOT NULL,title TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(project_id,path));
CREATE TABLE IF NOT EXISTS web_publications(id TEXT PRIMARY KEY,entry_id TEXT NOT NULL,bundle_id TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(entry_id,bundle_id));
'''

def init():
    with db.connect() as c:c.executescript(SCHEMA)

def relative(path):
    if not isinstance(path,str) or not path or '\\' in path or '\x00' in path or any(ord(x)<32 for x in path):raise ValueError('网页路径无效')
    p=PurePosixPath(path)
    if p.is_absolute() or '..' in p.parts or path!=p.as_posix():raise ValueError('网页路径必须是规范相对路径')
    return path

def blob(sha,size=None):
    if not re.fullmatch('[a-f0-9]{64}',sha):raise ValueError('网页对象哈希无效')
    path=db.blob_root()/sha[:2]/sha
    if path.is_symlink() or not path.is_file() or (size is not None and path.stat().st_size!=size):raise ValueError('网页归档对象缺失：'+sha)
    return path

class Document(HTMLParser):
    def __init__(self):
        super().__init__();self.in_title=False;self.title=[];self.references=[];self.scripts=False;self.base=False
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='title':self.in_title=True
        if tag=='script':self.scripts=True
        if tag=='base':self.base=True
        for k in ('src','href','poster','data'):
            if a.get(k):self.references.append({'value':a[k],'kind':'navigation' if tag=='a' else 'resource'})
        if a.get('srcset'):
            self.references.extend({'value':x.strip().split()[0],'kind':'resource'} for x in a['srcset'].split(',') if x.strip())
    def handle_endtag(self,tag):
        if tag=='title':self.in_title=False
    def handle_data(self,data):
        if self.in_title:self.title.append(data)

def analyze(path,raw,available):
    d=Document();d.feed(raw);refs=[]
    for r in d.references:
        u=urlsplit(r['value']);value=unquote(u.path)
        if u.scheme or u.netloc:
            status='embedded' if u.scheme in {'data','blob'} else 'external'
            target=None
        elif not value:status='fragment';target=path
        elif value.startswith('/'):status='root_path';target=None
        else:
            target=posixpath.normpath(posixpath.join(posixpath.dirname(path),value))
            status='resolved' if target in available else 'missing'
        refs.append({**r,'target':target,'status':status})
    api=bool(re.search(r'''(?:fetch|WebSocket|EventSource)\s*\(\s*['"](?:https?://|/api|/ws)''',raw))
    kind='service' if api else 'interactive' if d.scripts else 'document'
    if any(x in PurePosixPath(path).parts for x in ['sources','references','research']):kind='reference'
    return {'title':''.join(d.title).strip()[:240] or PurePosixPath(path).stem,'kind':kind,
            'references':refs[:500],'references_truncated':len(refs)>500,'has_scripts':d.scripts,'has_base':d.base,
            'coverage':'HTML 直接引用；动态请求、JS/CSS 深层依赖和运行环境需另行验证',
            'issues':sum(r['status'] in {'missing','root_path'} and r['kind']=='resource' for r in refs),
            'external_resources':sum(r['status']=='external' and r['kind']=='resource' for r in refs)}

def discover():
    init();out=[];skipped=[]
    with db.connect() as c:
        projects=[dict(x) for x in c.execute('SELECT * FROM projects')]
    for p in projects:
        with db.connect() as c:
            assets=[dict(x) for x in c.execute("SELECT * FROM assets WHERE project_id=? AND ext IN ('.html','.htm') AND (present=1 OR EXISTS(SELECT 1 FROM mirror_files m WHERE m.project_id=assets.project_id AND m.path=assets.relpath))",(p['id'],))]
            available={x[0] for x in c.execute('SELECT relpath FROM assets WHERE project_id=?',(p['id'],))}
        for a in assets:
            if a['size']>8*1024**2:
                skipped.append({'project':p['id'],'path':a['relpath'],'reason':'HTML 超过 8 MiB'});continue
            try:
                raw=core.source_path(a['id'])[0].read_text(errors='replace');body=analyze(a['relpath'],raw,available)
                with db.connect() as c:
                    body['sessions']=[dict(x) for x in c.execute('SELECT DISTINCT s.id,s.title FROM development_artifacts a JOIN development_turns t ON t.id=a.turn_id JOIN development_sessions s ON s.id=t.session_id WHERE a.project_id=? AND a.path=? LIMIT 20',(p['id'],a['relpath']))]
                    ident=hashlib.sha256((p['id']+'\0'+a['relpath']).encode()).hexdigest()[:24]
                    c.execute('INSERT INTO web_entries VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,kind=excluded.kind,body=excluded.body,updated_at=excluded.updated_at',(ident,p['id'],a['relpath'],body['title'],body['kind'],json.dumps(body,ensure_ascii=False),core.now()))
                out.append(ident)
            except (OSError,ValueError) as error:
                skipped.append({'project':p['id'],'path':a['relpath'],'reason':str(error)})
    return {'registered':len(out),'skipped':skipped,'source_files_unchanged':True}

def publish(project_id,files=None,metadata=None):
    """CLI-only: publish existing, verified objects. Never access an original host path."""
    init();metadata=metadata or {}
    if files is None:
        with db.connect() as c:files={r['path']:{'sha256':r['sha256'],'size':r['size']} for r in c.execute('SELECT * FROM mirror_files WHERE project_id=?',(project_id,))}
    if not files:raise ValueError('没有本节点已接收的内容')
    for path,info in files.items():relative(path);blob(info['sha256'],info['size'])
    manifest={'project_id':project_id,'files':files,'metadata':metadata}
    content=json.dumps(manifest,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    bid=hashlib.sha256(content).hexdigest();folder=db.DATA/'web-bundles';folder.mkdir(exist_ok=True)
    dest=folder/(bid+'.json.gz')
    if not dest.exists():
        tmp=dest.with_suffix('.tmp');tmp.write_bytes(gzip.compress(content,mtime=0));tmp.replace(dest)
    now=core.now();published=[];archive_node=node.identity()['id']
    with db.connect() as c:
        if not c.execute('SELECT 1 FROM projects WHERE id=?',(project_id,)).fetchone():raise ValueError('工程不存在')
        c.execute('INSERT OR IGNORE INTO web_bundles VALUES(?,?,?,?)',(bid,project_id,now,json.dumps({'files':len(files),'bytes':sum(x['size'] for x in files.values()),'metadata':metadata},ensure_ascii=False)))
        for entry in c.execute('SELECT * FROM web_entries WHERE project_id=?',(project_id,)).fetchall():
            if entry['path'] not in files:continue
            raw=blob(files[entry['path']]['sha256']).read_text(errors='replace')
            b=analyze(entry['path'],raw,files)
            b.update(archived=True,verification='pending',verified_at=None,archive_node=archive_node)
            pub=hashlib.sha256((entry['id']+bid).encode()).hexdigest()[:32]
            c.execute('INSERT OR IGNORE INTO web_publications VALUES(?,?,?,?,?)',(pub,entry['id'],bid,json.dumps(b,ensure_ascii=False),now));published.append(pub)
        db.audit(c,'web_publish',{'project_id':project_id,'bundle':bid,'entries':len(published)})
    return {'bundle_id':bid,'publications':len(published),'files':len(files)}

@lru_cache(maxsize=3)
def manifest(data_dir,bid):
    if not re.fullmatch('[a-f0-9]{64}',bid):raise ValueError('网页发布标识无效')
    raw=gzip.decompress((Path(data_dir)/'web-bundles'/(bid+'.json.gz')).read_bytes())
    if hashlib.sha256(raw).hexdigest()!=bid:raise ValueError('网页清单校验失败')
    return json.loads(raw)

def listing(q='',project='',kind='',page=1,limit=40):
    init()
    if page<1 or not 1<=limit<=200:raise ValueError('分页参数无效')
    where=' WHERE (?=\'\' OR e.project_id=?) AND (?=\'\' OR e.kind=?) AND (?=\'\' OR e.title LIKE ? OR e.path LIKE ?)'
    args=(project,project,kind,kind,q,'%'+q+'%','%'+q+'%')
    with db.connect() as c:
        total=c.execute('SELECT count(*) FROM web_entries e'+where,args).fetchone()[0]
        rows=[dict(r) for r in c.execute('SELECT e.*,p.name project_name FROM web_entries e JOIN projects p ON p.id=e.project_id'+where+' ORDER BY e.updated_at DESC,e.path LIMIT ? OFFSET ?',(*args,limit,(page-1)*limit))]
        for row in rows:
            row['body']=json.loads(row['body'])
            pubs=[{**dict(r),'body':json.loads(r['body'])} for r in c.execute('SELECT * FROM web_publications WHERE entry_id=? ORDER BY created_at DESC',(row['id'],))]
            row['publications']=pubs
    return {'items':rows,'total':total,'page':page,'node':node.identity()}

def signing_key():
    key=db.setting('web_preview_key')
    if not key:
        with db.connect() as c:
            c.execute("INSERT OR IGNORE INTO settings VALUES('web_preview_key',?)",(secrets.token_hex(32),))
        key=db.setting('web_preview_key')
    return bytes.fromhex(key)

def preview_ticket(publication_id):
    with db.connect() as c:
        r=c.execute('SELECT w.*,e.path FROM web_publications w JOIN web_entries e ON e.id=w.entry_id WHERE w.id=?',(publication_id,)).fetchone()
    if not r:raise ValueError('网页发布不存在')
    expires=str(int(time.time())+3600);payload=r['bundle_id']+'.'+expires
    token=expires+'.'+hmac.new(signing_key(),payload.encode(),hashlib.sha256).hexdigest()
    from urllib.parse import quote
    return {'url':'/api/web-preview/'+token+'/'+r['bundle_id']+'/'+quote(r['path'],safe='/'),'expires_in':3600}

def preview_file(token,bid,path):
    try:expires,signature=token.split('.')
    except ValueError:raise ValueError('预览凭证无效')
    if not expires.isdecimal() or int(expires)<time.time() or int(expires)>time.time()+3700:raise ValueError('预览已过期，请从网页目录重新打开')
    expected=hmac.new(signing_key(),(bid+'.'+expires).encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,signature):raise ValueError('预览凭证无效')
    relative(path);info=manifest(str(db.DATA),bid)['files'].get(path)
    if not info:raise FileNotFoundError('此路径不在固定发布版本中')
    return blob(info['sha256'],info['size']),mimetypes.guess_type(path)[0] or 'application/octet-stream'

def verify(bid):
    m=manifest(str(db.DATA),bid);checked=0
    for info in {v['sha256']:v for v in m['files'].values()}.values():
        with blob(info['sha256'],info['size']).open('rb') as f:
            if hashlib.file_digest(f,'sha256').hexdigest()!=info['sha256']:raise ValueError('内容校验失败')
        checked+=1
    return {'bundle_id':bid,'hashes_verified':checked,'status':'passed','scope':'manifest_and_content_only'}

def record_browser_verification(publication_id,evidence):
    """CLI evidence import, never an automatic consequence of opening a page."""
    if evidence.get('status')!='passed' or not evidence.get('checks') or not evidence.get('tested_url'):
        raise ValueError('需要明确的浏览器检查项、测试入口和通过结果')
    # Never retain a live signed URL in the human-readable receipt.
    evidence={**evidence,'tested_url':evidence['tested_url'].split('/api/web-preview/')[0]+'/api/web-preview/[redacted]'}
    with db.connect() as c:
        row=c.execute('SELECT body FROM web_publications WHERE id=?',(publication_id,)).fetchone()
        if not row:raise ValueError('网页发布不存在')
        body=json.loads(row['body']);body.update(verification='browser_verified',verified_at=core.now(),browser_evidence=evidence)
        c.execute('UPDATE web_publications SET body=? WHERE id=?',(json.dumps(body,ensure_ascii=False),publication_id))
        db.audit(c,'web_browser_verified',{'publication':publication_id,'checks':evidence['checks']})
    return {'publication_id':publication_id,'status':'browser_verified','scope':evidence['checks']}
