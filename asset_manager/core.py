import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit
from . import db

EXCLUDE_DIRS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', '.pytest_cache',
                '.mypy_cache', '.ruff_cache', '.cache', '.runtime', '.secrets', '.ssh',
                '.aws', '.gnupg', '.gradle', '.build', 'CMakeFiles', '.next', '.nuxt',
                'Intermediates.noindex', 'Index.noindex', 'ModuleCache.noindex', '.Trash'}
SECRET_NAMES = {'.env', '.netrc', '.npmrc', '.pypirc', 'credentials', 'credentials.json',
                'auth.json', 'id_rsa', 'id_ed25519', '.DS_Store'}
SECRET_EXTS = {'.pem', '.key', '.p12', '.pfx', '.keystore'}
KINDS = {
 'image': 'png jpg jpeg webp gif bmp tif tiff svg heic',
 'document': 'pdf md markdown txt rst doc docx odt ppt pptx rtf',
 'data': 'csv tsv xls xlsx parquet json jsonl yaml yml toml sqlite db h5 hdf5 npy npz mat',
 'model': 'stl obj step stp iges igs brep 3mf blend glb gltf usd usda usdc usdz urdf xacro sdf fbx dae fcstd sldasm sldprt f3d f3z iam ipt scad kicad_pcb kicad_sch gerber brd sch',
 'media': 'mp4 webm mov m4v mp3 wav flac aac ogg',
 'archive': 'zip tar gz bz2 xz 7z dmg appimage iso exe deb rpm',
 'code': 'py js mjs cjs jsx ts tsx html css scss c cpp cc cxx h hpp ino rs go java swift sh bash zsh command bat ps1 cs kt m mm sql tex xml cmake lock'
}
EXT_KIND = {'.' + e:k for k,v in KINDS.items() for e in v.split()}
TEXT_EXTS = {'.'+e for k,v in KINDS.items() if k in {'code','data'} for e in v.split()} | {'.md','.markdown','.txt','.rst','.log','.gcode','.svg','.urdf','.sdf'}
BINARY_EXTS = {'.xlsx','.xls','.parquet','.sqlite','.db','.npy','.npz','.h5','.hdf5','.mat'}
TEXT_EXTS -= BINARY_EXTS
from .locking import MutationLock
MUTATION_LOCK = MutationLock()
LOCAL_BUILD_DATA = Path(__file__).resolve().parents[1] / 'data'

def now():
    return datetime.now(timezone.utc).isoformat()

def secret(path):
    name = Path(path).name.lower()
    return name in SECRET_NAMES or (name.startswith('.env.') and name not in {'.env.example','.env.sample'}) or Path(name).suffix in SECRET_EXTS

def git(path, *args, timeout=45, check=False):
    env = {**os.environ, 'GIT_TERMINAL_PROMPT':'0', 'GIT_NO_LAZY_FETCH':'1', 'GIT_OPTIONAL_LOCKS':'0', 'GIT_LITERAL_PATHSPECS':'1'}
    p = subprocess.run(['git','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false','-c','commit.gpgsign=false','-C',str(path),*args],
                       capture_output=True, env=env, timeout=timeout)
    if check and p.returncode:
        raise ValueError(p.stderr.decode(errors='replace')[:1500] or 'Git 命令失败')
    if p.returncode:
        return None
    output = p.stdout.decode('utf-8',errors='surrogateescape')
    return output if '-z' in args else output.strip()

def git_info(path):
    root = git(path,'rev-parse','--show-toplevel')
    if not root:
        return {'state':'unmanaged','root':None,'branch':None,'head':None,'tracked':0}
    head = git(path,'rev-parse','--verify','HEAD')
    branch = git(path,'symbolic-ref','--quiet','--short','HEAD')
    remote = git(path,'remote','get-url','origin')
    if remote and '://' in remote:
        u = urlsplit(remote)
        remote = urlunsplit((u.scheme,u.hostname or '',u.path,'',''))
    files = git(root,'ls-files','-z') or ''
    common = git(root,'rev-parse','--path-format=absolute','--git-common-dir')
    return {'state':'versioned' if head else 'unborn','root':root,'branch':branch or '(detached)',
            'head':head,'tracked':len([x for x in files.split('\0') if x]),'remote':remote,
            'common_dir':common, 'dirty':bool(git(path,'status','--porcelain','--untracked-files=no'))}

def safe_path(root, relative, must_exist=True):
    rel = PurePosixPath(relative)
    if rel.is_absolute() or '..' in rel.parts or not relative or '\\' in relative or '\0' in relative:
        raise ValueError('无效的相对路径')
    base = Path(root).resolve()
    current = base
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('不读取符号链接')
    target = current.resolve()
    if not target.is_relative_to(base) or target.is_relative_to(db.DATA) or target.is_relative_to(db.blob_root()) or target.is_relative_to(LOCAL_BUILD_DATA):
        raise ValueError('路径越界或指向系统数据目录')
    if any(x in EXCLUDE_DIRS for x in rel.parts) or secret(target):
        raise ValueError('该路径不在可管理资产范围内')
    if must_exist and (not target.is_file() or not stat.S_ISREG(target.stat().st_mode)):
        raise FileNotFoundError('源文件已离线或不存在')
    return target

def import_projects(payload):
    projects = payload.get('projects', payload) if isinstance(payload,dict) else payload
    if not isinstance(projects,list):
        raise ValueError('项目清单必须包含 projects 数组')
    imported = 0
    with db.connect() as con:
        for p in projects:
            if not p.get('path'):
                continue
            path = str(Path(p['path']).expanduser().resolve())
            name = str(p.get('label') or p.get('name') or Path(path).name)
            pid = str(p.get('projectId') or p.get('id') or uuid.uuid5(uuid.NAMESPACE_URL,path))
            existing = con.execute('SELECT id FROM projects WHERE path=?',(path,)).fetchone()
            if existing:
                pid = existing['id']
            con.execute('INSERT INTO projects(id,name,path,host) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,path=excluded.path,host=excluded.host',
                        (pid,name,path,p.get('hostId','local')))
            imported += 1
        db.audit(con,'import_projects',{'count':imported})
    return imported

def scan_project(pid):
    with db.connect() as con:
        p = con.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone()
    if not p:
        raise ValueError('项目不存在')
    root = Path(p['path'])
    with db.connect() as con:location=con.execute('SELECT mode FROM project_locations WHERE project_id=?',(pid,)).fetchone()
    if location and location[0]=='archive':return {'project':p['name'],'skipped':'archive_mapping'}
    if not root.is_dir():
        with db.connect() as con:
            con.execute("UPDATE projects SET scan_error='项目路径不存在，请映射到当前主机目录' WHERE id=?",(pid,))
        return {'project':p['name'],'error':'path_missing'}
    info = git_info(root)
    repos = {}
    excluded = Counter()
    rows = []
    directories = []
    errors = []
    def add_repo(folder):
        ri = git_info(folder)
        if ri['root']:
            files = git(ri['root'],'ls-files','-z') or ''
            repos[str(Path(ri['root']).resolve())] = (ri,set(files.split('\0')))
    if info['root']:
        add_repo(root)
    def walk_error(e):
        errors.append(str(e))
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        base = Path(directory)
        directories.append((pid,'' if base==root else base.relative_to(root).as_posix()))
        kept=[]
        for d in dirs:
            child=base/d
            if d in EXCLUDE_DIRS or child.is_symlink() or any(child.resolve().is_relative_to(x) for x in (db.DATA, db.blob_root(), LOCAL_BUILD_DATA)):
                excluded[d] += 1
            else:
                kept.append(d)
        dirs[:]=sorted(kept)
        if base != root and (base/'.git').exists():
            add_repo(base)
        for name in files:
            f=base/name
            if name == '.git' or secret(f) or f.is_symlink():
                excluded['sensitive_or_symlink']+=1
                continue
            try:
                st=f.stat()
                if not stat.S_ISREG(st.st_mode) or f.resolve().is_relative_to(db.DATA):
                    continue
                rel=f.relative_to(root).as_posix()
                repo = next((k for k in sorted(repos,key=len,reverse=True) if f.is_relative_to(k)),None)
                git_state='untracked'
                if repo:
                    ri,tracked=repos[repo]
                    if f.relative_to(repo).as_posix() in tracked:
                        git_state='tracked' if ri['head'] else 'staged'
                ext=f.suffix.lower()
                kind=EXT_KIND.get(ext,'other')
                if name in {'.gitignore','.gitattributes','Dockerfile','Makefile','CMakeLists.txt'}: kind='code'
                if name.upper() in {'README','LICENSE','NOTICE','COPYING'}: kind='document'
                rows.append((pid,rel,name,ext,kind,st.st_size,st.st_mtime,git_state,repo))
            except (OSError,ValueError) as e:
                errors.append(str(e))
    info['nested_repositories']=[r[0] for k,r in repos.items() if k != info['root']]
    with db.connect() as con:
        con.execute('DELETE FROM project_directories WHERE project_id=?',(pid,))
        con.executemany('INSERT INTO project_directories(project_id,path) VALUES(?,?)',directories)
        con.execute('UPDATE assets SET present=0 WHERE project_id=? AND managed=0',(pid,))
        con.executemany('''INSERT INTO assets(project_id,relpath,name,ext,kind,size,mtime,git_state,repo_root)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id,relpath) DO UPDATE SET
            name=excluded.name,ext=excluded.ext,kind=excluded.kind,size=excluded.size,mtime=excluded.mtime,
            git_state=excluded.git_state,repo_root=excluded.repo_root,present=1''',rows)
        con.execute('UPDATE projects SET git_json=?,scan_at=?,scan_error=?,excluded_json=? WHERE id=?',
                    (json.dumps(info),now(),'\n'.join(errors[:20]) or None,json.dumps(dict(excluded)),pid))
        db.audit(con,'scan',{'project':p['name'],'files':len(rows),'excluded':dict(excluded),'errors':len(errors)})
    return {'project':p['name'],'files':len(rows),'excluded':dict(excluded),'errors':len(errors)}

def asset_row(asset_id):
    with db.connect() as con:
        r=con.execute('SELECT a.*,p.path AS project_path,p.name AS project_name FROM assets a JOIN projects p ON p.id=a.project_id WHERE a.id=?',(asset_id,)).fetchone()
    if not r:
        raise ValueError('资产不存在')
    return dict(r)

def source_path(asset_id, version=None):
    a=asset_row(asset_id)
    with db.connect() as con:
        if version:
            v=con.execute('SELECT * FROM versions WHERE asset_id=? AND id=?',(asset_id,version)).fetchone()
            if not v:
                raise ValueError('版本不存在')
        else:
            v=con.execute('SELECT * FROM versions WHERE asset_id=? AND reason IN (\'upload\',\'update\') ORDER BY id DESC LIMIT 1',(asset_id,)).fetchone()
            if not v and not a['present']:
                v=con.execute('SELECT sha256 FROM mirror_files WHERE project_id=? AND path=?',(a['project_id'],a['relpath'])).fetchone()
            if not v and not a['present']:
                v=con.execute('SELECT * FROM versions WHERE asset_id=? AND reason NOT LIKE \'source-history:%\' ORDER BY id DESC LIMIT 1',(asset_id,)).fetchone()
    if v:
        path=db.blob_root()/v['sha256'][:2]/v['sha256']
        if not path.is_file():
            raise FileNotFoundError('归档文件缺失，请从备份恢复')
        return path,a
    try:
        return safe_path(a['project_path'],a['relpath']),a
    except FileNotFoundError:
        # Restored catalogues may still say 'present' until the next scan.
        with db.connect() as con:
            fallback=con.execute('SELECT sha256 FROM versions WHERE asset_id=? AND reason NOT LIKE \'source-history:%\' ORDER BY id DESC LIMIT 1',(asset_id,)).fetchone()
        if fallback:
            archived=db.blob_root()/fallback['sha256'][:2]/fallback['sha256']
            if archived.is_file(): return archived,a
        raise

def store_blob(stream):
    digest=hashlib.sha256()
    size=0
    incoming=db.blob_root()/'.incoming'
    incoming.mkdir(exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=incoming)
    try:
        with os.fdopen(fd,'wb') as out:
            for chunk in iter(lambda:stream.read(1024*1024),b''):
                digest.update(chunk)
                size+=len(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        sha=digest.hexdigest()
        dest=db.blob_root()/sha[:2]/sha
        dest.parent.mkdir(exist_ok=True)
        if dest.exists():
            if dest.stat().st_size!=size:
                raise ValueError('归档对象校验失败：相同哈希的对象大小不同')
            os.unlink(tmp)
        else:
            os.replace(tmp,dest)
        return sha,size
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def archive_asset(asset_id,reason='snapshot'):
    path,a=source_path(asset_id)
    before=path.stat()
    storage_preflight(before.st_size)
    with path.open('rb') as f:
        sha,size=store_blob(f)
    after=path.stat()
    if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
        raise ValueError('文件在归档期间发生变化，请重试')
    with db.connect() as con:
        con.execute('INSERT OR IGNORE INTO versions(asset_id,sha256,size,reason) VALUES(?,?,?,?)',(asset_id,sha,size,reason))
        db.audit(con,'snapshot',{'asset_id':asset_id,'sha256':sha,'size':size})
    return {'sha256':sha,'size':size}

def upload(project_id,filename,stream,asset_id=None):
    name=Path(filename.replace('\\','/')).name
    if not name or secret(name) or name.startswith('.') or len(name)>240:
        raise ValueError('不支持该文件名')
    if asset_id:
        a=asset_row(asset_id)
        if a['project_id'] != project_id:
            raise ValueError('资产与项目不匹配')
        if Path(name).suffix.lower()!=a['ext']:
            raise ValueError('更新文件的扩展名必须与资产一致')
        # Preserve source before the first managed replacement.
        with db.connect() as con:
            previous=con.execute('SELECT id FROM versions WHERE asset_id=? LIMIT 1',(asset_id,)).fetchone()
        if not previous:
            archive_asset(asset_id, 'snapshot')
    with db.connect() as con:
        if not con.execute('SELECT id FROM projects WHERE id=?',(project_id,)).fetchone():
            raise ValueError('项目不存在')
    position=stream.tell()
    stream.seek(0,2)
    storage_preflight(stream.tell()-position)
    stream.seek(position)
    sha,size=store_blob(stream)
    with db.connect() as con:
        if not asset_id:
            rel='@uploads/'+uuid.uuid4().hex[:12]+'/'+name
            ext=Path(name).suffix.lower()
            cur=con.execute('INSERT INTO assets(project_id,relpath,name,ext,kind,size,mtime,git_state,managed) VALUES(?,?,?,?,?,?,?,?,1)',
                (project_id,rel,name,ext,EXT_KIND.get(ext,'other'),size,time.time(),'managed'))
            asset_id=cur.lastrowid
        con.execute('INSERT OR IGNORE INTO versions(asset_id,sha256,size,reason) VALUES(?,?,?,?)',
                    (asset_id,sha,size,'update' if 'a' in locals() else 'upload'))
        db.audit(con,'upload',{'asset_id':asset_id,'sha256':sha,'size':size})
    return {'asset_id':asset_id,'sha256':sha,'size':size}

def run_job(kind, function, *args):
    jid=uuid.uuid4().hex
    with db.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        busy=con.execute("SELECT id FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone()
        if busy:
            raise ValueError('已有任务执行中，请等待完成')
        con.execute('INSERT INTO jobs(id,kind,status) VALUES(?,?,?)',(jid,kind,'queued'))
    def progress(done,total,detail=''):
        with db.connect() as con:
            con.execute("UPDATE jobs SET status='running',done=?,total=?,detail=? WHERE id=?",(done,total,detail,jid))
    def work():
        try:
            with MUTATION_LOCK:
                result=function(progress,*args)
            with db.connect() as con:
                con.execute("UPDATE jobs SET status='complete',result=? WHERE id=?",(json.dumps(result,ensure_ascii=False),jid))
        except Exception as e:
            with db.connect() as con:
                con.execute("UPDATE jobs SET status='failed',detail=? WHERE id=?",(str(e)[:2000],jid))
    threading.Thread(target=work,daemon=True).start()
    return jid

def scan_all(progress,project_id=None):
    with db.connect() as con:
        ids=[r['id'] for r in con.execute('SELECT id FROM projects'+(' WHERE id=?' if project_id else ''),(project_id,) if project_id else ())]
    result=[]
    for i,pid in enumerate(ids):
        progress(i,len(ids),'正在扫描项目')
        result.append(scan_project(pid))
    progress(len(ids),len(ids),'索引完成')
    return result

def archive_many(progress,ids):
    # Conservative bound: batches that do not fit are rejected before copying.
    with db.connect() as con:
        wanted=set(ids)
        required=sum(r['size'] for r in con.execute('SELECT id,size FROM assets') if r['id'] in wanted)
    storage_preflight(required)
    result={'archived':0,'failed':[]}
    for i,aid in enumerate(ids):
        progress(i,len(ids),f'归档 {i+1} / {len(ids)}')
        try:
            archive_asset(aid)
            result['archived']+=1
        except Exception as e:
            result['failed'].append({'id':aid,'error':str(e)})
    progress(len(ids),len(ids),'归档完成' if not result['failed'] else '归档完成，部分文件失败')
    return result

def storage_preflight(required):
    reserve=int(os.environ.get('ASSET_RESERVE_BYTES',str(2*1024**3)))
    free=shutil.disk_usage(db.blob_root()).free
    if required > free-reserve:
        raise ValueError(f'可用空间不足：保守估计需要 {required/1024**3:.2f} GB，归档盘剩余 {free/1024**3:.2f} GB；另保留 {reserve/1024**3:.1f} GB 运行空间。请分批归档或更换存储目录。')

# Plans are immutable proposals; editing creates a fresh proposal. Execution requires
# an exact acknowledgement and matching source hashes, and never pushes a remote.
def git_plan(pid,branch='main',name=None,directory=None):
    with db.connect() as con:
        p=con.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone()
        assets=con.execute('SELECT * FROM assets WHERE project_id=? AND managed=0 AND present=1 ORDER BY relpath',(pid,)).fetchall()
    if not p: raise ValueError('项目不存在')
    root=Path(p['path']).resolve()
    if directory and Path(directory).expanduser().resolve()!=root:
        raise ValueError('当前版本支持原目录纳管；迁移需先完成目录映射和文件校验')
    info=git_info(root)
    if info['head']: raise ValueError('项目已有提交历史，请使用现有 Git 工作流提交')
    if info['root'] and Path(info['root']).resolve()!=root:
        raise ValueError('项目位于上级仓库内，不能自动建立嵌套仓库')
    if git(root,'check-ref-format','--branch',branch) is None:
        raise ValueError('分支名称无效')
    files=[]; excluded=[]
    ignored_result=subprocess.run(['git','-C',str(root),'check-ignore','--stdin','-z'],
        input=b'\0'.join(a['relpath'].encode() for a in assets)+b'\0',capture_output=True)
    ignored_paths=set(ignored_result.stdout.decode(errors='surrogateescape').split('\0'))
    for a in assets:
        rel=a['relpath']
        if a['repo_root'] and Path(a['repo_root']).resolve()!=root:
            excluded.append({'path':rel,'reason':'嵌套仓库，保留独立索引'}); continue
        if a['size']>10*1024*1024 or a['kind'] not in {'code','document','data'} or a['ext'] in BINARY_EXTS:
            excluded.append({'path':rel,'reason':'二进制／大型资产，使用归档快照'}); continue
        # Respect existing gitignore; do not force add generated or ignored material.
        if rel in ignored_paths:
            excluded.append({'path':rel,'reason':'现有 .gitignore 规则'}); continue
        f=safe_path(root,rel)
        with f.open('rb') as stream: content=stream.read(10*1024*1024+1)
        if b'\0' in content:
            excluded.append({'path':rel,'reason':'二进制文件'}); continue
        # Do not stage obvious embedded private keys or credential assignments.
        if re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY|(?:api[_-]?key|password|access[_-]?token)\s*[:=]\s*[\"\'][A-Za-z0-9_\-]{20,}',content,re.I):
            excluded.append({'path':rel,'reason':'疑似凭据，需单独核查'}); continue
        files.append({'path':rel,'size':a['size'],'sha256':hashlib.sha256(content).hexdigest()})
    plan={'project_id':pid,'directory':str(root),'name':name or p['name'],'branch':branch,
          'files':files,'excluded':excluded,'previous_branch':info['branch'],'message':'Archive initial project sources and documentation'}
    ident=uuid.uuid4().hex
    with db.connect() as con:
        con.execute('INSERT INTO plans(id,project_id,body) VALUES(?,?,?)',(ident,pid,json.dumps(plan,ensure_ascii=False)))
        db.audit(con,'git_plan',{'id':ident,'project':p['name'],'files':len(files)})
    return {'id':ident,**plan}

def execute_plan(plan_id,acknowledgement):
    if acknowledgement!='确认目录、名称、分支和文件清单并执行首次提交':
        raise ValueError('必须明确确认此方案后才能执行')
    with MUTATION_LOCK:
        with db.connect() as con:
            r=con.execute('SELECT * FROM plans WHERE id=?',(plan_id,)).fetchone()
        if not r or r['status']!='pending': raise ValueError('方案不存在或已执行')
        p=json.loads(r['body']); root=Path(p['directory'])
        if not p['files']: raise ValueError('没有可提交文件')
        info=git_info(root)
        if info['head']: raise ValueError('仓库已发生变化，请重新生成方案')
        if info['root'] and Path(info['root']).resolve()!=root: raise ValueError('仓库根目录已变化')
        if info['tracked']: raise ValueError('暂存区已有内容，请先自行处理后重新生成方案')
        for f in p['files']:
            path=safe_path(root,f['path'])
            with path.open('rb') as stream: sha=hashlib.file_digest(stream,'sha256').hexdigest()
            if sha!=f['sha256']: raise ValueError('方案中的文件已变化，请重新生成方案：'+f['path'])
        if not git(root,'config','user.name') or not git(root,'config','user.email'):
            raise ValueError('请先为 Git 配置 user.name 与 user.email；系统不会代填提交身份')
        if not info['root']:
            git(root,'init','-b',p['branch'],check=True)
        else:
            git(root,'symbolic-ref','HEAD','refs/heads/'+p['branch'],check=True)
        # Feed literal names through stdin; filenames never become shell commands.
        paths=b'\0'.join(f['path'].encode() for f in p['files'])+b'\0'
        env={**os.environ,'GIT_LITERAL_PATHSPECS':'1'}
        result=subprocess.run(['git','-c','core.hooksPath=/dev/null','-C',str(root),'add','--pathspec-from-file=-','--pathspec-file-nul'],input=paths,capture_output=True,env=env)
        if result.returncode: raise ValueError(result.stderr.decode(errors='replace')[:1200])
        # Verify the index exactly matches the approved content immediately before commit.
        staged=set((git(root,'ls-files','-z') or '').split('\0'))-{''}
        if staged!={f['path'] for f in p['files']}: raise ValueError('暂存区出现方案之外的变化，已停止提交')
        for f in p['files']:
            b=subprocess.run(['git','-C',str(root),'show',':'+f['path']],capture_output=True,check=True).stdout
            if hashlib.sha256(b).hexdigest()!=f['sha256']:
                raise ValueError('Git 过滤器或并发写入改变了内容，已停止提交')
        git(root,'commit','--no-verify','-m',p['message'],check=True,timeout=120)
        head=git(root,'rev-parse','HEAD')
        with db.connect() as con:
            con.execute("UPDATE plans SET status='executed' WHERE id=?",(plan_id,))
            con.execute('UPDATE projects SET name=? WHERE id=?',(p['name'],p['project_id']))
            db.audit(con,'git_commit',{'plan':plan_id,'head':head,'directory':str(root),'branch':p['branch']})
        scan_project(p['project_id'])
        return {'head':head}
