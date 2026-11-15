import io
import json
import pytest
from asset_manager import core, db, models, project_index, library_index
from test_assets import env, client, HEADERS
from test_models import put, zip_bytes


def freeze(root, monkeypatch):
    project_index.index_one('p')
    with db.connect() as c:
        rows = {r['relpath']: dict(r) for r in c.execute('SELECT * FROM assets')}
    for a in rows.values():
        core.archive_asset(a['id'])
    with db.connect() as c:
        c.execute('UPDATE assets SET present=0')
    monkeypatch.setenv('ATLAS_PORTABLE_ROOT', str(root))
    monkeypatch.setattr(core, 'safe_path', lambda *a: pytest.fail('must not read the host source'))
    return rows


def test_upload_refreshes_documents_links_preserves_manual_and_history(env, client, monkeypatch):
    original = '# Old title\n[old](old.stl)\n'
    put(env, 'README.md', original)
    put(env, 'old.stl', 'solid old')
    put(env, 'new.stl', 'solid new')
    rows = freeze(env, monkeypatch)
    aid = rows['README.md']['id']
    project_index.add_relation('p', {'source_id': aid, 'target_id': rows['old.stl']['id']})
    r = client.post('/api/upload', headers=HEADERS, data={'project_id': 'p', 'asset_id': aid},
                    files={'file': ('README.md', '# New title\n[new](new.stl)\nneedlecontent')})
    assert r.status_code == 200 and r.json()['index_refresh']['status'] == 'complete', r.text
    links = project_index.relations('p')['items']
    assert any(x['manual'] and x['target_path'] == 'old.stl' for x in links)
    assert {x['target_path'] for x in links if not x['manual']} == {'new.stl'}
    assert project_index.documents('p', 'needlecontent')['total'] == 1
    assert core.asset_row(aid)['size'] == len(b'# New title\n[new](new.stl)\nneedlecontent')
    versions = client.get(f'/api/assets/{aid}').json()['versions']
    assert client.get(f'/api/assets/{aid}/file?version={versions[-1]["id"]}').text == original
    assert (env/'README.md').read_text() == original


def test_engineering_upload_resolves_incoming_links_and_retry_is_idempotent(env, client, monkeypatch):
    put(env, 'robot/main.urdf', '<robot><mesh filename="meshes/new.stl"/></robot>')
    rows = freeze(env, monkeypatch)
    mid = models.list_projects()['items'][0]['id']
    url = '/api/model-projects/'+mid
    assert project_index.relations('p')['items'][0]['status'] == 'missing'
    files = {'meshes/new.stl': 'solid new', 'README.md': '# Portable documentation\nnewlyindexed'}
    for expected in [2, 0]:
        plan = client.post(url+'/upload-plan', headers=HEADERS, files={'file': ('update.zip', zip_bytes(files))}).json()
        result = client.post(url+'/upload-apply', headers=HEADERS, json={'plan_id': plan['id']})
        assert result.status_code == 200, result.text
        assert result.json()['updated'] == expected
        assert result.json()['index_refresh']['status'] == 'complete'
    links = project_index.relations('p')['items']
    assert len(links) == 1 and links[0]['status'] == 'resolved'
    assert links[0]['target_path'] == 'robot/meshes/new.stl'
    assert project_index.documents('p', 'newlyindexed')['total'] == 1
    assert not (env/'robot/meshes/new.stl').exists()


def test_index_failure_is_distinct_from_saved_upload(env, client, monkeypatch):
    monkeypatch.setenv('ATLAS_PORTABLE_ROOT', str(env))
    def broken(ids):
        raise OSError('synthetic index failure')
    monkeypatch.setattr(library_index, 'refresh', broken)
    r = client.post('/api/upload', headers=HEADERS, data={'project_id': 'p'}, files={'file': ('saved.txt', b'saved bytes')})
    assert r.status_code == 200
    assert r.json()['index_refresh']['status'] == 'failed'
    assert client.get(f'/api/assets/{r.json()["asset_id"]}/file').content == b'saved bytes'
    with db.connect() as c:
        audit = c.execute("SELECT detail FROM audits WHERE action='library_index_refresh'").fetchone()
        assert json.loads(audit[0])['status'] == 'failed'


def test_static_tuple_loops_and_trimesh_inputs_are_not_executed(env):
    from asset_manager.engineering_refs import script_references
    script = """from pathlib import Path
P=Path(__file__).resolve().parents[2]
for side,sign in [('L',-1),('R',1)]:
    path=P/f'references/Base_{side}.STL'
    mesh=trimesh.load(path)
for source,name,role in [(P/'references/source.STEP','copy.STEP','original')]:
    shutil.copy2(source,P/name)
(P/'NEVER_EXECUTE.txt').write_text('do not execute')
"""
    refs, dynamic = script_references({'relpath':'robot/scripts/audit.py','project_id':'p'}, script)
    assert ('project://references/Base_L.STL', 'build_input') in refs
    assert ('project://references/Base_R.STL', 'build_input') in refs
    assert ('project://references/source.STEP', 'build_input') in refs
    assert ('project://copy.STEP', 'build_output') in refs
    assert not (env/'NEVER_EXECUTE.txt').exists()


def test_missing_inputs_are_distinct_from_declared_outputs_and_temporaries(env, client, monkeypatch):
    put(env, 'robot/main.urdf', '<robot/>')
    put(env, 'robot/build.py', '''from pathlib import Path
P=Path(__file__).parent
(P/'required.tmp').read_bytes()
(P/'required.tmp').unlink()
(P/'result.step').write_bytes(b'output')
back=P/'readback.tmp'
call('download', '--output', str(back))
back.read_bytes()
back.unlink()
''')
    # A different consumer still needs the temporary file as a real input.
    put(env, 'robot/consumer.py', "from pathlib import Path\n(Path(__file__).parent/'readback.tmp').read_bytes()")
    rows = freeze(env, monkeypatch)
    mid = models.list_projects()['items'][0]['id']
    d = models.detail(mid)
    assert d['missing'] == 2
    assert d['unarchived_outputs'] == 1 and d['unarchived_intermediates'] == 1
    assert len(d['links']) == 4
    expected = {('robot/build.py', 'project://robot/required.tmp'): 'missing',
                ('robot/build.py', 'project://robot/result.step'): 'output_unarchived',
                ('robot/build.py', 'project://robot/readback.tmp'): 'temporary_unarchived',
                ('robot/consumer.py', 'project://robot/readback.tmp'): 'missing'}
    paths = {a['id']: a['relpath'] for a in d['files']}
    assert {(paths[r['source']], r['reference']): r['status'] for r in d['links']} == expected
    result = library_index.refresh([a['id'] for a in rows.values()])
    assert result['status'] == 'complete'
    assert {(r['source_path'], r['reference']): r['status'] for r in project_index.relations('p')['items']} == expected
    snap = models.snapshot(lambda *a: None, mid)
    assert models.get_snapshot(mid, snap['snapshot_id'])['links'] == d['links']
    assert not (env/'robot/result.step').exists()
    url = '/api/model-projects/'+mid
    plan = client.post(url+'/upload-plan', headers=HEADERS, files={'file': ('update.zip', zip_bytes({'result.step': 'new output'}))}).json()
    r = client.post(url+'/upload-apply', headers=HEADERS, json={'plan_id': plan['id']})
    assert r.status_code == 200, r.text
    assert models.detail(mid)['unarchived_outputs'] == 0
    assert next(r for r in project_index.relations('p')['items'] if r['reference'].endswith('result.step'))['status'] == 'resolved'


def test_bundle_size_uses_actual_current_bytes_and_refresh_repairs_old_catalog(env, client, monkeypatch):
    import zipfile
    put(env, 'robot/main.urdf', '<robot/>')
    put(env, 'robot/README.md', 'old')
    rows = freeze(env, monkeypatch)
    mid = models.list_projects()['items'][0]['id']
    before = models.snapshot(lambda *a: None, mid)
    content = '# updated document with a different size\n'
    url = '/api/model-projects/'+mid
    plan = client.post(url+'/upload-plan', headers=HEADERS, files={'file': ('update.zip', zip_bytes({'README.md': content}))}).json()
    result = client.post(url+'/upload-apply', headers=HEADERS, json={'plan_id': plan['id']})
    assert result.status_code == 200, result.text
    aid = rows['robot/README.md']['id']
    assert core.asset_row(aid)['size'] == len(content.encode())
    with db.connect() as c:
        c.execute('UPDATE assets SET size=3 WHERE id=?', (aid,))  # older deployment
    bundle, _ = models.bundle(mid)
    try:
        with zipfile.ZipFile(bundle) as z:
            manifest = json.loads(z.read('ATLAS-ENGINEERING-MANIFEST.json'))
            for f in manifest['files']:
                assert f['size'] == z.getinfo('project/'+f['path']).file_size
    finally:
        __import__('os').unlink(bundle)
    library_index.refresh([aid])
    assert core.asset_row(aid)['size'] == len(content.encode())
    old = next(f for f in models.get_snapshot(mid, before['snapshot_id'])['files'] if f['id'] == aid)
    assert old['size'] == 3 and (env/'robot/README.md').read_text() == 'old'
