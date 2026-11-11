"""Plaintext, versioned library recovery points over immutable SSD objects."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path, PurePosixPath
from . import db, core, portable


def digest(path):
    with path.open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()


def root():
    if not portable.enabled(): raise ValueError('当前不是随行资产库')
    db.portable_storage_check()
    return Path(os.environ['ATLAS_PORTABLE_ROOT']).resolve()


def save(path, value):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def create(progress):
    from . import web_assets
    web_assets.init()
    base = root(); ident = uuid.uuid4().hex
    folder = base/'backups/library-snapshots'/ident
    target = folder/'catalog'; target.mkdir(parents=True)
    progress(0, 1, '保存明文目录恢复点')
    with db.connect() as src, closing(sqlite3.connect(target/'catalog.sqlite3')) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('恢复点数据库校验失败')
        objects = dict(dst.execute('SELECT sha256,size FROM versions UNION SELECT sha256,size FROM mirror_files UNION SELECT sha256,size FROM web_objects'))
    for name in ['project-index', 'web-bundles', 'git-history']:
        source = db.DATA/name
        if source.exists():
            for p in source.rglob('*'):
                if p.is_symlink(): raise ValueError('恢复点元数据中存在软链接')
            shutil.copytree(source, target/name)
    files = {p.relative_to(target).as_posix(): {'sha256': digest(p), 'size': p.stat().st_size}
             for p in target.rglob('*') if p.is_file()}
    for sha, size in objects.items():
        if not re.fullmatch('[a-f0-9]{64}', sha): raise ValueError('内容对象标识无效')
        p = base/'archive'/sha[:2]/sha
        if p.is_symlink() or not p.is_file() or p.stat().st_size != size: raise ValueError('资产对象缺失，恢复点未发布：'+sha)
    config = json.loads((base/'portable.json').read_text(encoding='utf-8'))
    manifest = {'format': 1, 'id': ident, 'created_at': core.now(), 'encrypted': False, 'library_id': config['id'], 'portable_config': config,
                'storage': 'shared_immutable_objects', 'files': files, 'objects': objects,
                'note': '明文盘内恢复点，共享 archive 中的不可变内容；不是另一块磁盘上的副本。独立备份请复制整个 AssetVault。'}
    save(folder/'manifest.json', manifest)
    save(folder/'summary.json', {'id':ident,'created_at':manifest['created_at'],'encrypted':False,
                                'objects':len(objects),'object_bytes':sum(objects.values()),'metadata_files':len(files)})
    with db.connect() as c: db.audit(c, 'portable_backup', {'id': ident, 'encrypted': False, 'objects': len(objects)})
    progress(1, 1, '明文恢复点已保存')
    return {'id': ident, 'path': str(folder), 'encrypted': False, 'objects': len(objects), 'metadata_files': len(files)}


def listing():
    items=[]
    for p in sorted((root()/'backups/library-snapshots').glob('*/summary.json'),key=lambda p:p.stat().st_mtime,reverse=True):
        item=json.loads(p.read_text(encoding='utf-8'))
        check=p.parent/'verification.json'
        item['verification']=json.loads(check.read_text(encoding='utf-8')) if check.exists() else {'status':'pending'}
        items.append(item)
    return items


def verify(ident, full=True):
    try:
        return _verify(ident, full)
    except (ValueError, OSError, sqlite3.Error) as error:
        if full and re.fullmatch('[a-f0-9]{32}', ident):
            folder=root()/'backups/library-snapshots'/ident
            if folder.is_dir():
                save(folder/'verification.json', {'id':ident,'status':'failed','encrypted':False,
                     'checked_at':core.now(),'detail':str(error)})
        raise


def _verify(ident, full):
    if not re.fullmatch('[a-f0-9]{32}', ident): raise ValueError('恢复点标识无效')
    base = root(); folder = base/'backups/library-snapshots'/ident
    manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format') != 1 or manifest.get('encrypted') is not False or manifest.get('id')!=ident: raise ValueError('恢复点格式无效')
    for name, info in manifest['files'].items():
        rel = PurePosixPath(name)
        if rel.is_absolute() or '..' in rel.parts or '\\' in name: raise ValueError('恢复点路径无效')
        p = folder/'catalog'/name
        if not p.resolve().is_relative_to(folder.resolve()) or p.is_symlink() or not p.is_file() or p.stat().st_size != info['size'] or digest(p) != info['sha256']:
            raise ValueError('恢复点文件损坏：'+name)
    for sha, size in manifest['objects'].items():
        if not re.fullmatch('[a-f0-9]{64}', sha): raise ValueError('内容对象标识无效')
        p = base/'archive'/sha[:2]/sha
        if p.is_symlink() or not p.is_file() or p.stat().st_size != size or (full and digest(p) != sha): raise ValueError('恢复点内容缺失或损坏：'+sha)
    with closing(sqlite3.connect((folder/'catalog/catalog.sqlite3').as_uri()+'?mode=ro', uri=True)) as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('恢复点目录库校验失败')
    result={'id': ident, 'status': 'passed', 'encrypted': False, 'objects': len(manifest['objects']), 'full_content_hashes': full, 'checked_at':core.now()}
    if full:save(folder/'verification.json',result)
    return result


def check(progress, ident):
    progress(0,1,'校验明文恢复点与全部内容对象')
    result=verify(ident)
    progress(1,1,'恢复点完整校验通过')
    return result


def restore(ident):
    base = root()
    if (base/'session.json').exists(): raise ValueError('请先安全停止随行资产库，再恢复到新数据集')
    from . import file_lock
    with (base/'.session.lock').open('a+b') as handle:
        file_lock.acquire(handle, blocking=False)
        try:
            result = verify(ident)
            folder = base/'backups/library-snapshots'/ident
            manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
            current = json.loads((base/'portable.json').read_text(encoding='utf-8'))
            if manifest['library_id'] != current['id']: raise ValueError('恢复点不属于此资产库')
            dataset = uuid.uuid4().hex
            target = base/'datasets'/dataset/'catalog'
            target.mkdir(parents=True)
            for name in manifest['files']:
                destination=target/name;destination.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(folder/'catalog'/name,destination)
            # Keep the old dataset intact. Only the checked activation pointer changes.
            current['active_dataset'] = 'datasets/'+dataset
            current['restored_from'] = ident
            save(base/'portable.json', current)
            return {**result, 'activated_dataset': current['active_dataset'], 'previous_dataset_preserved': True}
        finally:
            file_lock.release(handle)
