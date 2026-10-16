"""Engineering packages preserve paths, references, provenance and pinned revisions."""
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import struct
import tempfile
import threading
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from . import core, db

GEOMETRY = {'.stl','.obj','.step','.stp','.iges','.igs','.brep','.3mf','.blend','.glb','.gltf','.usd','.usda','.usdc','.usdz','.urdf','.xacro','.sdf','.fbx','.dae','.fcstd','.sldasm','.sldprt','.f3d','.f3z','.iam','.ipt','.scad'}
VIEWABLE = {'.stl','.obj','.step','.stp','.iges','.igs','.brep','.3mf','.glb','.gltf','.urdf','.blend'}
NATIVE = {'.blend','.fcstd','.sldasm','.sldprt','.f3d','.f3z','.iam','.ipt','.scad'}
RESOURCE = {'.png','.jpg','.jpeg','.webp','.tga','.exr','.hdr','.mtl','.bin'}
PARSABLE = {'.urdf','.xacro','.sdf','.obj','.mtl','.gltf','.glb','.usd','.usda','.py','.json'}
_cache = {}; _lock = threading.RLock()

def available_sql(alias='a'):
    return f"({alias}.present=1 OR EXISTS(SELECT 1 FROM versions v WHERE v.asset_id={alias}.id))"

def infer_root(path):
    """Directory-derived grouping is explicitly a proposal, never a file move."""
    parts=PurePosixPath(path).parts
    if 'TeleoperationModels' in parts:
        i=parts.index('TeleoperationModels');return '/'.join(parts[:min(i+2,len(parts)-1)])
    for i,p in enumerate(parts[:-1]):
        if p.endswith('_description'): return '/'.join(parts[:i+1])
    if len(parts)>2 and parts[0] in {'artifacts','outputs','builds'}:return '/'.join(parts[:2])
    if len(parts)>2 and parts[0]=='mechanical':
        n=3 if len(parts)>3 and re.fullmatch(r'(r|v)\d[\w.-]*',parts[2],re.I) else 2
        return '/'.join(parts[:n])
    for token in ['urdf','meshes','cad','stl','printing','deliverables','exports','preview']:
        if token in parts[:-1]:
            i=parts.index(token)
            if i:return '/'.join(parts[:i])
    return '/'.join(parts[:-1])

def role(a):
    parts=PurePosixPath(a['relpath']).parts;ext=a['ext']
    if ext in NATIVE:return '原生工程'
    if ext in {'.urdf','.xacro','.sdf','.usd','.usda','.usdc'}:return '装配与场景'
    if ext=='.3mf' or any(x in parts for x in ['printing','slicing']):return '制造与打印'
    if ext in {'.step','.stp','.iges','.igs','.brep'}:return 'CAD 几何'
    if ext in {'.py','.scad','.sh','.js'}:return '生成脚本'
    if ext in {'.json','.yaml','.yml','.toml','.csv'}:return '参数与清单'
    if ext in RESOURCE:return '材质与资源'
    if ext in GEOMETRY:return '网格与交换'
    if a.get('kind')=='document':return '说明与记录'
    return '配套文件'

def entry_score(a):
    weights={'.urdf':100,'.blend':95,'.sldasm':90,'.fcstd':85,'.glb':80,'.gltf':78,'.step':75,'.stp':75,'.3mf':65,'.obj':45,'.stl':40}
    score=weights.get(a['ext'],0)-len(PurePosixPath(a['relpath']).parts)
    low=a['relpath'].lower()
    if any(x in low for x in ['assembly','assemblies','complete','nominal']):score+=12
    if any(x in low for x in ['/parts/','/printing/','/references/','/review/','_print.','_sliced.','.gcode.','support_blocker','debug_']):score-=35
    return score

def catalog(refresh=False):
    key=str(db.DATA)
    with _lock:
        cached=_cache.get(key)
        if cached and not refresh and time.monotonic()-cached[0]<20:return cached[1]
        with db.connect() as c:
            marks=','.join('?' for _ in GEOMETRY)
            rows=c.execute(f'SELECT a.*,p.name project_name FROM assets a JOIN projects p ON p.id=a.project_id WHERE a.ext IN ({marks}) AND '+available_sql(),tuple(GEOMETRY)).fetchall()
            settings={r['id']:dict(r) for r in c.execute('SELECT * FROM model_settings')}
        groups={}
        for r in rows:
            a=dict(r);root=infer_root(a['relpath']);mid=hashlib.sha256((a['project_id']+'\0'+root).encode()).hexdigest()[:24]
            if mid not in groups:
                runtime=any(x.endswith('.app') for x in PurePosixPath(root).parts) or '.benchmark' in root or '/products/' in root
                groups[mid]={'id':mid,'project_id':a['project_id'],'project_name':a['project_name'],'root':root,'title':PurePosixPath(root).name or a['project_name'],'models':[],'runtime':runtime,'basis':'按原始目录识别，保留完整相对路径'}
            groups[mid]['models'].append(a)
        for g in groups.values():
            g['runtime']=all(any(p.endswith('.app') for p in PurePosixPath(a['relpath']).parts) or '.benchmark' in a['relpath'] or '/products/' in a['relpath'] for a in g['models'])
            s=settings.get(g['id'],{});g['title']=s.get('title') or g['title'];g['note']=s.get('note') or ''
            ordered=sorted(g['models'],key=lambda a:(entry_score(a),a['mtime'] or 0),reverse=True)
            g['entry']=next((a for a in ordered if a['id']==s.get('entry_asset_id')),ordered[0])
            g['formats']=sorted({a['ext'][1:].upper() for a in ordered});g['model_count']=len(ordered);g['bytes']=sum(a['size'] for a in ordered);g['modified']=max(a['mtime'] or 0 for a in ordered)
        _cache[key]=(time.monotonic(),groups);return groups

def list_projects(q='',project='',runtime=False):
    groups=catalog().values();total=len(groups);runtime_count=sum(g['runtime'] for g in groups)
    items=[{k:v for k,v in g.items() if k!='models'} for g in groups if (runtime or not g['runtime']) and (not project or g['project_id']==project) and (not q or q.lower() in (g['title']+' '+g['root']+' '+g['project_name']).lower())]
    items.sort(key=lambda g:g['modified'],reverse=True)
    return {'items':items,'total':total,'runtime_count':runtime_count}

def group(mid):
    g=catalog().get(mid)
    if not g:raise ValueError('模型工程不存在，请刷新工程目录')
    return g

def scoped_assets(g):
    prefix=g['root']+'/' if g['root'] else ''
    with db.connect() as c:
        return [dict(r) for r in c.execute('SELECT a.* FROM assets a WHERE project_id=? AND substr(relpath,1,?)=? AND '+available_sql()+' ORDER BY relpath',(g['project_id'],len(prefix),prefix))]

def read_small(a,maximum=16*1024*1024,version=None):
    path,_=core.source_path(a['id'],version)
    if path.stat().st_size>maximum:raise ValueError('依赖描述超过解析大小限制')
    return path.read_bytes()

def references(a,version=None):
    ext=a['ext']
    if ext not in PARSABLE:return []
    if ext=='.glb':
        path,_=core.source_path(a['id'],version)
        with path.open('rb') as f:
            head=f.read(20)
            if len(head)!=20 or head[:4]!=b'glTF':raise ValueError('GLB 文件头无效')
            length,kind=struct.unpack_from('<II',head,12)
            if kind!=0x4e4f534a or length>16*1024*1024:raise ValueError('GLB JSON 块无效或过大')
            data=f.read(length)
    else:data=read_small(a,version=version)
    refs=[]
    if ext=='.py':
        from .engineering_refs import script_references
        try:refs,dynamic=script_references(a,data.decode('utf-8'))
        except (SyntaxError,UnicodeError):raise ValueError('脚本无法静态分析；原文件保留，不执行工程脚本')
        if dynamic:a['_analysis_note']=f'{dynamic} 处动态文件路径未执行，需结合原生工程确认'
    elif ext=='.json':
        doc=json.loads(data)
        if isinstance(doc,dict) and isinstance(doc.get('files'),dict):
            root=infer_root(a['relpath'])
            refs=[('project://'+posixpath.join(root,key),'manifest_entry') for key,val in doc['files'].items() if isinstance(key,str) and isinstance(val,str) and re.fullmatch('[a-fA-F0-9]{64}',val)]
    elif ext in {'.urdf','.xacro','.sdf'}:
        if b'<!DOCTYPE' in data or b'<!ENTITY' in data:raise ValueError('工程 XML 不接受实体声明')
        root=ET.fromstring(data)
        for e in root.iter():
            tag=e.tag.split('}')[-1]
            if tag in {'mesh','texture','include'}:
                uri=e.get('filename') or e.get('url') or e.findtext('uri')
                if uri:refs.append((uri,tag))
    elif ext=='.obj':
        for line in data.decode(errors='replace').splitlines():
            if line.strip().startswith('mtllib '):refs.append((line.strip()[7:].strip(),'material'))
    elif ext=='.mtl':
        for line in data.decode(errors='replace').splitlines():
            parts=shlex.split(line,comments=True)
            if parts and (parts[0].lower().startswith('map_') or parts[0].lower() in {'bump','disp','decal','norm'}):
                # Most paths are a single token; quoted names retain spaces.
                if len(parts)>1:refs.append((parts[-1],'texture'))
    elif ext in {'.gltf','.glb'}:
        doc=json.loads(data)
        for item in doc.get('buffers',[])+doc.get('images',[]):
            if item.get('uri') and not item['uri'].startswith('data:'):refs.append((item['uri'],'resource'))
    elif data[:8]==b'PXR-USDC':raise ValueError('USD 二进制层的内部引用尚需原生工具核对')
    else:refs=[(r,'reference') for r in re.findall(r'@([^@\r\n]+)@',data.decode(errors='replace'))]
    return refs

def resolve_reference(project_id,source,ref):
    ref=unquote(ref).replace('\\','/')
    if not ref or '\0' in ref:return None,'invalid'
    if '${' in ref or '$(' in ref:return None,'parameterized'
    if ref.startswith(('http:','https:','ftp:','file:')) or (ref.startswith('/') and not ref.startswith('//')):return None,'external'
    with db.connect() as c:
        def exact(path):
            norm=posixpath.normpath(path)
            if norm=='..' or norm.startswith('../') or norm.startswith('/') or any(x in core.EXCLUDE_DIRS for x in PurePosixPath(norm).parts) or core.secret(norm):return None
            r=c.execute('SELECT a.* FROM assets a WHERE project_id=? AND relpath=? AND '+available_sql(),(project_id,norm)).fetchone()
            return dict(r) if r else None
        if ref.startswith('project://'):
            hit=exact(ref[len('project://'):]);return hit,'static-path' if hit else 'missing'
        if ref.startswith(('package://','model://')):
            pkg,_,tail=ref.split('://',1)[1].partition('/')
            # Search ancestor package contexts first, keeping repeated build copies separate.
            for ancestor in PurePosixPath(source).parents:
                for candidate in [str(ancestor/pkg/tail),str(ancestor/tail) if ancestor.name==pkg or ancestor.name not in {'urdf','meshes'} else '']:
                    hit=exact(candidate) if candidate else None
                    if hit:return hit,'package-relative'
            suffix='/'+pkg+'/'+tail
            hits=c.execute('SELECT a.* FROM assets a WHERE project_id=? AND substr(relpath,-?)=? AND '+available_sql()+' LIMIT 2',(project_id,len(suffix),suffix)).fetchall()
            return (dict(hits[0]),'package-path') if len(hits)==1 else (None,'ambiguous' if hits else 'missing')
        if urlsplit(ref).scheme:return None,'external'
        hit=exact(posixpath.join(posixpath.dirname(source),ref[2:] if ref.startswith('//') else ref))
        return hit,'relative' if hit else 'missing'

def detail(mid):
    g=group(mid);files=scoped_assets(g);byid={a['id']:a for a in files};links=[];coverage=[];queue=list(files);seen=set()
    with db.connect() as c:manual=[dict(r) for r in c.execute('SELECT * FROM model_relations WHERE model_id=?',(mid,))]
    for r in manual:
        a=core.asset_row(r['target_id'])
        if a['project_id']!=g['project_id']:continue
        byid[a['id']]=a;queue.append(a);links.append({'source':r['source_id'],'target':r['target_id'],'reference':a['relpath'],'relation':r['relation'],'status':'confirmed','evidence':'用户确认'})
    while queue:
        a=queue.pop(0)
        if a['id'] in seen:continue
        seen.add(a['id'])
        if a['ext'] in PARSABLE:
            try:
                for ref,relation in references(a):
                    target,method=resolve_reference(g['project_id'],a['relpath'],ref)
                    links.append({'source':a['id'],'target':target['id'] if target else None,'reference':ref,'relation':relation,'status':'resolved' if target else method,'evidence':method})
                    if target and target['id'] not in byid:byid[target['id']]=target;queue.append(target)
                if a.get('_analysis_note'):coverage.append({'asset_id':a['id'],'message':a['_analysis_note']})
            except (OSError,ValueError,ET.ParseError) as e:coverage.append({'asset_id':a['id'],'message':str(e)[:300]})
        elif a['ext'] in NATIVE|{'.usdc','.fbx','.dae'}:
            analyzed=False
            if a['ext']=='.blend':
                from . import model_preview
                try:preview=model_preview.state(a['id'])
                except (ValueError,OSError):preview={'status':'unavailable'}
                if preview['status']=='ready':
                    analyzed=True
                    for dep in preview['metadata'].get('dependencies',[]):
                        target,method=resolve_reference(g['project_id'],a['relpath'],dep['reference'])
                        links.append({'source':a['id'],'target':target['id'] if target else None,'reference':dep['reference'],'relation':'native_reference','status':'resolved' if target else method,'evidence':'Blender 场景解析'})
                        if target and target['id'] not in byid:byid[target['id']]=target;queue.append(target)
            if not analyzed:coverage.append({'asset_id':a['id'],'message':'原生工程完整保存；内部引用需由对应格式解析器或人工确认'})
        if len(byid)>25000:raise ValueError('工程文件数量超过 25,000，请缩小工程目录')
    files=sorted(byid.values(),key=lambda a:a['relpath'])
    for a in files:a['role']=role(a);a['renderable']=a['ext'] in VIEWABLE
    # Same-name exports are suggestions, never asserted as authoritative dependencies.
    names={}
    for a in files:
        if a['ext'] in GEOMETRY:
            key=re.sub(r'_(assembly|print)$','',PurePosixPath(a['name']).stem.lower());names.setdefault(key,[]).append(a)
    associations=[]
    for matches in names.values():
        if len(matches)>1 and len({a['ext'] for a in matches})>1:
            base=max(matches,key=entry_score)
            associations.extend({'source':base['id'],'target':a['id'],'relation':'同名导出候选','status':'inferred'} for a in matches if a['id']!=base['id'])
    with db.connect() as c:snapshots=[{'id':r['id'],'created_at':r['created_at'],'files':len(json.loads(r['body'])['files'])} for r in c.execute('SELECT * FROM model_snapshots WHERE model_id=? ORDER BY created_at DESC',(mid,))]
    related={}
    for a in files:
        other_root=infer_root(a['relpath'])
        if a['ext'] in GEOMETRY and other_root!=g['root']:
            other_id=hashlib.sha256((a['project_id']+'\0'+other_root).encode()).hexdigest()[:24]
            if other_id in catalog():related[other_id]={'id':other_id,'title':catalog()[other_id]['title'],'root':other_root}
    return {**{k:v for k,v in g.items() if k!='models'},'files':files,'links':links,'associations':associations,'coverage':coverage,'snapshots':snapshots,'related_projects':list(related.values()),'missing':sum(not r['target'] for r in links),'bytes':sum(a['size'] for a in files)}

def save_settings(mid,body):
    g=group(mid);entry=int(body.get('entry_asset_id') or g['entry']['id'])
    if entry not in {a['id'] for a in scoped_assets(g)}:raise ValueError('入口文件必须位于当前工程目录')
    title=str(body.get('title') or g['title']).strip()[:200]
    with db.connect() as c:
        c.execute('INSERT OR REPLACE INTO model_settings VALUES(?,?,?,?)',(mid,title,entry,str(body.get('note',''))[:4000]));db.audit(c,'model_settings',{'id':mid,'title':title,'entry':entry})
    catalog(True);return {'ok':True}

def add_relation(mid,body):
    g=group(mid);source=core.asset_row(int(body['source_id']));target=core.asset_row(int(body['target_id']))
    if source['project_id']!=g['project_id'] or target['project_id']!=g['project_id']:raise ValueError('关联文件必须属于同一个 Codex 项目')
    if source['id'] not in {a['id'] for a in scoped_assets(g)}:raise ValueError('关系起点必须位于当前工程')
    relation=body.get('relation','depends_on')
    if relation not in {'depends_on','derived_from','manufacturing_input'}:raise ValueError('关系类型无效')
    with db.connect() as c:
        c.execute('INSERT OR IGNORE INTO model_relations VALUES(?,?,?,?)',(mid,source['id'],target['id'],relation));db.audit(c,'model_relation',body)
    return {'ok':True}

def snapshot(progress,mid):
    d=detail(mid);files=[];before=[];total=len(d['files']);core.storage_preflight(sum(a['size'] for a in d['files']))
    for i,a in enumerate(d['files']):
        path,_=core.source_path(a['id']);st=path.stat();before.append((a['id'],str(path),st.st_size,st.st_mtime_ns))
        snap=core.archive_asset(a['id'])
        with db.connect() as c:v=c.execute("SELECT id FROM versions WHERE asset_id=? AND sha256=? AND reason='snapshot'",(a['id'],snap['sha256'])).fetchone()[0]
        files.append({'id':a['id'],'path':a['relpath'],'role':a['role'],'version_id':v,**snap});progress(i+1,total,a['name'])
    for aid,oldpath,size,mtime in before:
        path,_=core.source_path(aid);st=path.stat()
        if (str(path),st.st_size,st.st_mtime_ns)!=(oldpath,size,mtime):raise ValueError('生成快照时工程发生变化，请重试；不会发布混合版本的工程清单')
    sid=uuid.uuid4().hex
    body={'schema':1,'id':sid,'model_id':mid,'title':d['title'],'project_id':d['project_id'],'entry_asset_id':d['entry']['id'],'root':d['root'],'created_at':core.now(),'files':files,'links':d['links'],'associations':d['associations'],'coverage':d['coverage'],'missing':d['missing']}
    with db.connect() as c:c.execute('INSERT INTO model_snapshots(id,model_id,body) VALUES(?,?,?)',(sid,mid,json.dumps(body,ensure_ascii=False)));db.audit(c,'model_snapshot',{'id':sid,'model_id':mid,'files':len(files),'missing':d['missing']})
    return {'snapshot_id':sid,'files':len(files),'missing':d['missing']}

def get_snapshot(mid,sid):
    with db.connect() as c:r=c.execute('SELECT body FROM model_snapshots WHERE id=? AND model_id=?',(sid,mid)).fetchone()
    if not r:raise ValueError('工程快照不存在')
    return json.loads(r[0])

def bundle(mid,sid=None):
    if sid:
        manifest=get_snapshot(mid,sid);rows=manifest['files']
    else:
        d=detail(mid);manifest={k:d[k] for k in ['id','title','project_id','root','links','associations','coverage','missing']};manifest.update(schema=1,entry_asset_id=d['entry']['id'],created_at=core.now(),files=[])
        rows=[{'id':a['id'],'path':a['relpath'],'size':a['size'],'role':a['role']} for a in d['files']]
    if sum(a['size'] for a in rows)>shutil.disk_usage(db.DATA).free-int(os.environ.get('ASSET_RESERVE_BYTES',str(2*1024**3))):raise ValueError('临时工程包空间不足')
    fd,tmp=tempfile.mkstemp(suffix='.zip',dir=db.DATA/'tmp');os.close(fd)
    try:
        with zipfile.ZipFile(tmp,'w',zipfile.ZIP_DEFLATED,compresslevel=1,allowZip64=True) as z:
            for a in rows:
                rel=PurePosixPath(a['path'])
                if rel.is_absolute() or '..' in rel.parts or '\\' in a['path']:raise ValueError('工程清单路径无效')
                path,_=core.source_path(a['id'],a.get('version_id'));h=hashlib.sha256();before=path.stat()
                with path.open('rb') as src,z.open('project/'+str(rel),'w',force_zip64=True) as dst:
                    for chunk in iter(lambda:src.read(1024*1024),b''):h.update(chunk);dst.write(chunk)
                after=path.stat()
                if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or (a.get('sha256') and a['sha256']!=h.hexdigest()):raise ValueError('工程文件变化或校验失败，请重试')
                if not sid:manifest['files'].append({**a,'sha256':h.hexdigest()})
            z.writestr('ATLAS-ENGINEERING-MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        return tmp,manifest['title']
    except BaseException:
        os.unlink(tmp);raise

def asset_context(aid,version=None):
    a=core.asset_row(aid);mid=hashlib.sha256((a['project_id']+'\0'+infer_root(a['relpath'])).encode()).hexdigest()[:24]
    if a['ext']=='.3mf':
        path,_=core.source_path(aid,version)
        with zipfile.ZipFile(path) as z:
            if len(z.infolist())>20000 or sum(i.file_size for i in z.infolist())>512*1024*1024:raise ValueError('3MF 解压内容超过浏览器预览容量，请选择工程中的零件网格')
    # Resolve transitive format references without loading unrelated project files.
    assets={aid:a};queue=[a];links=[];errors=[]
    while queue:
        row=queue.pop(0)
        try:
            for ref,kind in references(row,version if row['id']==aid else None):
                target,method=resolve_reference(a['project_id'],row['relpath'],ref)
                links.append({'source':row['id'],'target':target['id'] if target else None,'reference':ref,'relation':kind,'status':'resolved' if target else method})
                if target and target['id'] not in assets:assets[target['id']]=target;queue.append(target)
        except (OSError,ValueError,ET.ParseError) as e:errors.append(str(e)[:300])
        if len(assets)>2000:raise ValueError('模型引用数量超过预览限制')
    return {'asset':a,'model_id':mid,'assets':list(assets.values()),'links':links,'errors':errors,'version':version,'historical_dependencies':'当前引用版本；完整历史关系请使用工程快照' if version else None}
