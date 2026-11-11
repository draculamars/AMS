import hashlib
import json
import subprocess
import sys
from pathlib import Path


def test_model_adapter_preserves_sources_and_includes_shared_pages(tmp_path):
    root=tmp_path/'source';web=root/'model_library';web.mkdir(parents=True)
    (root/'docs').mkdir();(root/'docs/lineage.html').write_text('<h1>History</h1>')
    (root/'mesh.stl').write_text('solid fixture\nendsolid')
    index='<a href="/files/docs/lineage.html" target="_blank">History</a><script type="module" src="/model_library/app.js"></script>'
    script="fetch('/api/catalog'); const url='/api/zip?kind=pure&id=';"
    (web/'index.html').write_text(index);(web/'app.js').write_text(script)
    (web/'catalog.json').write_text(json.dumps({'generated':'fixture','versions':[{'project':'fixture','files':[{'path':'mesh.stl'}]}]}))
    out=tmp_path/'publication'
    run=subprocess.run([sys.executable,'scripts/freeze-model-library.py','--root',str(root),'--project','fixture','--output',str(out)],capture_output=True,text=True,cwd=Path(__file__).parents[1])
    assert run.returncode==0,run.stderr
    m=json.loads((out/'publication.json').read_text());prefix='web-publications/model-library/'
    assert prefix+'files/docs/lineage.html' in m['files']
    assert m['files'][prefix+'source/model_library/index.html']['sha256']==hashlib.sha256(index.encode()).hexdigest()
    adapted=(out/'generated/index.html').read_text()
    assert './files/docs/lineage.html' in adapted and 'target="_self"' in adapted
    assert "fetch('./catalog.json')" in (out/'generated/app.js').read_text()
    assert 'aria-disabled' in (out/'generated/app.js').read_text()
    assert (web/'index.html').read_text()==index and (web/'app.js').read_text()==script
    # Symlinks need an explicit dependency review, even within the source root.
    (root/'mesh.stl').unlink();(root/'mesh.stl').symlink_to(root/'docs/lineage.html')
    run=subprocess.run([sys.executable,'scripts/freeze-model-library.py','--root',str(root),'--project','fixture','--output',str(tmp_path/'rejected')],capture_output=True,text=True,cwd=Path(__file__).parents[1])
    assert run.returncode!=0 and '软链接' in run.stderr
