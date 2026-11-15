"""Refresh uploaded content indexes without scanning or writing source projects."""
import hashlib
import json
import posixpath
from xml.etree.ElementTree import ParseError
from . import core, db, models, portable, project_index as index


def refresh(asset_ids):
    if not portable.enabled():
        raise ValueError('此操作仅更新随行库内已保存的内容')
    ids = list(dict.fromkeys(asset_ids))
    if not ids or len(ids) > 5000 or any(type(x) is not int or x < 1 for x in ids):
        raise ValueError('资产列表须包含 1–5000 个有效编号')
    assets = [core.asset_row(aid) for aid in ids]
    with db.connect() as c:
        projects = [dict(p) for p in c.execute('SELECT id,path FROM projects')]
        lookup = {(r['project_id'], r['relpath']): r['id'] for r in c.execute(
            'SELECT project_id,relpath,id FROM assets a WHERE ' + models.available_sql())}
    dirs = set()
    for pid, path in lookup:
        parent = posixpath.dirname(path)
        while parent and (pid, parent) not in dirs:
            dirs.add((pid, parent))
            parent = posixpath.dirname(parent)
    links, documents, issues = [], [], []
    for a in assets:
        if a['ext'] not in index.PARSE_EXTS:
            continue
        try:
            path, _ = core.source_path(a['id'])
            with path.open('rb') as f:
                raw = f.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024 or b'\0' in raw[:8192]:
                issues.append({'asset_id': a['id'], 'detail': '内容超过解析上限或为二进制，仅保留文件版本'})
                continue
            text = raw.decode('utf-8')
            refs = index.text_references(a, text)
            if a['ext'] in models.PARSABLE:
                refs += models.references(a)
            if a['ext'] in index.DOC_EXTS:
                title = next((s.lstrip('# ').strip() for s in text.splitlines() if s.startswith('#')), a['name'])[:250]
                documents.append((a['project_id'], a['relpath'], title, text[:16384], int(len(text) > 16384)))
            for ref, kind in refs:
                pid, target, status = index.resolve(a['project_id'], a['relpath'], ref, lookup, projects, dirs)
                status = models.reference_status(status, kind)
                if status == 'anchor' or (kind == 'imports' and status == 'missing'):
                    continue
                key = hashlib.sha256(json.dumps([a['project_id'], a['relpath'], ref, kind], ensure_ascii=False).encode()).hexdigest()[:32]
                links.append((key, a['project_id'], a['relpath'], pid, target, ref, kind, status, '随行库当前版本解析', 0))
        except (OSError, ValueError, ParseError) as error:
            issues.append({'asset_id': a['id'], 'detail': str(error)[:300]})
    affected = {a['project_id'] for a in assets}
    repaired = 0
    with db.connect() as c:
        for a in assets:
            # Repair metadata left by older portable uploads; historical index
            # snapshots and version sizes are retained independently.
            current = c.execute("SELECT size FROM versions WHERE asset_id=? AND reason IN ('upload','update') ORDER BY id DESC LIMIT 1", (a['id'],)).fetchone()
            if current:
                c.execute('UPDATE assets SET size=? WHERE id=?', (current[0], a['id']))
            # Old generated links must not masquerade as the new content.
            c.execute('DELETE FROM project_relations WHERE source_project=? AND source_path=? AND manual=0', (a['project_id'], a['relpath']))
            c.execute('DELETE FROM project_documents WHERE project_id=? AND path=?', (a['project_id'], a['relpath']))
        c.executemany('INSERT OR IGNORE INTO project_relations VALUES(?,?,?,?,?,?,?,?,?,?)', links)
        c.executemany('INSERT OR REPLACE INTO project_documents VALUES(?,?,?,?,?)', documents)
        # Added files can satisfy previously missing incoming references too.
        for pid in affected:
            for row in c.execute("SELECT * FROM project_relations WHERE source_project=? AND manual=0 AND status IN ('missing','output_unarchived','temporary_unarchived')", (pid,)).fetchall():
                target_pid, target, status = index.resolve(pid, row['source_path'], row['reference'], lookup, projects, dirs)
                if target is not None:
                    c.execute('UPDATE project_relations SET target_project=?,target_path=?,status=?,evidence=? WHERE id=?',
                              (target_pid, target, status, '随行库当前内容重新解析', row['id']))
                    repaired += 1
        result = {'status': 'partial' if issues else 'complete', 'assets': len(ids),
                  'relations': len(links), 'documents': len(documents), 'incoming_resolved': repaired, 'issues': issues}
        db.audit(c, 'library_index_refresh', {**result, 'asset_ids': ids})
    return result


def after_upload(asset_ids):
    if not portable.enabled():
        return {}
    try:
        return {'index_refresh': refresh(asset_ids)}
    except Exception as error:
        # Versions have already committed; report this distinct failure honestly.
        result = {'status': 'failed', 'detail': str(error)[:500]}
        with db.connect() as c:
            db.audit(c, 'library_index_refresh', {**result, 'asset_ids': asset_ids})
        return {'index_refresh': result}


def refresh_job(progress, asset_ids):
    progress(0, 1, '更新盘内版本的关联与文档索引')
    result = refresh(asset_ids)
    progress(1, 1, '盘内索引已刷新' if result['status'] == 'complete' else '盘内索引已刷新，部分内容超出解析范围')
    return result
