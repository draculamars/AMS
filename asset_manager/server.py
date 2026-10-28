import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import secrets
import sqlite3
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from . import db, core, models, model_preview, engineering_upload, engineering_api, __version__

STATIC=Path(__file__).parent/'static'
MAX_UPLOAD=int(os.environ.get('ASSET_MAX_UPLOAD_MB','2048'))*1024*1024
ATTEMPTS={}
COOKIE_NAME=os.environ.get('ASSET_COOKIE_NAME','atlas_session')

def password_hash(password,salt):
    return hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()

def set_password(password):
    if len(password)<12: raise ValueError('密码至少需要 12 个字符')
    salt=secrets.token_hex(16)
    value=json.dumps({'salt':salt,'hash':password_hash(password,salt)})
    with db.connect() as con:
        con.execute("INSERT OR REPLACE INTO settings VALUES('password',?)",(value,))
        con.execute("DELETE FROM settings WHERE key LIKE 'session:%'")
        db.audit(con,'password_changed',{})

@asynccontextmanager
async def lifespan(app):
    db.init(recover_jobs=True)
    from . import node
    node.start()
    try: yield
    finally: node.stop()

app=FastAPI(title='Atlas 本地资产管理',version=__version__,lifespan=lifespan)

@app.middleware('http')
async def protection(request,call_next):
    host=request.headers.get('host','').split(':')[0].lower()
    allowed={x.strip().lower() for x in os.environ.get('ASSET_ALLOWED_HOSTS','localhost,127.0.0.1').split(',')}
    if host not in allowed:
        return JSONResponse({'detail':'主机名未获授权，请配置 ASSET_ALLOWED_HOSTS'},400)
    path=request.url.path
    from . import portable
    if portable.enabled() and not portable.request_allowed(request.method,path):
        return JSONResponse({'detail':'便携副本仅管理盘内资产；原项目扫描、路径绑定、Git 写入和原件备份请在原部署中操作。'},409)
    if request.method not in {'GET','HEAD','OPTIONS'}:
        origin=request.headers.get('origin')
        if origin and urlsplit(origin).netloc.lower()!=request.headers.get('host','').lower():
            return JSONResponse({'detail':'拒绝跨站请求'},403)
        if request.headers.get('x-asset-request')!='1':
            return JSONResponse({'detail':'缺少请求校验标头'},403)
        try:
            if int(request.headers.get('content-length','0'))>MAX_UPLOAD+1024*1024:
                return JSONResponse({'detail':'上传文件超过大小限制'},413)
        except ValueError:
            return JSONResponse({'detail':'请求长度无效'},400)
    local=request.client and request.client.host in {'127.0.0.1','::1','testclient'}
    pw=db.setting('password')
    authenticated=False
    token=request.cookies.get(COOKIE_NAME,'')
    if token:
        exp=db.setting('session:'+hashlib.sha256(token.encode()).hexdigest())
        authenticated=bool(exp and float(exp)>time.time())
    request.state.local=local
    request.state.authenticated=authenticated
    request.state.setup=not pw
    public=path in {'/api/auth/status','/api/auth/login','/api/auth/setup','/api/health'}
    local_bootstrap=local and not pw and os.environ.get('ASSET_LOCAL_BOOTSTRAP','1')=='1'
    if path.startswith('/api/') and not public and not (authenticated or local_bootstrap):
        return JSONResponse({'detail':'请先登录' if pw else '请在服务所在主机设置管理员密码'},401)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='same-origin'
    response.headers['X-Frame-Options']='SAMEORIGIN'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; frame-src 'self' blob:; connect-src 'self' blob:; object-src 'self'; base-uri 'none'; frame-ancestors 'self'"
    if path=='/static/step-worker.js':
        # OCCT's generated Emscripten bindings use new Function inside this worker.
        # Keep that permission out of the document and other script contexts.
        response.headers['Content-Security-Policy']="default-src 'none'; script-src 'self' 'unsafe-eval'; connect-src 'self'"
    if path.startswith('/api/'):
        response.headers['Cache-Control']='no-store'
    return response

@app.exception_handler(ValueError)
async def value_error(request,error):
    return JSONResponse({'detail':str(error)},400)

@app.exception_handler(FileNotFoundError)
async def missing_error(request,error):
    return JSONResponse({'detail':str(error)},404)

@app.get('/api/health')
def health():
    value={'status':'ok','version':__version__}
    if os.environ.get('ATLAS_PORTABLE_ROOT'):
        value['portable_session']=os.environ.get('ATLAS_PORTABLE_SESSION','')
        value['portable_pid']=os.getpid()
    return value

@app.get('/api/portable')
def portable_status():
    from . import portable
    return portable.status()

@app.post('/api/portable/stop')
def portable_stop():
    from . import portable
    if not portable.enabled() or not os.environ.get('ATLAS_PORTABLE_SESSION'):
        raise HTTPException(409,'当前不是便携启动会话')
    with db.connect() as con:
        if con.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone():
            raise HTTPException(409,'请等待当前上传、归档或模型预览任务完成后再停止')
    import signal
    return JSONResponse({'status':'stopping','message':'正在关闭并保存目录库，请等待启动终端显示完成后再弹出磁盘。'},
                        background=BackgroundTask(os.kill,os.getpid(),signal.SIGTERM))

@app.get('/api/auth/status')
def auth_status(request:Request):
    return {'authenticated':request.state.authenticated,'setup_required':request.state.setup,
            'local_bootstrap':request.state.local and request.state.setup and os.environ.get('ASSET_LOCAL_BOOTSTRAP','1')=='1'}

@app.post('/api/auth/setup')
def setup(request:Request,body:dict):
    with core.MUTATION_LOCK:
        if not request.state.local or db.setting('password'):
            raise HTTPException(403,'只能在服务所在主机首次设置密码')
        set_password(body.get('password',''))
    return {'ok':True}

@app.post('/api/auth/login')
def login(request:Request,body:dict):
    addr=request.client.host
    attempts=[t for t in ATTEMPTS.get(addr,[]) if t>time.time()-900]
    if len(attempts)>=15: raise HTTPException(429,'尝试过于频繁，请 15 分钟后再试')
    attempts.append(time.time()); ATTEMPTS[addr]=attempts
    stored=db.setting('password')
    if not stored: raise HTTPException(403,'管理员尚未设置密码')
    p=json.loads(stored)
    if not hmac.compare_digest(password_hash(body.get('password',''),p['salt']),p['hash']):
        raise HTTPException(401,'密码错误')
    ATTEMPTS.pop(addr,None)
    token=secrets.token_urlsafe(48)
    with db.connect() as con:
        con.execute('INSERT INTO settings VALUES(?,?)',('session:'+hashlib.sha256(token.encode()).hexdigest(),str(time.time()+28800)))
        con.execute("DELETE FROM settings WHERE key LIKE 'session:%' AND CAST(value AS REAL) < ?",(time.time(),))
        db.audit(con,'login',{'address':addr})
    response=JSONResponse({'ok':True})
    response.set_cookie(COOKIE_NAME,token,httponly=True,samesite='strict',secure=os.environ.get('ASSET_SECURE_COOKIE','0')=='1',max_age=28800)
    return response

@app.post('/api/auth/logout')
def logout(request:Request):
    token=request.cookies.get(COOKIE_NAME,'')
    with db.connect() as con:
        con.execute('DELETE FROM settings WHERE key=?',('session:'+hashlib.sha256(token.encode()).hexdigest(),))
    r=JSONResponse({'ok':True}); r.delete_cookie(COOKIE_NAME); return r

@app.get('/api/stats')
def stats():
    with db.connect() as con:
        counts=dict(con.execute('SELECT COUNT(*) AS assets,COALESCE(SUM(size),0) AS bytes,SUM(git_state=\'tracked\') AS tracked FROM assets a WHERE present=1 OR EXISTS(SELECT 1 FROM versions v WHERE v.asset_id=a.id)').fetchone())
        counts['projects']=con.execute('SELECT COUNT(*) FROM projects').fetchone()[0]
        counts['archived']=con.execute('SELECT COUNT(DISTINCT asset_id) FROM versions').fetchone()[0]
        counts['blob_bytes']=con.execute('SELECT COALESCE(SUM(size),0) FROM (SELECT sha256,MAX(size) AS size FROM versions GROUP BY sha256)').fetchone()[0]
        counts['kinds']=[dict(r) for r in con.execute('SELECT kind,COUNT(*) AS count,SUM(size) AS bytes FROM assets a WHERE present=1 OR EXISTS(SELECT 1 FROM versions v WHERE v.asset_id=a.id) GROUP BY kind ORDER BY count DESC')]
        counts['pending']=sum(json.loads(r[0]).get('state')!='versioned' for r in con.execute('SELECT git_json FROM projects'))
    return counts

@app.get('/api/projects')
def projects():
    with db.connect() as con:
        rows=con.execute('''SELECT p.*,COUNT(a.id) AS assets,COALESCE(SUM(a.size),0) AS bytes,
            SUM(a.git_state='tracked') AS tracked,MAX(a.mtime) AS last_modified,
            (SELECT COUNT(DISTINCT v.asset_id) FROM versions v JOIN assets aa ON aa.id=v.asset_id WHERE aa.project_id=p.id) AS archived
            FROM projects p LEFT JOIN assets a ON a.project_id=p.id AND (a.present=1 OR EXISTS(SELECT 1 FROM versions v WHERE v.asset_id=a.id)) GROUP BY p.id ORDER BY p.name''').fetchall()
    return [{**dict(r),'git':json.loads(r['git_json']),'excluded':json.loads(r['excluded_json'])} for r in rows]

@app.post('/api/projects/import')
def project_import(body:dict): return {'imported':core.import_projects(body)}

@app.patch('/api/projects/{pid}')
def project_edit(pid:str,body:dict):
    path=str(Path(body['path']).expanduser().resolve())
    if not Path(path).is_dir(): raise ValueError('目标目录不存在')
    with core.MUTATION_LOCK,db.connect() as con:
        if not con.execute('SELECT id FROM projects WHERE id=?',(pid,)).fetchone(): raise ValueError('项目不存在')
        con.execute("UPDATE projects SET path=?,host='local',scan_error=NULL WHERE id=?",(path,pid))
        from . import node
        con.execute('DELETE FROM mirror_files WHERE project_id=?',(pid,))
        con.execute('INSERT OR REPLACE INTO project_locations VALUES(?,?,?,?,?,?)',(pid,db.setting('node_id','local'),'workspace',None,path,core.now()))
        db.audit(con,'remap',{'project':pid,'path':path})
    return {'ok':True,'job_id':core.run_job('scan',core.scan_all,pid)}

@app.post('/api/scan')
def scan(body:dict): return {'job_id':core.run_job('scan',core.scan_all,body.get('project_id'))}

@app.get('/api/assets')
def assets(q:str='',project:str='',kind:str='',status:str='',sort:str='recent',page:int=1,limit:int=60):
    if page<1 or not 1<=limit<=200: raise ValueError('分页参数无效')
    where=['(a.present=1 OR EXISTS(SELECT 1 FROM versions v WHERE v.asset_id=a.id))']; params=[]
    if q:
        where.append('(a.name LIKE ? ESCAPE \'\\\' OR a.relpath LIKE ? ESCAPE \'\\\' OR a.tags LIKE ? ESCAPE \'\\\' OR a.note LIKE ? ESCAPE \'\\\')')
        term='%'+q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'; params += [term]*4
    if project: where.append('a.project_id=?'); params.append(project)
    if kind: where.append('a.kind=?'); params.append(kind)
    if status=='archived': where.append('EXISTS(SELECT 1 FROM versions v WHERE v.asset_id=a.id)')
    elif status=='unarchived': where.append('NOT EXISTS(SELECT 1 FROM versions v WHERE v.asset_id=a.id)')
    elif status=='untracked': where.append("a.git_state IN ('untracked','staged')")
    elif status=='tracked': where.append("a.git_state='tracked'")
    order={'recent':'a.mtime DESC','name':'a.name COLLATE NOCASE','size':'a.size DESC'}.get(sort,'a.mtime DESC')
    clause=' AND '.join(where)
    with db.connect() as con:
        total=con.execute('SELECT COUNT(*) FROM assets a WHERE '+clause,params).fetchone()[0]
        rows=con.execute('''SELECT a.*,p.name AS project_name,
            (SELECT COUNT(*) FROM versions v WHERE v.asset_id=a.id) AS versions,
            (SELECT v.size FROM versions v WHERE v.asset_id=a.id AND reason IN ('upload','update') ORDER BY id DESC LIMIT 1) AS updated_size
            FROM assets a JOIN projects p ON p.id=a.project_id WHERE '''+clause+' ORDER BY '+order+' LIMIT ? OFFSET ?',params+[limit,(page-1)*limit]).fetchall()
    return {'items':[dict(r) for r in rows],'total':total,'page':page,'pages':max(1,(total+limit-1)//limit)}

@app.get('/api/assets/{aid}')
def asset_detail(aid:int):
    a=core.asset_row(aid)
    with db.connect() as con:
        a['versions']=[dict(r) for r in con.execute('SELECT * FROM versions WHERE asset_id=? ORDER BY id DESC',(aid,))]
    return a

@app.patch('/api/assets/{aid}')
def asset_edit(aid:int,body:dict):
    core.asset_row(aid)
    tags=str(body.get('tags',''))[:1000]; note=str(body.get('note',''))[:10000]
    with db.connect() as con:
        con.execute('UPDATE assets SET tags=?,note=? WHERE id=?',(tags,note,aid))
        db.audit(con,'metadata',{'asset_id':aid,'tags':tags})
    return {'ok':True}

@app.get('/api/assets/{aid}/file')
def file(aid:int,version:int|None=None,inline:bool=False):
    path,a=core.source_path(aid,version)
    # Active documents never execute on the application origin.
    safe_inline={'.pdf','.png','.jpg','.jpeg','.gif','.webp','.bmp','.mp4','.webm','.mp3','.wav','.ogg','.flac'}
    allowed=inline and a['ext'] in safe_inline
    media=mimetypes.guess_type(a['name'])[0] if allowed else 'application/octet-stream'
    r=FileResponse(path,filename=a['name'],media_type=media or 'application/octet-stream',content_disposition_type='inline' if allowed else 'attachment')
    r.headers['Content-Security-Policy']="sandbox; default-src 'none'"
    return r

@app.get('/api/assets/{aid}/preview')
def preview(aid:int,version:int|None=None):
    path,a=core.source_path(aid,version); ext=a['ext']
    if ext in core.TEXT_EXTS or ext in {'.csv','.tsv'} or ext=='':
        with path.open('rb') as f: raw=f.read(512*1024)
        if b'\0' in raw[:8192]: return {'type':'unsupported','message':'二进制文件，请下载后使用对应软件打开'}
        content=raw.decode('utf-8',errors='replace')
        if ext in {'.csv','.tsv'}:
            rows=list(__import__('itertools').islice(csv.reader(io.StringIO(content),delimiter='\t' if ext=='.tsv' else ','),201))
            return {'type':'table','rows':[r[:40] for r in rows],'truncated':len(rows)>200 or path.stat().st_size>len(raw)}
        return {'type':'text','text':content,'truncated':path.stat().st_size>len(raw),'format':ext}
    if ext in {'.docx','.pptx','.xlsx','.3mf'}:
        with zipfile.ZipFile(path) as z:
            def xml(name):
                i=z.getinfo(name)
                if i.file_size>8*1024*1024: raise ValueError('文档内容过大，请下载阅览')
                return ET.fromstring(z.read(name))
            if ext=='.docx':
                root=xml('word/document.xml'); ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                text='\n'.join(''.join(n.itertext()) for n in root.findall('.//w:p',ns))
                return {'type':'text','text':text[:512000],'truncated':len(text)>512000,'format':ext}
            if ext=='.pptx':
                names=sorted(n for n in z.namelist() if __import__('re').match(r'ppt/slides/slide\d+\.xml$',n))
                text='\n\n'.join('幻灯片 '+str(i+1)+'\n'+'\n'.join(n.text or '' for n in xml(name).iter() if n.tag.endswith('}t')) for i,name in enumerate(names[:100]))
                return {'type':'text','text':text[:512000],'truncated':len(names)>100,'format':ext}
            if ext=='.xlsx':
                ns={'x':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                strings=[''.join(e.itertext()) for e in xml('xl/sharedStrings.xml').findall('x:si',ns)] if 'xl/sharedStrings.xml' in z.namelist() else []
                names=sorted(n for n in z.namelist() if __import__('re').match(r'xl/worksheets/sheet\d+\.xml$',n))
                if not names: raise ValueError('未找到工作表')
                rows=[]
                for row in xml(names[0]).findall('.//x:row',ns)[:201]:
                    cells=[]
                    for c in row.findall('x:c',ns):
                        col=0
                        for char in __import__('re').match(r'[A-Z]+',c.get('r','A1'))[0]: col=col*26+ord(char)-64
                        if col>40: continue
                        while len(cells)<col: cells.append('')
                        v=c.find('x:v',ns)
                        val=v.text if v is not None else ''.join(c.find('x:is',ns).itertext()) if c.find('x:is',ns) is not None else ''
                        cells[col-1]=strings[int(val)] if c.get('t')=='s' and val else val
                    rows.append(cells)
                return {'type':'table','rows':rows,'truncated':len(rows)>200,'message':'首张工作表，最多 200 行 / 40 列'}
            return {'type':'text','text':'3MF 打印包内容\n\n'+'\n'.join(z.namelist()[:300]),'format':ext,'message':'下载后用 Bambu Studio 等切片软件打开'}
    return {'type':'unsupported','message':'该格式提供元数据和版本记录；请下载后使用对应专业软件打开。'}

@app.post('/api/upload')
def upload(project_id:str=Form(...),asset_id:int|None=Form(None),file:UploadFile=File(...)):
    if file.size and file.size>MAX_UPLOAD: raise HTTPException(413,'文件超过上传大小限制')
    with core.MUTATION_LOCK:
        return core.upload(project_id,file.filename or '',file.file,asset_id)

@app.post('/api/archive')
def archive(body:dict):
    ids=body.get('asset_ids')
    if ids is None:
        with db.connect() as con:
            where="a.present=1 AND NOT EXISTS(SELECT 1 FROM versions v WHERE v.asset_id=a.id)"
            args=[]
            if body.get('project_id'): where+=' AND a.project_id=?'; args.append(body['project_id'])
            if body.get('artifacts_only',False): where+=" AND a.kind<>'code'"
            ids=[r[0] for r in con.execute('SELECT a.id FROM assets a WHERE '+where,args)]
    if not isinstance(ids,list) or not all(isinstance(x,int) for x in ids): raise ValueError('资产列表无效')
    return {'job_id':core.run_job('archive',core.archive_many,ids)}

@app.get('/api/jobs')
def jobs():
    with db.connect() as con:
        return [dict(r) for r in con.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT 20')]

@app.get('/api/audit')
def audit():
    with db.connect() as con:
        return [dict(r) for r in con.execute('SELECT * FROM audits ORDER BY id DESC LIMIT 200')]

@app.post('/api/git/plan')
def git_plan(body:dict):
    with core.MUTATION_LOCK:
        return core.git_plan(body['project_id'],body.get('branch','main'),body.get('name'),body.get('directory'))

@app.get('/api/git/plans/{pid}')
def get_plan(pid:str):
    with db.connect() as con:
        r=con.execute('SELECT * FROM plans WHERE id=?',(pid,)).fetchone()
    if not r: raise HTTPException(404)
    return {**json.loads(r['body']),'id':r['id'],'status':r['status']}

@app.post('/api/git/plans/{pid}/execute')
def execute_plan(pid:str,body:dict):
    return core.execute_plan(pid,body.get('acknowledgement',''))

@app.get('/api/export/catalog')
def export_catalog():
    with db.connect() as con:
        data={table:[dict(r) for r in con.execute('SELECT * FROM '+table)] for table in ['projects','assets','versions','plans','model_settings','model_relations','model_snapshots','repositories','repository_archives','project_repositories','project_directories','project_index_runs','project_index_state','project_relations','project_documents','project_locations','mirror_files','development_sessions','development_turns','development_artifacts','project_preferences']}
    data.update(schema_version=3,exported_at=core.now())
    return JSONResponse(data,headers={'Content-Disposition':'attachment; filename="atlas-catalog.json"'})

@app.get('/api/export/project/{pid}')
def export_project(pid:str):
    with db.connect() as con:
        p=con.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone()
        rows=con.execute('SELECT id,relpath,size FROM assets WHERE project_id=? AND (present=1 OR EXISTS(SELECT 1 FROM versions v WHERE v.asset_id=assets.id))',(pid,)).fetchall()
    if not p: raise HTTPException(404)
    reserve=int(os.environ.get('ASSET_RESERVE_BYTES',str(2*1024**3)))
    free=__import__('shutil').disk_usage(db.DATA).free
    if sum(r['size'] for r in rows)>free-reserve:
        raise ValueError('临时导出空间不足，请改用单文件下载，或将目录数据库放到容量更大的磁盘后重试')
    fd,tmp=tempfile.mkstemp(suffix='.zip',dir=db.DATA/'tmp'); os.close(fd)
    manifest={'project':p['name'],'exported_at':core.now(),'files':[],'errors':[]}
    try:
        with zipfile.ZipFile(tmp,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1,allowZip64=True) as z:
            for a in rows:
                try:
                    path,asset=core.source_path(a['id'])
                    sha=hashlib.sha256()
                    with path.open('rb') as src,z.open('files/'+a['relpath'],'w',force_zip64=True) as dst:
                        for chunk in iter(lambda:src.read(1024*1024),b''):
                            sha.update(chunk); dst.write(chunk)
                    manifest['files'].append({'path':a['relpath'],'sha256':sha.hexdigest()})
                except Exception as e: manifest['errors'].append({'path':a['relpath'],'error':str(e)})
            z.writestr('MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        return FileResponse(tmp,filename=p['name']+'.zip',media_type='application/zip',background=BackgroundTask(os.unlink,tmp))
    except BaseException:
        os.unlink(tmp); raise

@app.get('/api/system')
def system():
    return {'data_dir':str(db.DATA),'blob_dir':str(db.blob_root()),'free_bytes':__import__('shutil').disk_usage(db.blob_root()).free,'version':__version__,'platform':os.sys.platform,
            'excluded_directories':sorted(core.EXCLUDE_DIRS),'max_upload_mb':MAX_UPLOAD//1024//1024,
            'git_plan_max_file_mb':10,'migration_root':db.setting('migration_root',str(Path.home()/'AssetVault') if os.sys.platform=='darwin' else '/srv/asset-vault')}

@app.post('/api/migration/plan')
def migration_plan(body:dict):
    target=Path(body.get('root') or (Path.home()/'AssetVault' if os.sys.platform=='darwin' else '/srv/asset-vault')).expanduser().resolve()
    if target==Path('/') or target==Path.home(): raise ValueError('请选择独立的资产根目录')
    rows=projects(); plans=[]
    for p in rows:
        dest=target/'projects'/p['name']
        if not dest.is_relative_to(target/'projects'): raise ValueError('项目名称不能作为安全的目录名，请先修改名称')
        plans.append({'project':p['name'],'source':p['path'],'target':str(dest),'bytes':p['bytes'],
                      'strategy':'保留主仓库关联，重建 worktree 并校验分支' if '/worktrees/' in p['path'] else '复制后校验 SHA-256 与 Git 历史，再切换索引',
                      'status':'待确认','blocker':'目标目录已存在，需人工合并方案' if dest.exists() else None})
    result={'root':str(target),'directories':['projects','archive','catalog','backups'],'status':'proposal_only','projects':plans,
            'note':'此操作仅生成方案，不复制、删除、移动文件，也不改写 Codex 项目配置。迁移须另行确认后执行。'}
    with db.connect() as con:
        con.execute("INSERT OR REPLACE INTO settings VALUES('migration_root',?)",(str(target),))
        db.audit(con,'migration_plan',result)
    return result

@app.get('/api/model-projects')
def model_projects(q:str='',project:str='',runtime:bool=False):return models.list_projects(q,project,runtime)

@app.get('/api/model-projects/{mid}')
def model_project(mid:str):return models.detail(mid)

@app.patch('/api/model-projects/{mid}')
def model_settings(mid:str,body:dict):return models.save_settings(mid,body)

@app.post('/api/model-projects/{mid}/relations')
def model_relation(mid:str,body:dict):return models.add_relation(mid,body)

@app.post('/api/model-projects/{mid}/snapshots')
def model_snapshot(mid:str):return {'job_id':core.run_job('model_snapshot',models.snapshot,mid)}

@app.post('/api/model-projects/{mid}/upload-plan')
def engineering_upload_plan(mid:str,file:UploadFile=File(...)):
    if file.size and file.size>MAX_UPLOAD:raise HTTPException(413,'工程包超过上传大小限制')
    with core.MUTATION_LOCK:return engineering_upload.stage(mid,file.file)

@app.post('/api/model-projects/{mid}/upload-apply')
def engineering_upload_apply(mid:str,body:dict):
    with core.MUTATION_LOCK:return engineering_upload.apply(mid,str(body.get('plan_id','')))

@app.get('/api/model-projects/{mid}/bundle')
def model_bundle(mid:str,snapshot:str|None=None):
    path,title=models.bundle(mid,snapshot)
    return FileResponse(path,filename=title+'-工程包.zip',media_type='application/zip',background=BackgroundTask(os.unlink,path))

@app.get('/api/assets/{aid}/model-context')
def model_context(aid:int,version:int|None=None):return models.asset_context(aid,version)

@app.get('/api/assets/{aid}/model-preview')
def model_preview_status(aid:int,version:int|None=None):return model_preview.state(aid,version)

@app.post('/api/assets/{aid}/model-preview')
def model_preview_start(aid:int,body:dict):
    version=body.get('version');status=model_preview.state(aid,version)
    if status['status']=='ready':return status
    if status['status']=='unavailable':raise ValueError(status['message'])
    return {'job_id':core.run_job('model_preview',model_preview.convert,aid,version)}

@app.get('/api/model-previews/{key}/file')
def model_preview_download(key:str):return FileResponse(model_preview.preview_file(key),media_type='model/gltf-binary')

app.include_router(engineering_api.router)
app.mount('/static',StaticFiles(directory=STATIC),name='static')

@app.get('/')
def index(): return FileResponse(STATIC/'index.html')

from .steward_api import router as steward_router
app.include_router(steward_router)

from .vault_api import router as vault_router
app.include_router(vault_router)
