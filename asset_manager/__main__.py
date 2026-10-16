import argparse
import getpass
import json
from pathlib import Path
from . import db, core

def main():
    p=argparse.ArgumentParser(description='Atlas 本地资产管理')
    sub=p.add_subparsers(dest='command',required=True)
    serve=sub.add_parser('serve'); serve.add_argument('--host',default='127.0.0.1'); serve.add_argument('--port',type=int,default=8765)
    imp=sub.add_parser('import'); imp.add_argument('manifest',type=Path)
    scan=sub.add_parser('scan'); scan.add_argument('--project')
    arc=sub.add_parser('archive'); arc.add_argument('--project'); arc.add_argument('--artifacts-only',action='store_true')
    structure=sub.add_parser('index');structure.add_argument('--project')
    sync=sub.add_parser('sync');sync.add_argument('--project')
    target=sub.add_parser('configure-sync');target.add_argument('--host',required=True);target.add_argument('--root',required=True)
    sub.add_parser('set-password')
    backup=sub.add_parser('backup'); backup.add_argument('destination',type=Path)
    args=p.parse_args(); db.init()
    if args.command=='serve':
        import uvicorn
        uvicorn.run('asset_manager.server:app',host=args.host,port=args.port,proxy_headers=False)
    elif args.command=='import': print(core.import_projects(json.loads(args.manifest.read_text())))
    elif args.command=='scan': print(json.dumps(core.scan_all(lambda n,t,d: print(f'{n}/{t} {d}',flush=True),args.project),ensure_ascii=False,indent=2))
    elif args.command in {'index','sync'}:
        from . import project_index,project_sync
        worker=project_index.index_all if args.command=='index' else project_sync.synchronize
        with core.MUTATION_LOCK:print(json.dumps(worker(lambda n,t,d:print(f'{n}/{t} {d}',flush=True),args.project),ensure_ascii=False))
    elif args.command=='archive':
        with db.connect() as con:
            query="SELECT id FROM assets WHERE present=1 AND NOT EXISTS(SELECT 1 FROM versions WHERE asset_id=assets.id)"
            params=[]
            if args.project: query+=' AND project_id=?'; params.append(args.project)
            if args.artifacts_only: query+=" AND kind<>'code'"
            ids=[r[0] for r in con.execute(query,params)]
        print(json.dumps(core.archive_many(lambda n,t,d:print(f'{n}/{t} {d}',flush=True),ids),ensure_ascii=False))
    elif args.command=='configure-sync':
        from .project_sync import validate_config
        cfg=validate_config({'host':args.host,'root':args.root})
        with db.connect() as con:con.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('sync_target',json.dumps(cfg)))
        print('同步目标已保存；执行 sync 才连接。')
    elif args.command=='set-password':
        from .server import set_password
        password=getpass.getpass('管理员密码（至少 12 位）：')
        if password!=getpass.getpass('再次输入：'): raise SystemExit('两次密码不一致')
        set_password(password); print('密码已设置，所有旧会话已失效。')
    elif args.command=='backup':
        import shutil,sqlite3,hashlib
        target=args.destination.expanduser().resolve(); target.mkdir(parents=True,exist_ok=False)
        if target.is_relative_to(db.DATA): raise SystemExit('备份目标不能位于运行数据目录内')
        with db.connect() as con,sqlite3.connect(target/'catalog.sqlite3') as dest:
            con.backup(dest)
        # Blobs are immutable: use the consistent DB backup to select the exact set.
        with sqlite3.connect(target/'catalog.sqlite3') as con:
            hashes=[r[0] for r in con.execute('SELECT DISTINCT sha256 FROM versions')]
            con.execute("DELETE FROM settings WHERE key LIKE 'session:%'")
        manifest=[]
        for sha in hashes:
            src=db.blob_root()/sha[:2]/sha; dst=target/'blobs'/sha[:2]/sha
            dst.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(src,dst)
            with dst.open('rb') as f:
                if hashlib.file_digest(f,'sha256').hexdigest()!=sha: raise SystemExit('备份校验失败：'+sha)
            manifest.append(sha)
        support={}
        with sqlite3.connect(target/'catalog.sqlite3') as con:
            runs=[r[0] for r in con.execute('SELECT manifest FROM project_index_runs')]
            archives=[r[0] for r in con.execute('SELECT DISTINCT fingerprint FROM repository_archives')]
        for name in runs:
            src=db.DATA/'project-index'/Path(name).name;dst=target/'project-index'/Path(name).name
            dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dst)
            with dst.open('rb') as f:support[dst.relative_to(target).as_posix()]=hashlib.file_digest(f,'sha256').hexdigest()
        for key in archives:
            src=db.DATA/'git-history/objects'/key;dst=target/'git-history/objects'/key
            shutil.copytree(src,dst)
            for file in dst.rglob('*'):
                if file.is_file():
                    with file.open('rb') as f:support[file.relative_to(target).as_posix()]=hashlib.file_digest(f,'sha256').hexdigest()
        (target/'manifest.json').write_text(json.dumps({'created_at':core.now(),'sha256':manifest,'supporting_files':support,'git_history_snapshots':len(archives),'index_snapshots':len(runs)},indent=2))
        print(f'备份完成并校验 {len(hashes)} 个归档对象：{target}')

if __name__=='__main__': main()
