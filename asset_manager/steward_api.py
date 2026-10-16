"""Task-oriented overview, provenance and explicit node-local editing."""
import json
from pathlib import Path
from fastapi import APIRouter
from . import core, db, node, provenance, source_edit

router=APIRouter()

@router.get('/api/node')
def status():return node.revision()

@router.get('/api/workbench')
def workbench():
    with db.connect() as c:
        projects=[dict(r) for r in c.execute('SELECT p.id,p.name,p.path,p.scan_at,p.scan_error,p.git_json,COALESCE(f.favorite,0) favorite,COALESCE(f.note,\'\') note,l.mode,l.refreshed_at,l.source_node FROM projects p LEFT JOIN project_preferences f ON f.project_id=p.id LEFT JOIN project_locations l ON l.project_id=p.id ORDER BY favorite DESC,p.name')]
        counts={r['project_id']:dict(r) for r in c.execute('SELECT project_id,COUNT(*) files,SUM(CASE WHEN '+node.current_sql()+' THEN 1 ELSE 0 END) current_files,SUM(size) bytes,SUM(kind=\'model\') models,SUM(kind=\'document\') documents FROM assets a GROUP BY project_id')}
        history={r[0]:r[1] for r in c.execute('SELECT project_id,COUNT(*) FROM development_turns GROUP BY project_id')}
        states={r['project_id']:{**json.loads(r['body']),'indexed_at':r['indexed_at']} for r in c.execute('SELECT * FROM project_index_state')}
        jobs=[dict(r) for r in c.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT 5')]
        recent=[dict(r) for r in c.execute('SELECT a.id,a.name,a.relpath,a.project_id,p.name project_name,a.kind,a.mtime FROM assets a JOIN projects p ON p.id=a.project_id WHERE '+node.current_sql()+' ORDER BY a.mtime DESC LIMIT 8')]
    issues=[]
    for p in projects:
        p['counts']=counts.get(p['id'],{});p['turns']=history.get(p['id'],0);p['index']=states.get(p['id']);p['git']=json.loads(p.pop('git_json'))
        if p['scan_error'] and p['mode']!='archive':issues.append({'project_id':p['id'],'title':p['name'],'message':p['scan_error'],'tab':'coverage'})
        if p['counts'].get('current_files') and not p['index']:issues.append({'project_id':p['id'],'title':p['name'],'message':'需要首次结构索引','tab':'coverage'})
        if p['counts'].get('current_files') and not p['turns']:issues.append({'project_id':p['id'],'title':p['name'],'message':'暂无已关联开发记录，可检查会话来源与项目路径','tab':'development'})
    return {'node':node.revision(),'projects':projects,'issues':issues,'jobs':jobs,'recent':recent,
            'sync_configured':bool(db.setting('sync_target')),'development':provenance.listing(limit=5)}

@router.post('/api/project-preferences/{pid}')
def preferences(pid:str,body:dict):
    with db.connect() as c:
        if not c.execute('SELECT 1 FROM projects WHERE id=?',(pid,)).fetchone():raise ValueError('项目不存在')
        c.execute('INSERT INTO project_preferences VALUES(?,?,?) ON CONFLICT(project_id) DO UPDATE SET favorite=excluded.favorite,note=excluded.note',(pid,int(bool(body.get('favorite'))),str(body.get('note',''))[:2000]))
        db.audit(c,'project_preference',{'project_id':pid})
    return {'saved':True}

@router.get('/api/knowledge-search')
def search(q:str,limit:int=20):
    if len(q.strip())<2:return {'items':[]}
    if not 1<=limit<=50:raise ValueError('分页参数无效')
    pattern='%'+q[:200]+'%'
    with db.connect() as c:
        projects=[{'type':'project','id':r['id'],'project_id':r['id'],'title':r['name'],'excerpt':r['path']} for r in c.execute('SELECT id,name,path FROM projects WHERE name LIKE ? LIMIT ?',(pattern,limit))]
        files=[{'type':'file','id':r['id'],'project_id':r['project_id'],'title':r['relpath'],'excerpt':r['name']} for r in c.execute('SELECT a.id,a.project_id,a.relpath,p.name FROM assets a JOIN projects p ON p.id=a.project_id WHERE (a.relpath LIKE ? OR a.note LIKE ? OR a.tags LIKE ?) AND '+node.current_sql()+' LIMIT ?',(pattern,pattern,pattern,limit))]
        docs=[{'type':'document','project_id':r['project_id'],'title':r['path'],'excerpt':r['body'][:160]} for r in c.execute('SELECT project_id,path,body FROM project_documents WHERE body LIKE ? LIMIT ?',(pattern,limit))]
    turns=provenance.listing(q=q,limit=min(limit,10))['items']
    from . import knowledge
    entries=[{'type':'knowledge','id':r['id'],'title':r['name'],'excerpt':r['path']} for r in knowledge.listing(q=q)['items'][:limit]]
    return {'items':(projects+files+docs+entries+[{'type':'development','id':r['id'],'project_id':r['project_id'],'title':r['title'],'excerpt':r['body']['request'][:160]} for r in turns])[:limit*3], 'note':'每类结果有限额；开发记录与文档正文仅在已索引范围内搜索。'}

@router.get('/api/development')
def development(project:str='',q:str='',page:int=1,limit:int=30,path:str=''):
    return provenance.listing(project or None,q,page,limit,path)

@router.post('/api/development-index')
def development_index():return {'job_id':core.run_job('development_index',provenance.ingest)}

@router.get('/api/development-graph/{pid}')
def development_graph(pid:str,path:str=''):
    rows=provenance.listing(pid,limit=60,path=path)['items'];nodes={};edges=[];previous={}
    for turn in reversed(rows):
        t=turn['id'];nodes[t]={'id':t,'type':'turn','label':(turn['body']['request'] or turn['title'])[:70],'date':turn['started_at']}
        for a in turn['artifacts']:
            if a['project_id']!=pid:continue
            key=a['id'];nodes[key]={'id':key,'type':'artifact','path':a['path'],'label':a['path'],'turn_id':t,'action':a['action']}
            edges.append({'source':t,'target':key,'kind':'mentioned' if a['action']=='mentioned' else 'changed' if a['body']['status']=='completed' else 'attempted','evidence':a['body']['evidence']})
            if a['body']['status']=='completed':
                if a['path'] in previous:edges.append({'source':previous[a['path']],'target':key,'kind':'iteration','evidence':'同一路径的先后修改事件，不表示内容派生'})
                previous[a['path']]=key
    return {'nodes':list(nodes.values()),'edges':edges,'turns':len(rows),'limit':60,'note':'实线：记录中的文件变更；虚线：同一路径迭代顺序。设计依赖见内容引用图，不能由时间顺序推断。'}

@router.get('/api/assets/{aid}/source-edit')
def edit_read(aid:int):return source_edit.read(aid)

@router.post('/api/assets/{aid}/source-edit/plan')
def edit_plan(aid:int,body:dict):return source_edit.plan(aid,body)

@router.post('/api/assets/{aid}/source-edit/apply')
def edit_apply(aid:int,body:dict):return source_edit.apply(aid,body)

@router.post('/api/node/configure')
def configure(body:dict):
    allowed={'label','codex_source_root'}
    if not set(body)<=allowed:raise ValueError('节点配置字段无效')
    with db.connect() as c:
        if 'label' in body:
            label=str(body['label']).strip()
            if not label or len(label)>100:raise ValueError('节点名称长度无效')
            c.execute("INSERT OR REPLACE INTO settings VALUES('node_label',?)",(label,))
        if 'codex_source_root' in body:
            root=str(body['codex_source_root']).strip()
            if root and not Path(root).expanduser().is_absolute():raise ValueError('会话来源必须是绝对路径')
            c.execute("INSERT OR REPLACE INTO settings VALUES('codex_source_root',?)",(root,))
        db.audit(c,'node_configuration',{'fields':list(body)})
    return node.revision()
