"""Review and atomically register engineering ZIP updates without writing source trees."""
import hashlib
import json
import os
import shutil
import stat
import time
import uuid
import zipfile
from pathlib import PurePosixPath
from . import core, db, models

MAX_EXPANDED = 2 * 1024**3
MAX_FILES = 5000
TTL = 3600


def digest(stream):
    h = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        h.update(block)
    return h.hexdigest()


def valid_path(value):
    p = PurePosixPath(value)
    if (not value or '\\' in value or ':' in value or '\0' in value or p.is_absolute()
        or any(x in {'', '.', '..'} for x in value.split('/'))
        or any(x in core.EXCLUDE_DIRS or core.secret(x) for x in p.parts)
        or any(ord(x) < 32 for x in value) or core.secret(value)):
        raise ValueError('工程包包含不安全或受排除的路径：' + value[:160])
    return str(p)


def stage(mid, stream):
    d = models.detail(mid)
    root = d['root']
    folders = db.DATA / 'tmp' / 'engineering-updates'
    folders.mkdir(exist_ok=True)
    for old in folders.iterdir():
        if old.is_dir() and old.stat().st_mtime < time.time() - TTL:
            shutil.rmtree(old)
    if len(list(folders.iterdir())) >= 8:
        raise ValueError('待确认工程包过多，请先确认或稍后再试')
    pid = uuid.uuid4().hex
    folder = folders / pid
    folder.mkdir()
    try:
        stream.seek(0, 2)
        length = stream.tell()
        stream.seek(0)
        if length > MAX_EXPANDED:
            raise ValueError('工程包超过 2 GiB 限制')
        core.storage_preflight(length)
        with (folder / 'package.zip').open('wb') as out:
            shutil.copyfileobj(stream, out)
        known = {a['relpath']: a for a in d['files']}
        rows = []
        with zipfile.ZipFile(folder / 'package.zip') as z:
            info = z.infolist()
            if len(info) > MAX_FILES or sum(i.file_size for i in info) > MAX_EXPANDED:
                raise ValueError('工程包超过 5,000 项或展开后 2 GiB 的限制')
            manifest = None
            if 'ATLAS-ENGINEERING-MANIFEST.json' in z.namelist():
                if z.getinfo('ATLAS-ENGINEERING-MANIFEST.json').file_size > 8 * 1024**2:
                    raise ValueError('工程清单过大')
                manifest = json.loads(z.read('ATLAS-ENGINEERING-MANIFEST.json'))
                if manifest.get('root') != root or manifest.get('project_id') != d['project_id']:
                    raise ValueError('工程包所属项目或工程根目录不匹配')
                manifest = {r['path']: r['sha256'] for r in manifest['files']}
            seen = set()
            for i in info:
                if i.is_dir():
                    continue
                if i.filename == 'ATLAS-ENGINEERING-MANIFEST.json':
                    continue
                if stat.S_ISLNK(i.external_attr >> 16) or i.flag_bits & 1:
                    raise ValueError('工程包不接受符号链接或加密条目')
                name = valid_path(i.filename)
                if manifest is not None:
                    if not name.startswith('project/'):
                        raise ValueError('Atlas 工程包文件须位于 project/ 下')
                    target = name[len('project/'):]
                else:
                    target = '/'.join(x for x in [root, name] if x)
                target = valid_path(target)
                if target.casefold() in seen:
                    raise ValueError('工程包含重复路径或大小写冲突：' + target)
                seen.add(target.casefold())
                if root and not target.startswith(root + '/') and target not in known:
                    raise ValueError('工程包试图修改未纳入此工程的外部文件：' + target)
                with z.open(i) as source:
                    sha = digest(source)
                if manifest is not None and manifest.get(target) != sha:
                    raise ValueError('工程包清单校验失败：' + target)
                a = known.get(target)
                with db.connect() as c:
                    existing = c.execute('SELECT id,relpath FROM assets WHERE project_id=? AND relpath=?', (d['project_id'], target)).fetchone()
                    collisions = c.execute('SELECT relpath FROM assets WHERE project_id=? AND lower(relpath)=lower(?) AND relpath<>?', (d['project_id'], target, target)).fetchall()
                if collisions:
                    raise ValueError('目标路径存在大小写冲突：' + target)
                if existing and not a:
                    a = core.asset_row(existing['id'])
                old_sha = None
                if a:
                    path, _ = core.source_path(a['id'])
                    with path.open('rb') as source:
                        old_sha = digest(source)
                rows.append({'path': target, 'zip_path': name, 'id': a['id'] if a else None,
                             'sha256': sha, 'size': i.file_size, 'previous_sha256': old_sha,
                             'action': 'unchanged' if sha == old_sha else 'update' if a else 'add'})
            if not rows:
                raise ValueError('工程包为空')
            if manifest is not None and set(manifest) != {r['path'] for r in rows}:
                raise ValueError('工程包与清单文件范围不一致')
        plan = {'id': pid, 'model_id': mid, 'project_id': d['project_id'], 'root': root,
                'created_at': time.time(), 'files': rows,
                'note': '保留包内目录；只更新资产库版本，原目录不变；包内未包含的文件不会删除。'}
        (folder / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False))
        return plan
    except (zipfile.BadZipFile, KeyError, TypeError, json.JSONDecodeError) as error:
        shutil.rmtree(folder)
        raise ValueError('工程 ZIP 或清单格式无效') from error
    except BaseException:
        shutil.rmtree(folder)
        raise


def apply(mid, pid):
    if len(pid) != 32 or any(x not in '0123456789abcdef' for x in pid):
        raise ValueError('无效的工程更新方案')
    folder = db.DATA / 'tmp' / 'engineering-updates' / pid
    plan = json.loads((folder / 'plan.json').read_text())
    if plan['model_id'] != mid or time.time() - plan['created_at'] > TTL:
        raise ValueError('工程更新方案不匹配或已过期，请重新上传')
    changed = [r for r in plan['files'] if r['action'] != 'unchanged']
    core.storage_preflight(sum(r['size'] for r in changed) * 2)
    stored = []
    fingerprints = []
    with zipfile.ZipFile(folder / 'package.zip') as z:
        for row in plan['files']:
            old_blob = None
            if row['id']:
                source, _ = core.source_path(row['id'])
                before = source.stat()
                with source.open('rb') as f:
                    if row['action'] == 'unchanged':
                        previous = digest(f)
                    else:
                        old_blob = core.store_blob(f)
                        previous = old_blob[0]
                fingerprints.append((row['id'], str(source), before.st_size, before.st_mtime_ns))
                if previous != row['previous_sha256']:
                    raise ValueError('审核后工程文件已变化，请重新上传：' + row['path'])
            if row['action'] == 'unchanged':
                continue
            with z.open(row['zip_path']) as f:
                sha, size = core.store_blob(f)
            if (sha, size) != (row['sha256'], row['size']):
                raise ValueError('暂存工程包校验失败')
            stored.append((row, old_blob))
    for aid, old_path, size, mtime in fingerprints:
        path, _ = core.source_path(aid)
        s = path.stat()
        if (str(path), s.st_size, s.st_mtime_ns) != (old_path, size, mtime):
            raise ValueError('工程在更新期间发生变化，请重新上传')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        for row, previous in stored:
            aid = row['id']
            if aid:
                c.execute("INSERT OR IGNORE INTO versions(asset_id,sha256,size,reason) VALUES(?,?,?,'snapshot')", (aid, *previous))
            else:
                name = PurePosixPath(row['path']).name
                ext = PurePosixPath(name).suffix.lower()
                aid = c.execute("INSERT INTO assets(project_id,relpath,name,ext,kind,size,mtime,git_state,managed,present) VALUES(?,?,?,?,?,?,?,'managed',1,0)",
                                (plan['project_id'], row['path'], name, ext, core.EXT_KIND.get(ext, 'other'), row['size'], time.time())).lastrowid
            c.execute('INSERT INTO versions(asset_id,sha256,size,reason) VALUES(?,?,?,?)', (aid, row['sha256'], row['size'], 'update' if row['id'] else 'upload'))
        db.audit(c, 'engineering_upload', {'model_id': mid, 'plan_id': pid, 'files': changed})
    shutil.rmtree(folder)
    models.catalog(True)
    return {'updated': len(stored), 'unchanged': len(plan['files']) - len(stored)}
