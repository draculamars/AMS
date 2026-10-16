"""Derived Blender previews; immutable originals and their logical paths are retained."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from . import core, db, models

def executable():
    explicit=os.environ.get('ASSET_BLENDER')
    candidates=[explicit,shutil.which('blender'),'/Applications/Blender.app/Contents/MacOS/Blender']
    for p in candidates:
        if p and Path(p).is_file():return [p,'--background','--factory-startup','--disable-autoexec','--python']
    python=Path(__file__).resolve().parents[1]/'.runtime/blender/bin/python'
    if python.is_file():return [str(python)]
    return None

def inputs(aid,version=None):
    a=core.asset_row(aid)
    if a['ext']!='.blend':raise ValueError('此转换器只处理 Blender 原生工程')
    root=models.infer_root(a['relpath'])
    rows=models.scoped_assets({'project_id':a['project_id'],'root':root})
    rows=[r for r in rows if r['id']==aid or r['ext'] in models.RESOURCE|{'.blend'}]
    if not any(r['id']==aid for r in rows):rows.append(a)
    parts=[];files=[]
    for r in rows:
        path,_=core.source_path(r['id'],version if r['id']==aid else None);st=path.stat()
        parts.append((r['id'],str(path),st.st_size,st.st_mtime_ns));files.append((r,path))
    key=hashlib.sha256(json.dumps(parts,sort_keys=True).encode()).hexdigest()
    return a,files,key

def state(aid,version=None):
    _,_,key=inputs(aid,version);folder=db.DATA/'model-previews'/key
    if (folder/'metadata.json').exists() and (folder/'model.glb').exists():
        return {'status':'ready','key':key,'metadata':json.loads((folder/'metadata.json').read_text()),'url':f'/api/model-previews/{key}/file'}
    return {'status':'available' if executable() else 'unavailable','key':key,'message':'需要本机 Blender 或独立的 bpy 转换运行环境'}

def convert(progress,aid,version=None):
    a,files,key=inputs(aid,version);command=executable()
    if not command:raise ValueError('当前主机尚未安装 Blender 预览运行环境')
    cache=db.DATA/'model-previews'/key;cache.mkdir(parents=True,exist_ok=True)
    amount=sum(p.stat().st_size for _,p in files)
    core.storage_preflight(amount)
    with tempfile.TemporaryDirectory(prefix='model-preview-',dir=db.DATA/'tmp') as tmp:
        work=Path(tmp);project=work/'project';project.mkdir()
        for i,(r,path) in enumerate(files):
            rel=Path(r['relpath'])
            if rel.is_absolute() or '..' in rel.parts:raise ValueError('工程路径无效')
            dest=project/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,dest)
            progress(i+1,len(files)+1,'准备工程资源：'+r['name'])
        source=project/a['relpath'];out=work/'model.glb';metadata=work/'metadata.json'
        script=Path(__file__).with_name('blender_export.py')
        args=command+[str(script)]
        if len(command)>1:args+=['--']
        args += [str(source),str(out),str(metadata),str(project)]
        progress(len(files),len(files)+1,'正在读取 Blender 场景、材质和装配层级')
        result=subprocess.run(args,capture_output=True,text=True,timeout=240,env={**os.environ,'PYTHONNOUSERSITE':'1'})
        if result.returncode or not out.exists():raise ValueError('Blender 转换失败：'+(result.stderr or result.stdout)[-1500:])
        if inputs(aid,version)[2]!=key:raise ValueError('转换期间源工程发生变化，请重新生成预览')
        meta=json.loads(metadata.read_text());meta.update(asset_id=aid,source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),generated_at=core.now(),preview_sha256=hashlib.sha256(out.read_bytes()).hexdigest(),derived=True,format='glb')
        os.replace(out,cache/'model.glb');(cache/'metadata.json').write_text(json.dumps(meta,ensure_ascii=False))
    with db.connect() as c:db.audit(c,'model_preview',{'asset_id':aid,'key':key,'source_sha256':meta['source_sha256']})
    progress(len(files)+1,len(files)+1,'原生场景预览已生成')
    return {'key':key,'asset_id':aid}

def preview_file(key):
    if len(key)!=64 or any(x not in '0123456789abcdef' for x in key):raise ValueError('无效的预览标识')
    path=db.DATA/'model-previews'/key/'model.glb'
    if not path.is_file():raise FileNotFoundError('预览尚未生成')
    return path
