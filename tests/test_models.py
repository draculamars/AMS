import io
import json
import hashlib
import zipfile
from pathlib import Path
import pytest
from asset_manager import core,db,models
from test_assets import env,client,HEADERS

def put(root,path,data):
    p=root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data.encode() if isinstance(data,str) else data)

def index(root):
    core.scan_project('p');models.catalog(True)
    with db.connect() as c:return {r['relpath']:dict(r) for r in c.execute('SELECT * FROM assets')}

def test_engineering_groups_preserve_revision_roots_and_native_types(env,client):
    for version in ['v1','v2']:
        put(env,f'artifacts/gripper_{version}/design.blend',b'BLENDER')
        put(env,f'artifacts/gripper_{version}/cad/part.step','ISO-10303-21;')
        put(env,f'artifacts/gripper_{version}/stl/part.stl','solid test\nendsolid')
    put(env,'native/arm.SLDASM',b'assembly')
    put(env,'artifacts/build/App.app/Contents/Resources/a.stl','solid test\nendsolid')
    rows=index(env);assert rows['native/arm.SLDASM']['kind']=='model'
    groups=client.get('/api/model-projects').json()
    assert len(groups['items'])==3 and groups['runtime_count']==1
    g=next(g for g in groups['items'] if g['root']=='artifacts/gripper_v1')
    d=client.get('/api/model-projects/'+g['id']).json()
    assert len(d['files'])==3 and d['entry']['ext']=='.blend'
    assert all('/gripper_v1/' in f['relpath'] for f in d['files'])
    assert d['associations'][0]['status']=='inferred'

def test_urdf_dependency_closure_materials_and_missing_are_explicit(env,client):
    put(env,'robot/urdf/main.urdf','<robot name="test"><link name="a"><visual><geometry><mesh filename="../meshes/a.obj"/></geometry></visual><visual><geometry><mesh filename="../missing.stl"/></geometry></visual></link></robot>')
    put(env,'robot/meshes/a.obj','mtllib paint.mtl\nv 0 0 0\n')
    put(env,'robot/meshes/paint.mtl','newmtl paint\nmap_Kd ../../shared/color.png\n')
    put(env,'shared/color.png',b'png')
    rows=index(env);g=next(g for g in models.list_projects()['items'] if g['root']=='robot')
    d=models.detail(g['id']);assert d['missing']==1
    assert {x['reference'] for x in d['links']}=={'../meshes/a.obj','../missing.stl','paint.mtl','../../shared/color.png'}
    assert any(a['relpath']=='shared/color.png' for a in d['files'])
    assert models.resolve_reference('p','robot/main.urdf','../../outside.key')[0] is None
    assert models.resolve_reference('p','robot/main.urdf','https://external.invalid/a.stl')[1]=='external'
    assert models.resolve_reference('p','robot/main.urdf','${prefix}/a.stl')[1]=='parameterized'
    with pytest.raises(ValueError):models.references({'id':rows['robot/urdf/main.urdf']['id'],'ext':'.glb'})

def test_package_references_keep_their_original_package_context(env):
    for p in ['src/arm_description','build/arm_description']:
        put(env,p+'/urdf/arm.urdf','<robot><link><visual><geometry><mesh filename="package://arm_description/meshes/part.stl"/></geometry></visual></link></robot>')
        put(env,p+'/meshes/part.stl',p)
    rows=index(env)
    target,method=models.resolve_reference('p','src/arm_description/urdf/arm.urdf','package://arm_description/meshes/part.stl')
    assert target['id']==rows['src/arm_description/meshes/part.stl']['id']

def test_engineering_snapshot_pins_versions_and_download_keeps_references(env,client):
    put(env,'robot/urdf/main.urdf','<robot><mesh filename="../../shared/part.stl"/></robot>')
    put(env,'shared/part.stl','original geometry');put(env,'robot/parameters.json','{"length":1}')
    rows=index(env);g=next(g for g in models.list_projects()['items'] if g['root']=='robot')
    result=models.snapshot(lambda *args:None,g['id']);sid=result['snapshot_id']
    core.upload('p','part.stl',io.BytesIO(b'changed geometry'),rows['shared/part.stl']['id'])
    r=client.get(f'/api/model-projects/{g["id"]}/bundle?snapshot={sid}')
    assert r.status_code==200,r.text
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert z.read('project/shared/part.stl')==b'original geometry'
        manifest=json.loads(z.read('ATLAS-ENGINEERING-MANIFEST.json'))
        assert manifest['root']=='robot' and manifest['links'][0]['target']==rows['shared/part.stl']['id']
        for a in manifest['files']:assert hashlib.sha256(z.read('project/'+a['path'])).hexdigest()==a['sha256']
    assert not list((db.DATA/'tmp').glob('*.zip'))

def test_manual_relationship_is_explicit_and_validates_scope(env,client):
    put(env,'artifacts/a/a.stl','solid a');put(env,'artifacts/b/b.stl','solid b')
    put(env,'shared/settings.json','{}');rows=index(env);g=next(g for g in models.list_projects()['items'] if g['root']=='artifacts/a')
    assert client.post(f'/api/model-projects/{g["id"]}/relations',headers=HEADERS,json={'source_id':rows['artifacts/b/b.stl']['id'],'target_id':rows['shared/settings.json']['id']}).status_code==400
    assert client.post(f'/api/model-projects/{g["id"]}/relations',headers=HEADERS,json={'source_id':rows['artifacts/a/a.stl']['id'],'target_id':rows['shared/settings.json']['id'],'relation':'depends_on'}).status_code==200
    d=models.detail(g['id']);assert d['links'][0]['status']=='confirmed' and len(d['files'])==2

def test_historical_model_context_reads_the_selected_root_version(env):
    put(env,'robot/model.gltf',json.dumps({'asset':{'version':'2.0'},'buffers':[{'uri':'old.bin'}]}));put(env,'robot/old.bin',b'old');put(env,'robot/new.bin',b'new');rows=index(env);aid=rows['robot/model.gltf']['id'];core.archive_asset(aid)
    with db.connect() as c:version=c.execute('SELECT id FROM versions WHERE asset_id=?',(aid,)).fetchone()[0]
    core.upload('p','model.gltf',io.BytesIO(json.dumps({'asset':{'version':'2.0'},'buffers':[{'uri':'new.bin'}]}).encode()),aid)
    assert models.asset_context(aid,version)['links'][0]['reference']=='old.bin'
    assert models.asset_context(aid)['links'][0]['reference']=='new.bin'

def test_build_script_lineage_is_parsed_without_execution(env):
    put(env,'artifacts/hand_v1/cad/part.step','base CAD')
    put(env,'artifacts/hand_v1/stl/part.stl','base mesh')
    put(env,'artifacts/hand_v2/hand.blend','native')
    put(env,'artifacts/hand_v2/cad/build.py',"from pathlib import Path\nimport shutil\nP=Path(__file__).resolve().parents[1]\nB=P.parent/'hand_v1'\nshutil.copytree(B/'stl',P/'stl')\nfor f in (B/'cad').glob('*.step'):\n shutil.copy2(f,P/'cad'/f.name)\n(P/'DANGER.txt').write_text('never execute')\n")
    rows=index(env);g=next(g for g in models.list_projects()['items'] if g['root']=='artifacts/hand_v2');d=models.detail(g['id'])
    assert not (env/'artifacts/hand_v2/DANGER.txt').exists()
    assert {'artifacts/hand_v1/cad/part.step','artifacts/hand_v1/stl/part.stl'} <= {a['relpath'] for a in d['files']}
    assert d['related_projects'][0]['root']=='artifacts/hand_v1'
    assert any(r['relation']=='build_input' and r['target']==rows['artifacts/hand_v1/stl/part.stl']['id'] for r in d['links'])


def zip_bytes(files):
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as z:
        for name,value in files.items():z.writestr(name,value)
    buffer.seek(0);return buffer


def test_engineering_package_update_preserves_paths_history_and_atomic_conflict(env,client):
    put(env,'robot/cad/part.stl','original mesh');put(env,'robot/params.json','{"length":1}')
    rows=index(env);g=next(g for g in models.list_projects()['items'] if g['root']=='robot')
    url=f'/api/model-projects/{g["id"]}'
    package=zip_bytes({'cad/part.stl':'updated mesh','textures/color.png':b'color','params.json':'{"length":2}'})
    r=client.post(url+'/upload-plan',headers=HEADERS,files={'file':('robot.zip',package)})
    assert r.status_code==200,r.text
    plan=r.json();assert {f['path'] for f in plan['files']}=={'robot/cad/part.stl','robot/textures/color.png','robot/params.json'}
    assert (env/'robot/cad/part.stl').read_text()=='original mesh'
    r=client.post(url+'/upload-apply',headers=HEADERS,json={'plan_id':plan['id']})
    assert r.status_code==200,r.text
    assert r.json()['updated']==3
    assert core.source_path(rows['robot/cad/part.stl']['id'])[0].read_text()=='updated mesh'
    assert (env/'robot/cad/part.stl').read_text()=='original mesh'
    assert not (env/'robot/textures/color.png').exists()
    with db.connect() as c:
        history=c.execute('SELECT * FROM versions WHERE asset_id=? ORDER BY id',(rows['robot/cad/part.stl']['id'],)).fetchall()
        assert len(history)==2 and history[0]['reason']=='snapshot'
    assert core.source_path(rows['robot/cad/part.stl']['id'],history[0]['id'])[0].read_text()=='original mesh'
    package=zip_bytes({'cad/part.stl':'third mesh','params.json':'{"length":3}'})
    plan=client.post(url+'/upload-plan',headers=HEADERS,files={'file':('robot.zip',package)}).json()
    core.upload('p','params.json',io.BytesIO(b'{"length":99}'),rows['robot/params.json']['id'])
    r=client.post(url+'/upload-apply',headers=HEADERS,json={'plan_id':plan['id']})
    assert r.status_code==400 and '变化' in r.json()['detail']
    assert core.source_path(rows['robot/cad/part.stl']['id'])[0].read_text()=='updated mesh'


def test_package_rejects_traversal_duplicate_case_and_manifest_tampering(env,client):
    put(env,'robot/cad/a.stl','mesh');index(env);g=models.list_projects()['items'][0];url=f'/api/model-projects/{g["id"]}/upload-plan'
    for files in [{'../escape.stl':'x'},{'A.stl':'x','a.stl':'y'},{'nested/.git/config':'x'},{'nested/.env':'secret'}]:
        r=client.post(url,headers=HEADERS,files={'file':('bad.zip',zip_bytes(files))});assert r.status_code==400,r.text
    r=client.post(url,headers=HEADERS,files={'file':('config.zip',zip_bytes({'.gitignore':'*.tmp','settings/.project':'engineering metadata'}))})
    assert r.status_code==200,r.text
    assert {x['path'] for x in r.json()['files']}=={'robot/.gitignore','robot/settings/.project'}
    path,_=models.bundle(g['id'])
    with open(path,'rb') as f:
        r=client.post(url,headers=HEADERS,files={'file':('valid.zip',f)});assert r.status_code==200,r.text
    assert all(row['action']=='unchanged' for row in r.json()['files'])
    with zipfile.ZipFile(path) as z:files={name:z.read(name) for name in z.namelist()}
    files['project/robot/cad/a.stl']=b'tampered'
    r=client.post(url,headers=HEADERS,files={'file':('tampered.zip',zip_bytes(files))})
    assert r.status_code==400 and '校验' in r.json()['detail']
    Path(path).unlink()
