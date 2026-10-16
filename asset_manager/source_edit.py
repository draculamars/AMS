"""Reviewable text edits to a node-local workspace, with backups and conflict checks."""
import difflib
import hashlib
import io
import json
import os
import tempfile
import uuid
from pathlib import Path
from . import core, db, node

MAX_BYTES=512*1024

def source(aid):
    a=core.asset_row(aid)
    if not node.location(a['project_id'])['local_available'] or a['managed'] or not a['present']:
        raise ValueError('此文件不是本节点在线工作文件；可通过上传保存资产库版本')
    path=core.safe_path(a['project_path'],a['relpath'])
    if core.secret(path) or path.stat().st_size>MAX_BYTES:raise ValueError('此文件不支持在线文本编辑')
    raw=path.read_bytes()
    if b'\0' in raw:raise ValueError('二进制文件请使用原生工具编辑后同步')
    text=raw.decode('utf-8')
    return path,a,raw,text

def read(aid):
    _,a,raw,text=source(aid)
    return {'asset_id':aid,'path':a['relpath'],'text':text,'sha256':hashlib.sha256(raw).hexdigest(),'target':'workspace'}

def plan(aid,body):
    path,a,raw,old=source(aid);new=str(body.get('text',''));encoded=new.encode('utf-8')
    before=hashlib.sha256(raw).hexdigest()
    if before!=body.get('sha256'):raise ValueError('原文件已变化，请重新读取后编辑')
    if len(encoded)>MAX_BYTES or '\0' in new:raise ValueError('编辑内容超过文本限制')
    if encoded==raw:raise ValueError('内容没有变化')
    core.storage_preflight(len(raw)+len(encoded))
    after,size=core.store_blob(io.BytesIO(encoded));ident=uuid.uuid4().hex
    diff=''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile=a['relpath'],tofile=a['relpath']))
    detail={'path':str(path),'note':str(body.get('note','在线编辑'))[:1000],'size':size,'diff':diff[:50000],'diff_truncated':len(diff)>50000}
    with db.connect() as c:c.execute('INSERT INTO source_edit_plans VALUES(?,?,?,?,?,?,?)',(ident,aid,before,after,json.dumps(detail,ensure_ascii=False),'pending',core.now()))
    return {'id':ident,'asset_id':aid,'before_sha':before,'after_sha':after,**detail}

def apply(aid,body):
    if body.get('acknowledgement')!='确认写入本节点原文件':raise ValueError('请确认写入本节点原文件')
    with core.MUTATION_LOCK:
        with db.connect() as c:r=c.execute('SELECT * FROM source_edit_plans WHERE id=? AND asset_id=?',(body.get('plan_id'),aid)).fetchone()
        if not r or r['status']!='pending':raise ValueError('编辑方案不存在或已经执行')
        path,a,raw,_=source(aid);detail=json.loads(r['body']);st=path.stat()
        if str(path)!=detail['path'] or hashlib.sha256(raw).hexdigest()!=r['before_sha']:raise ValueError('原文件已变化，编辑方案失效；请重新生成差异')
        incoming=db.blob_root()/r['after_sha'][:2]/r['after_sha'];new=incoming.read_bytes()
        if hashlib.sha256(new).hexdigest()!=r['after_sha']:raise ValueError('编辑内容校验失败')
        sha,size=core.store_blob(io.BytesIO(raw))
        with db.connect() as c:c.execute('INSERT INTO versions(asset_id,sha256,size,reason) VALUES(?,?,?,?)',(aid,sha,size,'before-source-edit'))
        fd,tmp=tempfile.mkstemp(prefix='.ams-edit-',dir=path.parent)
        try:
            with os.fdopen(fd,'wb') as f:f.write(new);f.flush();os.fsync(f.fileno())
            os.chmod(tmp,st.st_mode & 0o777)
            checked=core.safe_path(a['project_path'],a['relpath'])
            if checked!=path or hashlib.sha256(path.read_bytes()).hexdigest()!=sha:raise ValueError('保存前检测到外部修改，已保留备份，请重新编辑')
            os.replace(tmp,path)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
        with db.connect() as c:
            c.execute("UPDATE source_edit_plans SET status='applied' WHERE id=?",(r['id'],))
            c.execute('INSERT INTO versions(asset_id,sha256,size,reason) VALUES(?,?,?,?)',(aid,r['after_sha'],len(new),'source-edit'))
            c.execute('UPDATE assets SET size=?,mtime=? WHERE id=?',(len(new),path.stat().st_mtime,aid))
            db.audit(c,'source_edit',{'asset_id':aid,'path':a['relpath'],'note':detail['note'],'before':sha,'after':r['after_sha']})
        with db.connect() as c:c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('watch_dirty:'+a['project_id'],'1'))
        return {'applied':True,'index_pending':True,'backup_sha256':sha,'sha256':r['after_sha']}
