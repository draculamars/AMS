"""Preserve referenced pictures and recover explicit embedded model-input images.

Original rollouts remain unchanged. Recovered images are derived copies, and
different payloads for one old path are retained as ambiguous, never guessed.
"""
import base64
import copy
import hashlib
import html
import json
import os
import re
import sqlite3
import time
from pathlib import Path


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def references(database):
    import sys
    links = sys.modules.get('atlas_codex_links')
    if links is None:
        from . import codex_links as links
    found = {}
    with sqlite3.connect(Path(database).as_uri() + '?mode=rw', uri=True) as c:
        c.execute('PRAGMA query_only=ON')
        for kind, raw in c.execute("SELECT item_type,item_json FROM thread_items WHERE item_type IN ('userMessage','imageView','agentMessage')"):
            item = json.loads(raw)
            values = [item.get('path')] if kind == 'imageView' else [v.get('path') for v in item.get('content', []) if v.get('type') == 'localImage']
            if kind == 'agentMessage':
                values = [r['path'] for r in links.resources(item.get('text', '')) if r['media'] != 'file']
            for value in values:
                if isinstance(value, str) and Path(value).is_absolute():
                    found.setdefault(value, set()).add(kind)
    return [{'path': path, 'kinds': sorted(kinds)} for path, kinds in sorted(found.items())]


def relocate_item(item, source_home, target_home, aliases=None):
    if item.get('type') not in {'userMessage','imageView','fileChange','commandExecution','agentMessage'}:
        return copy.deepcopy(item)
    import sys
    history = sys.modules.get('atlas_codex_history')
    if history is None:
        from . import codex_history as history
    return history.mapped_item(item, source_home, target_home, aliases)


def embedded_images(record):
    """Only a named image wrapper directly paired with an input_image counts."""
    def messages(node):
        if isinstance(node, dict):
            if node.get('type') == 'message' and node.get('role') == 'user':
                yield node
            for value in node.values():
                if isinstance(value, (list, dict)):
                    yield from messages(value)
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (list, dict)):
                    yield from messages(value)
    for message in messages(record):
        pending = None
        for part in message.get('content', []):
            if part.get('type') == 'input_text':
                tags = re.findall(r'<image\b[^>]*\bpath="([^"]+)"[^>]*>', part.get('text', ''))
                pending = html.unescape(tags[-1]) if len(tags) == 1 else None
            elif part.get('type') == 'input_image' and pending:
                yield pending, part.get('image_url', '')
                pending = None


def recover(root, source_home, target_home, supplement_sha256=None, max_record_bytes=128 * 1024**2):
    root = Path(root)
    incoming, output = root / 'incoming', root / 'derived-attachments'
    output.mkdir(exist_ok=True, mode=0o700)
    refs = references(incoming / '.codex/thread_history_1.sqlite')
    aliases, files, available, recovered, ambiguous = {}, {}, set(), {}, set()
    supplement = root / 'evidence/attachment-inputs.json'
    if supplement_sha256:
        if not supplement.exists() or sha(supplement) != supplement_sha256:
            raise ValueError('补充附件清单校验不一致')
        for item in json.loads(supplement.read_text())['files']:
            name = item['name']
            if Path(name).name != name:
                raise ValueError('附件文件名越界')
            source = root / 'supplements/attachments' / name
            if sha(source) != item['sha256'] or source.stat().st_size != item['size']:
                raise ValueError('补充附件内容未通过校验')
            relative = '.codex/attachments/atlas-migrated/' + name
            aliases[item['source']] = str(Path(target_home) / relative)
            files[relative] = {'source': str(source), 'sha256': item['sha256'], 'kind': 'original_file'}
            available.add(item['source'])
    for ref in refs:
        path = Path(ref['path'])
        if path.is_relative_to(source_home) and (incoming / path.relative_to(source_home)).is_file():
            available.add(ref['path'])
    wanted = {r['path'] for r in refs} - available
    skipped, parsed, scanned = [], 0, 0
    rollouts = sorted(p for name in ['sessions', 'archived_sessions'] for p in (incoming / '.codex' / name).rglob('*.jsonl'))
    for rollout in rollouts:
        line_number = 0
        with rollout.open('rb') as f:
            while True:
                raw = f.readline(max_record_bytes + 1)
                if not raw:
                    break
                line_number += 1
                scanned += len(raw)
                if len(raw) > max_record_bytes:
                    skipped.append({'rollout': str(rollout.relative_to(incoming)), 'line': line_number, 'reason': 'record_size_limit'})
                    while raw and not raw.endswith(b'\n'):
                        raw = f.readline(1024 * 1024); scanned += len(raw)
                    continue
                if b'"input_image"' not in raw or b'<image' not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeError):
                    skipped.append({'rollout': str(rollout.relative_to(incoming)), 'line': line_number, 'reason': 'invalid_json'})
                    continue
                parsed += 1
                for original, uri in embedded_images(record):
                    if original not in wanted or not isinstance(uri, str) or not uri.startswith('data:image/'):
                        continue
                    header, separator, encoded = uri.partition(',')
                    extension = {'data:image/png;base64': '.png', 'data:image/jpeg;base64': '.jpg',
                                 'data:image/webp;base64': '.webp', 'data:image/gif;base64': '.gif'}.get(header)
                    if not extension or not separator:
                        continue
                    uri_hash = hashlib.sha256(uri.encode()).hexdigest()
                    if original in recovered and recovered[original]['uri_sha256'] == uri_hash:
                        continue
                    data = base64.b64decode(encoded, validate=True)
                    content_hash = hashlib.sha256(data).hexdigest()
                    name = hashlib.sha256(original.encode()).hexdigest()[:20] + '-' + content_hash[:20] + extension
                    path = output / name
                    if not path.exists():
                        with path.open('xb') as out:
                            out.write(data)
                        path.chmod(0o600)
                    relative = '.codex/attachments/atlas-recovered/' + name
                    files[relative] = {'source': str(path), 'sha256': content_hash, 'kind': 'embedded_model_input',
                        'original_path': original, 'rollout': str(rollout.relative_to(incoming)), 'line': line_number}
                    if original in recovered and recovered[original]['sha256'] != content_hash:
                        ambiguous.add(original)
                        aliases.pop(original, None)
                    if original not in ambiguous:
                        recovered[original] = {'uri_sha256': uri_hash, 'sha256': content_hash}
                        aliases[original] = str(Path(target_home) / relative)
                del record
        progress = {'rollout': str(rollout.relative_to(incoming)), 'bytes_scanned': scanned, 'recovered_paths': len(recovered),
                    'ambiguous_paths': len(ambiguous), 'updated_at': time.time()}
        temp = root / 'evidence/attachment-recovery-progress.tmp'
        temp.write_text(json.dumps(progress)); temp.chmod(0o600)
        os.replace(temp, root / 'evidence/attachment-recovery-progress.json')
    unresolved = [r for r in refs if r['path'] not in available and r['path'] not in aliases]
    return {'aliases': aliases, 'files': files, 'references': len(refs), 'available_original_paths': len(available),
            'recovered_paths': len(set(recovered) - ambiguous), 'ambiguous_paths': sorted(ambiguous),
            'unresolved': unresolved, 'unresolved_user_images': sum('userMessage' in r['kinds'] for r in unresolved),
            'records_parsed': parsed, 'records_skipped': skipped, 'bytes_scanned': scanned,
            'max_record_bytes': max_record_bytes, 'original_rollouts_unchanged': True,
            'note': '嵌入图片保留模型当时收到的图像；不能保证与已丢失的原图具有相同分辨率或编码。'}
