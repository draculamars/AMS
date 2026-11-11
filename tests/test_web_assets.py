import hashlib
import json
from pathlib import Path
import pytest
from test_assets import env,client,HEADERS
from asset_manager import db,core,web_assets

def fixture_bundle(env):
    (env/'index.html').write_text('<title>固定网页</title><script src="view.js"></script><img src="images/a.svg">')
    (env/'view.js').write_text('document.body.dataset.loaded="yes"')
    (env/'images').mkdir();(env/'images/a.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    core.scan_project('p');web_assets.discover();files={}
    with db.connect() as c:assets=list(c.execute('SELECT id,relpath FROM assets'))
    for a in assets:
        r=core.archive_asset(a['id']);path=db.blob_root()/r['sha256'][:2]/r['sha256']
        files[a['relpath']]={'sha256':r['sha256'],'size':path.stat().st_size}
    return web_assets.publish('p',files)

def test_publications_are_immutable_and_relocatable(env,client,tmp_path,monkeypatch):
    first=fixture_bundle(env);entry=web_assets.listing()['items'][0]
    assert entry['body']['issues']==0
    assert len(entry['publications'])==1
    (env/'index.html').write_text('<title>后来</title>')
    ticket=web_assets.preview_ticket(entry['publications'][0]['id'])
    response=client.get(ticket['url']);assert '固定网页' in response.text and response.status_code==200
    assert 'sandbox allow-scripts' in response.headers['content-security-policy']
    assert 'allow-same-origin' not in response.headers['content-security-policy']
    assert response.headers['access-control-allow-origin']=='*'
    assert web_assets.verify(first['bundle_id'])['status']=='passed'
    # No original path fallback when a stored object is absent.
    m=web_assets.manifest(str(db.DATA),first['bundle_id']);sha=m['files']['index.html']['sha256']
    (db.blob_root()/sha[:2]/sha).unlink()
    assert client.get(ticket['url']).status_code==400

def test_preview_capabilities_do_not_grant_admin_access(env,client,monkeypatch):
    fixture_bundle(env);pub=web_assets.listing()['items'][0]['publications'][0]['id']
    ticket=web_assets.preview_ticket(pub)
    monkeypatch.setenv('ASSET_LOCAL_BOOTSTRAP','0')
    assert client.get('/api/web-assets').status_code==401
    assert client.post('/api/web-publications/'+pub+'/preview',headers=HEADERS,json={}).status_code==401
    assert client.get(ticket['url'],headers={'Origin':'null'}).status_code==200
    assert client.get(ticket['url'].replace('/index.html','/absent')).status_code==404
    assert client.get(ticket['url'].replace('/api/web-preview/','/api/web-preview/0.')).status_code==400
    assert client.post('/api/scan',headers={**HEADERS,'Origin':'null'},json={}).status_code==403

def test_publish_rejects_paths_and_missing_content(env,client):
    with pytest.raises(ValueError):web_assets.publish('p',{'../outside':{'sha256':'a'*64,'size':1}})
    with pytest.raises(ValueError):web_assets.publish('p',{'index.html':{'sha256':'a'*64,'size':1}})
    assert web_assets.listing()['total']==0

def test_analysis_exposes_dependencies_and_never_executes(env,client):
    (env/'report.html').write_text('<title>来源</title><script src="/api/run"></script><img src="missing.png"><img src="https://example.invalid/a.png"><a href="../other.html">链接</a>')
    core.scan_project('p');web_assets.discover();r=web_assets.listing()['items'][0]
    assert r['body']['issues']==2 and r['body']['external_resources']==1
    assert r['publications']==[]
    assert not list(env.glob('*.executed'))

def test_backup_restores_web_only_objects_and_expired_ticket_fails(env,client,tmp_path,monkeypatch):
    import os,sqlite3,subprocess,sys
    fixture_bundle(env)
    raw=b'<title>Generated publication</title>'
    sha=hashlib.sha256(raw).hexdigest();path=db.blob_root()/sha[:2]/sha
    path.parent.mkdir(exist_ok=True);path.write_bytes(raw)
    bundle=web_assets.publish('p',{'index.html':{'sha256':sha,'size':len(raw)}})
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM versions WHERE sha256=?',(sha,)).fetchone()
        pub=c.execute('SELECT id FROM web_publications WHERE bundle_id=?',(bundle['bundle_id'],)).fetchone()[0]
    ticket=web_assets.preview_ticket(pub)
    target=tmp_path/'backup'
    r=subprocess.run([sys.executable,'-m','asset_manager','backup',str(target)],env={**os.environ,'ASSET_DATA_DIR':str(db.DATA)},capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    monkeypatch.setattr(db,'DATA',target)
    assert web_assets.verify(bundle['bundle_id'])['status']=='passed'
    assert client.get(ticket['url']).content==raw
    monkeypatch.setattr(web_assets.time,'time',lambda:10**12)
    assert client.get(ticket['url']).status_code==400

def test_browser_evidence_is_explicit_and_scoped(env,client):
    fixture_bundle(env);pub=web_assets.listing()['items'][0]['publications'][0]['id']
    with pytest.raises(ValueError):web_assets.record_browser_verification(pub,{'status':'passed'})
    web_assets.record_browser_verification(pub,{'status':'passed','checks':['module import','JSON fetch'],'tested_url':'http://localhost/api/web-preview/secret/bundle/index.html'})
    body=web_assets.listing()['items'][0]['publications'][0]['body']
    assert body['verification']=='browser_verified'
    assert 'secret' not in body['browser_evidence']['tested_url']
