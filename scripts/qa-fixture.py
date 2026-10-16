"""Create an isolated fixture; runtime path must explicitly point outside real data."""
import os
import subprocess
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asset_manager import db,core
assert 'atlas-qa-' in str(db.DATA), 'QA requires a temporary atlas-qa-* directory'
db.init(); root=db.DATA.parent/'fixture-project'; root.mkdir(exist_ok=True)
(root/'验收说明.md').write_text('# 验收说明\n\n浏览器测试专用，不包含用户资产。\n')
(root/'模型.stl').write_text('solid test\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 10 0 0\nvertex 0 10 0\nendloop\nendfacet\nendsolid\n')
subprocess.run(['git','init','-b','main',str(root)],check=True,capture_output=True)
core.import_projects({'projects':[{'projectId':'qa','label':'浏览器验收项目','path':str(root)}]});core.scan_project('qa')
