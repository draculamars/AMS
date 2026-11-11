#!/usr/bin/env python3
"""Capture an existing model_library catalog as a standalone immutable web release.

Adapter for the catalog API format, not robot names or machine paths. Original
engineering files and original application code are never modified.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asset_manager import db,project_sync,web_assets,core

p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--project',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--session',default='');p.add_argument('--send',action='store_true')
a=p.parse_args();root=a.root.resolve();web=root/'model_library';out=a.output.resolve();out.mkdir(parents=True,exist_ok=True)
data=json.loads((web/'catalog.json').read_text());rows=data['versions'];prefix='web-publications/model-library/'
files={};objects={};fingerprints={};shared=set();generated=out/'generated';generated.mkdir(exist_ok=True)

def add(alias,path):
    web_assets.relative(alias)
    path=Path(path)
    if not path.resolve().is_relative_to(root) and not path.resolve().is_relative_to(out):raise ValueError('依赖位于声明范围之外：'+str(path))
    if path.is_symlink() or any(x.is_symlink() for x in path.parents if x!=root.parent and x.is_relative_to(root)):raise ValueError('软链接需要显式审查：'+str(path))
    before=path.stat();key=(before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)
    sha=fingerprints.get(key)
    if not sha:
        with path.open('rb') as f:sha=hashlib.file_digest(f,'sha256').hexdigest()
        after=path.stat()
        if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise ValueError('文件在发布时变化：'+str(path))
        fingerprints[key]=sha
    files[prefix+alias]={'sha256':sha,'size':before.st_size}
    objects[sha]={'sha256':sha,'source':str(path),'size':before.st_size,'mtime_ns':before.st_mtime_ns,'path':alias}

# Preserve the exact original application alongside the browsing adaptation.
for f in web.iterdir():
    if f.is_file() and f.suffix in {'.js','.html','.css','.py','.json','.command'}:
        add('source/model_library/'+f.name,f)
        if f.suffix in {'.js','.html','.css'}:
            text=f.read_text()
            shared.update(re.findall(r'''/files/([^"'`<>\s${}]+)''',text))
            text=text.replace("await fetch('/api/refresh')","Promise.resolve()")
            text=text.replace('/api/catalog','./catalog.json').replace('/model_library/','./').replace('/vendor/','./vendor/').replace('/files/','./files/')
            text=re.sub(r'''(href="\./files/[^"\n]+"\s+)target="_blank"''',r'\1target="_self"',text)
            # Dynamic ZIP construction is intentionally not reproduced. Existing
            # release packages remain downloadable with their original bytes.
            text=text.replace('/api/zip?kind=pure&id=','#unavailable-pure-').replace('/api/zip?kind=full&id=','#unavailable-full-')
            if f.name=='app.js':
                text+='''\n// Dynamic packages require the original independent service.
const markUnavailable=()=>document.querySelectorAll('a[href^="#unavailable-"]').forEach(a=>{a.removeAttribute('href');a.setAttribute('aria-disabled','true');a.title='固定归档不执行动态打包；请下载文件列表中的已有交付包';a.textContent+=' · 需原服务';a.style.opacity='.5'});
new MutationObserver(markUnavailable).observe(document.body,{childList:true,subtree:true});markUnavailable();
'''
            if f.name=='index.html':
                text=text.replace('<button id="refresh">刷新目录</button>','<button id="refresh" disabled>固定归档 · '+data.get('generated','')+'</button>')
                text=text.replace('<main class="grid"','<p style="margin:16px 32px">固定内容归档。已有交付包可下载；历史动态打包入口不可用，请通过文件列表取得原件。模拟视图不代表实机标定或验收。</p><main class="grid"')
            target=generated/f.name;target.write_text(text);add(f.name,target)
catalog=generated/'catalog.json';catalog.write_text(json.dumps(rows,ensure_ascii=False));add('catalog.json',catalog)
for i,(rel,record) in enumerate({f['path']:f for r in rows for f in r['files']}.items()):
    web_assets.relative(rel);add('files/'+rel,root/rel)
    if i%1000==0:print(json.dumps({'hashed':i,'total_files':len(files)}),flush=True)
vendor=web/'node_modules/three'
for sub in ['build','examples/jsm','src']:
    for f in (vendor/sub).rglob('*'):
        if f.is_file():add('vendor/'+f.relative_to(vendor).as_posix(),f)
for name in ['LICENSE','package.json']:
    if (vendor/name).is_file():add('vendor/'+name,vendor/name)
# Shared explanatory pages may be referenced outside a model's version folder.
for rel in sorted(shared|{'artifacts/preview_VIEW-R2/README.txt','archive/feishu/version_governance.json'}):
    web_assets.relative(rel)
    if (root/rel).is_file():add('files/'+rel,root/rel)
descriptor={'project_id':a.project,'files':files,'entries':[{'path':prefix+'index.html','title':'机器人模型库 · 固定归档','kind':'interactive','body':{'sessions':[{'id':a.session,'title':'来源模型库会话'}] if a.session else [],'source_entry':'model_library/index.html'}}],
 'metadata':{'adapter':'model-library-static-v1','catalog_generated':data.get('generated'),'models':len(rows),'series':len({r['project'] for r in rows}),'source_root':str(root),'source_session':a.session,'dynamic_zip':'unavailable; original release packages preserved','original_files_unchanged':True}}
(out/'publication.json').write_text(json.dumps(descriptor,ensure_ascii=False));(out/'objects.json').write_text(json.dumps(list(objects.values()),ensure_ascii=False))
if a.send:
    db.init();cfg=project_sync.config();inventory=project_sync.remote_json(cfg,'inventory',a.project)
    pending=[o for sha,o in objects.items() if inventory['objects'].get(sha)!=o['size']]
    print(json.dumps({'sending_objects':len(pending),'bytes':sum(o['size'] for o in pending)}),flush=True)
    print(json.dumps(project_sync.send_objects(cfg,pending)),flush=True)
print(json.dumps({'manifest':str(out/'publication.json'),'files':len(files),'unique_objects':len(objects),'models':len(rows)}),flush=True)
