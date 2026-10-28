"""Explicit removable-disk mode. Normal installations retain their existing behavior."""
import json
import os
import re
from pathlib import Path


def storage_probe(root, delay=2):
    """Fail before export when removable writes vanish or change after close."""
    import hashlib
    import tempfile
    import time
    folder=Path(root)/'build'
    folder.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='atlas-storage-probe-',dir=folder)
    path=Path(name)
    content=bytes(range(256))*4096
    try:
        with os.fdopen(fd,'wb') as out:
            out.write(content);out.flush();os.fsync(out.fileno())
        time.sleep(delay)
        if not path.is_file() or path.read_bytes()!=content:
            raise ValueError('目标盘写入持久性验证失败：测试文件在关闭后消失或变化。请检查磁盘、挂载及终端安全策略，未开始复制资产。')
        return {'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest(),'delay_seconds':delay,'passed':True}
    finally:
        path.unlink(missing_ok=True)


def enabled():
    return bool(os.environ.get('ATLAS_PORTABLE_ROOT'))


def request_allowed(method, path):
    # Vault plans may inspect host paths even though their HTTP method is GET.
    if re.fullmatch(r'/api/vault/profiles/[^/]+/plan', path):
        return False
    if re.fullmatch(r'/api/assets/\d+/source-edit', path):
        return False
    if method in {'GET', 'HEAD', 'OPTIONS'}:
        return True
    if path in {'/api/auth/login', '/api/auth/logout', '/api/auth/setup', '/api/upload', '/api/archive', '/api/portable/stop'}:
        return method == 'POST'
    allowed = {
        'PATCH': [r'/api/assets/\d+', r'/api/model-projects/[^/]+'],
        'POST': [r'/api/project-preferences/[^/]+', r'/api/engineering/[^/]+/relations',
                 r'/api/model-projects/[^/]+/(relations|snapshots|upload-plan|upload-apply)',
                 r'/api/assets/\d+/model-preview'],
    }
    return any(re.fullmatch(pattern, path) for pattern in allowed.get(method, []))


def status():
    if not enabled():
        return {'enabled': False}
    root = Path(os.environ['ATLAS_PORTABLE_ROOT'])
    config = json.loads((root / 'portable.json').read_text())
    return {'enabled': True, 'label': 'Atlas 随行资产库', 'root': str(root),
            'dataset': config['active_dataset'], 'captured_at': config.get('captured_at'),
            'scope': config.get('coverage', {}), 'offline': True,
            'notice': '当前查看磁盘上的独立资产副本。上传、备注和版本保存在此盘；原项目路径仅表示来源。'}
