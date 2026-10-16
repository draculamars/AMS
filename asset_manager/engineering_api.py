"""HTTP interface for engineering structure and read-only historical browsing."""
import json
import mimetypes
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import FileResponse
from . import core, db, project_index as index, git_history

router=APIRouter()

@router.get('/api/engineering')
def projects():return index.list_projects()

@router.get('/api/engineering/{pid}')
def overview(pid:str):return index.overview(pid)

@router.get('/api/engineering/{pid}/tree')
def tree(pid:str,path:str='',page:int=1,limit:int=150,q:str='',kind:str='',run:str='',scope:str='current'):
    return index.tree(pid,path,page,limit,q,kind,run,scope)

@router.get('/api/engineering/{pid}/documents')
def documents(pid:str,q:str='',page:int=1,limit:int=40):return index.documents(pid,q,page,limit)

@router.get('/api/engineering/{pid}/relations')
def relations(pid:str,path:str='',page:int=1,limit:int=100,status:str=''):return index.relations(pid,path,page,limit,status)

@router.post('/api/engineering/{pid}/relations')
def add_relation(pid:str,body:dict):return index.add_relation(pid,body)

@router.get('/api/engineering/{pid}/graph')
def graph(pid:str,path:str=''):return index.graph(pid,path)

@router.get('/api/engineering/{pid}/timeline')
def timeline(pid:str,page:int=1,limit:int=60):return index.timeline(pid,page,limit)

@router.get('/api/engineering/{pid}/file')
def file_detail(pid:str,path:str):return index.file_detail(pid,path)

@router.get('/api/engineering/{pid}/runs/{run_id}')
def run_detail(pid:str,run_id:str):
    r=index.read_run(run_id)
    if r['project_id']!=pid:raise ValueError('索引版本与项目不匹配')
    return {k:v for k,v in r.items() if k!='files'}

@router.post('/api/engineering-index')
def reindex(body:dict):return {'job_id':core.run_job('project_index',index.index_all,body.get('project_id'))}

@router.post('/api/engineering-sync')
def sync(body:dict):
    from . import project_sync
    if not db.setting('sync_target'):raise ValueError('请在 来源节点配置已批准的 SSH 归档目标')
    return {'job_id':core.run_job('project_sync',project_sync.synchronize,body.get('project_id'))}

@router.get('/api/repositories/{rid}/commits')
def commits(rid:str,ref:str='',page:int=1,limit:int=40,path:str=''):return git_history.commits(rid,ref,page,limit,path)

@router.get('/api/repositories/{rid}/tree')
def git_tree(rid:str,ref:str='',path:str='',page:int=1,limit:int=100):return git_history.tree(rid,ref,path,page,limit)

@router.get('/api/repositories/{rid}/changes')
def changes(rid:str,ref:str,base:str='',path:str=''):return git_history.changes(rid,ref,base,path)

@router.get('/api/repositories/{rid}/preview')
def preview(rid:str,ref:str,path:str):
    raw,a,commit=git_history.blob_bytes(rid,ref,path)
    if raw.startswith(b'version https://git-lfs.github.com/spec/v1'):
        return {'type':'unsupported','asset':a,'commit':commit,'message':'此提交保存的是 Git LFS 指针；实际大文件需对应 LFS 对象，不能将指针当作模型内容。'}
    if b'\0' in raw[:8192]:return {'type':'unsupported','asset':a,'commit':commit,'message':'二进制内容可下载，模型可切换到三维预览。'}
    return {'type':'text','text':raw.decode('utf-8',errors='replace'),'asset':a,'commit':commit}

@router.get('/api/repositories/{rid}/file')
def git_file(rid:str,ref:str,path:str,inline:bool=False):
    source,a=git_history.file_path(rid,ref,path)
    safe=inline and a['ext'] in {'.png','.jpg','.jpeg','.webp','.gif','.pdf','.mp4','.webm','.mp3','.wav'}
    return FileResponse(source,filename=a['name'],media_type=(mimetypes.guess_type(a['name'])[0] if safe else 'application/octet-stream'),content_disposition_type='inline' if safe else 'attachment')

@router.get('/api/repositories/{rid}/model-context')
def model_context(rid:str,ref:str,path:str):return git_history.model_context(rid,ref,path)

@router.get('/api/repositories/{rid}/asset')
def git_asset(rid:str,ref:str,path:str):
    repo,commit,asset=git_history.blob_info(rid,ref,path)
    return {**asset,'commit':commit}

@router.get('/api/repositories/{rid}/archives')
def history_archives(rid:str):
    with db.connect() as c:
        return [dict(r) for r in c.execute('SELECT fingerprint,created_at FROM repository_archives WHERE repository_id=? ORDER BY created_at DESC',(rid,))]

@router.get('/api/project-connections')
def connections():return index.project_connections()

@router.get('/api/engineering/{pid}/runs')
def index_runs(pid:str,page:int=1,limit:int=200):
    if page<1 or not 1<=limit<=200:raise ValueError('分页参数无效')
    with db.connect() as c:
        total=c.execute('SELECT COUNT(*) FROM project_index_runs WHERE project_id=?',(pid,)).fetchone()[0]
        rows=[{**dict(r),'summary':json.loads(r['body']),'type':'index'} for r in c.execute('SELECT id,created_at,body FROM project_index_runs WHERE project_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?',(pid,limit,(page-1)*limit))]
    for r in rows:r.pop('body')
    return {'items':rows,'total':total,'page':page}

@router.get('/api/repositories/{rid}/bundle')
def history_bundle(rid:str):
    from starlette.background import BackgroundTask
    path,name=git_history.bundle(rid)
    return FileResponse(path,filename=name,media_type='application/zip',background=BackgroundTask(path.unlink,missing_ok=True))
