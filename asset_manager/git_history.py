"""Read-only Git inspection, including worktrees, nested repositories and archived objects."""
import difflib
import hashlib
import json
import os
import posixpath
import re
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode, unquote, urlsplit
from . import core, db, models

HEX = re.compile(r'^[a-f0-9]{40}([a-f0-9]{24})?$')


def command(root, *args):
    return ['git', '--no-optional-locks', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
            '-c', 'diff.external=', '-c', 'protocol.allow=never', '-C', str(root), *args]


def environment():
    return {**os.environ, 'GIT_TERMINAL_PROMPT':'0', 'GIT_NO_LAZY_FETCH':'1', 'GIT_OPTIONAL_LOCKS':'0', 'GIT_LITERAL_PATHSPECS':'1'}


def run(root, *args, timeout=60, optional=False):
    result=subprocess.run(command(root,*args),capture_output=True,env=environment(),timeout=timeout)
    if result.returncode:
        if optional:return b''
        raise ValueError('无法读取 Git 历史：'+result.stderr.decode(errors='replace')[-800:])
    return result.stdout


def safe_relative(path, allow_empty=False):
    if not isinstance(path,str) or '\0' in path or '\\' in path or '\n' in path:
        raise ValueError('历史文件路径无效')
    if not path and allow_empty:return ''
    p=PurePosixPath(path)
    if not path or p.is_absolute() or any(x in {'..','.'} or x in core.EXCLUDE_DIRS or core.secret(x) for x in p.parts):
        raise ValueError('历史路径越界或不在可展示范围内')
    return str(p)


def discover(pid):
    with db.connect() as c:p=dict(c.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone())
    info=json.loads(p['git_json']);candidates=[info]+info.get('nested_repositories',[]);linked=[]
    for item in candidates:
        root=item.get('root')
        if not root or not Path(root).is_dir():continue
        common=run(root,'rev-parse','--path-format=absolute','--git-common-dir',optional=True).decode().strip()
        if not common:continue
        rid=hashlib.sha256(common.encode()).hexdigest()[:24]
        prefix=Path(root).relative_to(Path(p['path'])).as_posix() if Path(root).is_relative_to(Path(p['path'])) else ''
        if prefix=='.':prefix=''
        head=run(root,'rev-parse','--verify','HEAD',optional=True).decode().strip()
        branch=run(root,'symbolic-ref','--short','HEAD',optional=True).decode().strip() or '(detached)'
        refs=[]
        for line in run(root,'for-each-ref','--format=%(refname)%00%(objectname)%00%(objecttype)').splitlines():
            name,sha,kind=line.decode(errors='replace').split('\0');refs.append({'name':name,'oid':sha,'type':kind})
        heads=set()
        for line in run(root,'worktree','list','--porcelain',optional=True).decode(errors='replace').splitlines():
            if line.startswith('HEAD ') and HEX.fullmatch(line[5:]) and int(line[5:],16):heads.add(line[5:])
        if head:heads.add(head)
        shallow_path=Path(common)/'shallow';shallow=shallow_path.read_text().splitlines() if shallow_path.is_file() else []
        partial=bool(run(root,'config','--get-regexp','remote.*.promisor',optional=True))
        missing=[]
        if partial and (refs or heads):
            missing=sorted(line[1:] for line in run(root,'rev-list','--objects','--all',*sorted(heads),'--missing=print','--no-object-names').decode().splitlines() if line.startswith('?'))
        identity={'refs':refs,'heads':sorted(heads),'shallow':shallow}
        if missing:identity['missing_objects']=missing
        fingerprint=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
        count=int(run(root,'rev-list','--count','--all',*sorted(heads)).decode().strip() or 0) if refs or heads else 0
        status=run(root,'status','--porcelain=v1','-z','--untracked-files=no',optional=True).split(b'\0')
        changes=[{'status':r[:2].decode(errors='replace'),'path':r[3:].decode(errors='replace')} for r in status if len(r)>3]
        body={'partial':partial,'missing_objects':missing,'missing_count':len(missing),'refs':refs,'heads':sorted(heads),'commits':count,'shallow':bool(shallow),'shallow_boundaries':shallow,'fingerprint':fingerprint,
              'changes':changes[:1000],'changed_count':len(changes),'remote':item.get('remote'),'coverage':'本机可达引用和 worktree HEAD；不执行 fetch。浅克隆只含已有历史；LFS 对象单独标注。',
              'object_format':run(root,'rev-parse','--show-object-format',optional=True).decode().strip() or 'sha1'}
        role='主仓库' if not prefix else '构建依赖' if any(t in prefix for t in ['SourcePackages/','_deps/','.benchmark','upstream-']) else '子仓库'
        with db.connect() as c:
            c.execute('INSERT INTO repositories(id,source_root,common_dir,body,indexed_at) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET source_root=excluded.source_root,common_dir=excluded.common_dir,body=excluded.body,indexed_at=excluded.indexed_at',(rid,root,common,json.dumps(body,ensure_ascii=False),core.now()))
            c.execute('INSERT OR REPLACE INTO project_repositories VALUES(?,?,?,?,?,?,?)',(pid,rid,prefix,root,head or None,branch,role))
        linked.append((pid,prefix))
    with db.connect() as c:
        for row in c.execute('SELECT prefix FROM project_repositories WHERE project_id=?',(pid,)).fetchall():
            if (pid,row[0]) not in linked:c.execute('DELETE FROM project_repositories WHERE project_id=? AND prefix=?',(pid,row[0]))
    return len(linked)


def repository(rid):
    original_id=rid;rid,_,snapshot=rid.partition('@')
    with db.connect() as c:r=c.execute('SELECT * FROM repositories WHERE id=?',(rid,)).fetchone()
    if not r:raise ValueError('仓库索引不存在，请刷新本机工程索引')
    r=dict(r)
    if snapshot:
        with db.connect() as c:s=c.execute('SELECT * FROM repository_archives WHERE repository_id=? AND fingerprint=?',(rid,snapshot)).fetchone()
        if not s:raise ValueError('历史副本版本不存在')
        r.update(body=s['body'],archive_path=s['archive_path'],archive_at=s['created_at'])
    r['id']=original_id;r['metadata']=json.loads(r.pop('body'))
    archived=Path(r['archive_path']) if r.get('archive_path') else None
    if os.environ.get('ATLAS_PORTABLE_ROOT'):
        archived=db.DATA/'git-history'/'objects'/r['metadata']['fingerprint']
    if not archived or not archived.is_dir():
        candidate=db.DATA/'git-history'/'objects'/r['metadata']['fingerprint']
        if candidate.is_dir():archived=candidate
    if not os.environ.get('ATLAS_PORTABLE_ROOT') and not snapshot and Path(r['source_root']).is_dir():r['location']=r['source_root'];r['source']='local'
    elif archived and archived.is_dir():r['location']=str(archived);r['source']='archive'
    else:raise FileNotFoundError('仓库历史尚未同步到当前主机；文件归档仍可浏览')
    return r


def resolve_commit(repo, ref=None):
    ref=ref or next(iter(repo['metadata']['heads']),None)
    if not ref or len(ref)>300 or ref.startswith('-') or '\0' in ref:raise ValueError('Git 引用无效或仓库尚无提交')
    value=run(repo['location'],'rev-parse','--verify','--end-of-options',ref+'^{commit}').decode().strip()
    if not HEX.fullmatch(value):raise ValueError('无法解析提交')
    return value


def commits(rid,ref='',page=1,limit=40,path=''):
    if page<1 or not 1<=limit<=100:raise ValueError('分页参数无效')
    repo=repository(rid)
    if not repo['metadata']['heads'] and not repo['metadata']['refs']:return {'items':[],'has_more':False,'repository':public(repo)}
    args=['log','--date-order','--format=%H%x00%P%x00%an%x00%aI%x00%cI%x00%s%x00','-z',f'--skip={(page-1)*limit}',f'-n{limit+1}']
    if ref:args.append(resolve_commit(repo,ref))
    else:args+=['--all',*repo['metadata']['heads']]
    args+=['--']
    if path:args.insert(1,'--follow');args.append(safe_relative(path))
    raw=run(repo['location'],*args);fields=raw.split(b'\0');items=[];i=0
    while i<len(fields):
        if not fields[i]:i+=1;continue
        if i+5>=len(fields):break
        oid,parents,author,authored,committed,subject=[x.decode(errors='replace') for x in fields[i:i+6]];i+=6
        if HEX.fullmatch(oid):items.append({'oid':oid,'parents':parents.split(),'author':author,'authored_at':authored,'created_at':committed,'subject':subject})
    return {'items':items[:limit],'has_more':len(items)>limit,'page':page,'repository':public(repo)}


def public(repo):
    return {k:v for k,v in repo.items() if k not in {'location','common_dir'}}


def tree(rid,ref='',path='',page=1,limit=100):
    if page<1 or not 1<=limit<=500:raise ValueError('分页参数无效')
    repo=repository(rid);commit=resolve_commit(repo,ref);path=safe_relative(path,True)
    spec=commit+(':'+path if path else '')
    entries=[];excluded=0
    for line in run(repo['location'],'ls-tree','-z','-l',spec).split(b'\0'):
        if not line:continue
        info,name=line.split(b'\t',1);mode,kind,oid,size=info.decode().split();name=name.decode(errors='replace');relative=posixpath.join(path,name)
        try:safe_relative(relative)
        except ValueError:excluded+=1;continue
        entries.append({'name':name,'path':relative,'type':'directory' if kind=='tree' else 'submodule' if kind=='commit' else 'symlink' if mode=='120000' else 'file','oid':oid,'mode':mode,'size':int(size) if size.isdecimal() else None,'content_available':size.isdecimal() or kind=='tree','ext':PurePosixPath(name).suffix.lower()})
    entries.sort(key=lambda x:(x['type']!='directory',x['name'].casefold()))
    return {'commit':commit,'path':path,'items':entries[(page-1)*limit:page*limit],'total':len(entries),'page':page,'excluded':excluded,'repository':public(repo)}


def blob_info(rid,ref,path):
    repo=repository(rid);commit=resolve_commit(repo,ref);path=safe_relative(path)
    raw=run(repo['location'],'ls-tree','-z','-l',commit,'--',path)
    if not raw:raise FileNotFoundError('该提交中没有此文件')
    info,name=raw.rstrip(b'\0').split(b'\t',1);mode,kind,oid,size=info.decode().split()
    if kind!='blob' or mode=='120000':raise ValueError('符号链接和子模块仅展示引用，不自动跟随或执行')
    if not size.isdecimal():raise ValueError('此文件内容未包含在本机部分克隆中；目录与提交信息可见，但不能下载或渲染缺失对象')
    return repo,commit,{'oid':oid,'path':path,'name':PurePosixPath(path).name,'ext':PurePosixPath(path).suffix.lower(),'size':int(size),'mode':mode}


def blob_bytes(rid,ref,path,maximum=2*1024*1024):
    repo,commit,a=blob_info(rid,ref,path)
    if a['size']>maximum:raise ValueError('文件超过在线文本读取上限，请下载或选择模型预览')
    return run(repo['location'],'cat-file','blob',a['oid']),a,commit


def file_path(rid,ref,path):
    repo,commit,a=blob_info(rid,ref,path)
    folder=db.DATA/'git-previews'/rid;folder.mkdir(parents=True,exist_ok=True)
    out=folder/a['oid']
    if not out.exists():
        if a['size']>__import__('shutil').disk_usage(db.DATA).free-256*1024**2:raise ValueError('历史文件缓存空间不足')
        import tempfile
        fd,tmp=tempfile.mkstemp(dir=folder)
        try:
            with os.fdopen(fd,'wb') as dest:
                r=subprocess.run(command(repo['location'],'cat-file','blob',a['oid']),stdout=dest,stderr=subprocess.PIPE,env=environment(),timeout=120)
            if r.returncode or Path(tmp).stat().st_size!=a['size']:raise ValueError('历史内容读取失败')
            os.replace(tmp,out)
        finally:Path(tmp).unlink(missing_ok=True)
    return out,a


def changes(rid,ref,base='',path=''):
    repo=repository(rid);commit=resolve_commit(repo,ref)
    parents=run(repo['location'],'rev-list','--parents','-n','1',commit).decode().split()[1:]
    previous=resolve_commit(repo,base) if base else (parents[0] if parents else None)
    if path:
        path=safe_relative(path);contents=[]
        for sha in [previous,commit]:
            if not sha:contents.append('');continue
            try:raw,_,_=blob_bytes(rid,sha,path)
            except FileNotFoundError:raw=b''
            if b'\0' in raw[:8192]:return {'binary':True,'commit':commit,'base':previous,'path':path,'message':'二进制文件可分别下载或打开三维预览'}
            contents.append(raw.decode('utf-8',errors='replace'))
        lines=list(difflib.unified_diff(contents[0].splitlines(),contents[1].splitlines(),fromfile=(previous or 'empty')[:12]+'/'+path,tofile=commit[:12]+'/'+path,lineterm=''))
        return {'commit':commit,'base':previous,'path':path,'diff':'\n'.join(lines[:8000]),'truncated':len(lines)>8000}
    args=['diff','--no-ext-diff','--no-textconv','--name-status','-z','-M',previous,commit,'--'] if previous else ['diff-tree','--root','--no-commit-id','-r','--name-status','-z',commit,'--']
    tokens=run(repo['location'],*args).split(b'\0');items=[];i=0
    while i<len(tokens) and tokens[i]:
        status=tokens[i].decode();i+=1
        old=tokens[i].decode(errors='replace');i+=1
        new=old
        if status.startswith(('R','C')):new=tokens[i].decode(errors='replace');i+=1
        try:safe_relative(new)
        except ValueError:continue
        items.append({'status':status,'path':new,'previous_path':old})
    return {'commit':commit,'base':previous,'parents':parents,'items':items[:3000],'total':len(items),'truncated':len(items)>3000}


def model_context(rid,ref,path):
    repo,commit,initial=blob_info(rid,ref,path)
    raw=run(repo['location'],'ls-tree','-r','-z',commit);paths={}
    for line in raw.split(b'\0'):
        if not line:continue
        info,name=line.split(b'\t',1);mode,kind,oid=info.decode().split();name=name.decode(errors='replace')
        if kind=='blob' and mode!='120000':
            try:safe_relative(name)
            except ValueError:continue
            paths[name]=oid
    def asset(p):
        size=int(run(repo['location'],'cat-file','-s',paths[p]).decode())
        if size<1024 and run(repo['location'],'cat-file','blob',paths[p]).startswith(b'version https://git-lfs.github.com/spec/v1'):
            raise ValueError('历史模型引用的是 Git LFS 指针：'+p+'；需对应 LFS 对象才能渲染')
        return {'id':p,'relpath':p,'name':PurePosixPath(p).name,'ext':PurePosixPath(p).suffix.lower(),'size':size,'file_url':'/api/repositories/'+rid+'/file?'+urlencode({'ref':commit,'path':p}),'historical':True}
    assets={path:asset(path)};links=[];queue=[path];errors=[]
    while queue:
        p=queue.pop(0);a=assets[p]
        if a['ext'] not in {'.urdf','.xacro','.obj','.mtl','.gltf','.glb','.usd','.usda'}:continue
        try:
            data,_,_=blob_bytes(rid,commit,p,16*1024**2);refs=[]
            if a['ext'] in {'.urdf','.xacro'}:
                import xml.etree.ElementTree as ET
                if b'<!DOCTYPE' in data or b'<!ENTITY' in data:raise ValueError('XML 实体声明不受支持')
                for node in ET.fromstring(data).iter():
                    tag=node.tag.split('}')[-1]
                    if tag in {'mesh','texture','include'} and node.get('filename'):refs.append((node.get('filename'),tag))
            elif a['ext']=='.obj':refs=[(l.strip()[7:].strip(),'material') for l in data.decode(errors='replace').splitlines() if l.strip().startswith('mtllib ')]
            elif a['ext']=='.mtl':
                import shlex
                for l in data.decode(errors='replace').splitlines():
                    bits=shlex.split(l,comments=True)
                    if len(bits)>1 and (bits[0].lower().startswith('map_') or bits[0].lower() in {'bump','norm','disp'}):refs.append((bits[-1],'texture'))
            elif a['ext'] in {'.gltf','.glb'}:
                if a['ext']=='.glb':
                    import struct
                    length,kind=struct.unpack_from('<II',data,12);data=data[20:20+length]
                doc=json.loads(data);refs=[(i['uri'],'resource') for i in doc.get('buffers',[])+doc.get('images',[]) if i.get('uri') and not i['uri'].startswith('data:')]
            else:refs=[(x,'reference') for x in re.findall(r'@([^@\r\n]+)@',data.decode(errors='replace'))]
            for reference,kind in refs:
                target=None;value=unquote(reference).replace('\\','/')
                if value.startswith('package://'):
                    pkg,_,tail=value[10:].partition('/')
                    for ancestor in PurePosixPath(p).parents:
                        for candidate in [str(ancestor/pkg/tail),str(ancestor/tail)]:
                            if candidate in paths:target=candidate;break
                        if target:break
                elif not urlsplit(value).scheme and not value.startswith('/'):
                    candidate=posixpath.normpath(posixpath.join(posixpath.dirname(p),value));target=candidate if candidate in paths else None
                links.append({'source':p,'target':target,'reference':reference,'relation':kind,'status':'resolved' if target else 'missing-in-commit'})
                if target and target not in assets:assets[target]=asset(target);queue.append(target)
            if len(assets)>2000:raise ValueError('历史模型依赖超过预览上限')
        except (ValueError,OSError, __import__('xml.etree.ElementTree',fromlist=['ParseError']).ParseError) as e:errors.append(str(e))
    return {'asset':assets[path],'assets':list(assets.values()),'links':links,'errors':errors,'commit':commit,'historical_dependencies':'所有已解析引用均来自同一 Git 提交 '+commit[:12]}


def bundle(rid):
    """Download an independent archived bare repository, never the source .git directory."""
    import shutil,tempfile,zipfile
    repo=repository(rid);key=repo['metadata']['fingerprint']
    folder=db.DATA/'git-history/objects'/key
    if not (folder/'ATLAS-GIT.json').is_file():
        raise ValueError('完整 Git 历史副本保存在归档主机，请从 归档节点 下载')
    files=[p for p in folder.rglob('*') if p.is_file() and not p.is_symlink()]
    if sum(p.stat().st_size for p in files)>shutil.disk_usage(db.DATA).free-256*1024**2:
        raise ValueError('历史包临时导出空间不足')
    fd,tmp=tempfile.mkstemp(suffix='.zip',dir=db.DATA/'tmp');os.close(fd)
    name=Path(repo['source_root']).name+'-git-history'
    try:
        with zipfile.ZipFile(tmp,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as z:
            for p in files:z.write(p,name+'/'+p.relative_to(folder).as_posix())
            z.writestr('ATLAS-HISTORY-README.txt','This package contains an independent bare Git repository.\nInspect ATLAS-GIT.json for captured refs, worktree heads, shallow boundaries and missing partial-clone objects.\nIt does not include uncommitted working files; download the project file archive separately.\nTo create a working checkout, run git clone --branch <captured-branch> <bare-folder> <new-directory>.\nThe archive does not alter the original project or its remotes.\n')
        return Path(tmp),name+'.zip'
    except BaseException:
        Path(tmp).unlink(missing_ok=True);raise
