"""Codex profile preparation on copies; never edit the source migration evidence.

The destination schema remains authoritative. Unknown schema changes, conflicting
identities and missing dependencies are surfaced instead of silently discarded.
This is an Atlas adapter for the inspected Codex schema, not an official importer.
"""
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import selectors
import time
import uuid
from pathlib import Path

DATABASE_TABLES = {
    'state_5.sqlite': ['projects', 'project_roots', 'thread_sections', 'threads',
        'thread_dynamic_tools', 'thread_spawn_edges', 'thread_attachments',
        'guardian_review_feedback', 'thread_read_receipts'],
    'thread_history_1.sqlite': ['thread_turns', 'thread_items',
        'thread_history_projection_state', 'thread_realtime_items'],
    'memories_1.sqlite': ['stage1_outputs'],
    'memories_v2_1.sqlite': ['stage1_outputs'],
}
PATH_COLUMNS = {'threads': {'cwd', 'rollout_path'}, 'project_roots': {'path'}}
JSON_PATH_COLUMNS = {'threads': {'sandbox_policy'}, 'thread_attachments': {'payload'}}
PATH_KEYS = {'cwd', 'path', 'root', 'roots', 'writable_roots', 'image_path', 'file_path',
             'workspace', 'worktree', 'attachment_path', 'local_path', 'rollout_path', 'rootPaths',
             'writableRoots', 'filePath', 'localPath', 'common_dir', 'checkout_root', 'repo_root'}


def history_module():
    module = sys.modules.get('atlas_codex_history')
    if module is None:
        from . import codex_history as module
    return module


def map_path(value, source_home, target_home):
    if isinstance(value, str) and (value == source_home or value.startswith(source_home + '/')):
        return target_home + value[len(source_home):]
    return value


def map_json_paths(value, source_home, target_home, key=''):
    return history_module().mapped_paths(value, source_home, target_home, key=key)


def load_toml(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        import tomllib
        return tomllib.loads(path.read_text())
    except ImportError:
        python = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3.12'
        if not python.exists():
            raise ValueError('合并 Codex 设置需要 Python 3.11+；原始设置已归档')
        result = subprocess.run([str(python), '-c', 'import json,sys,tomllib;print(json.dumps(tomllib.loads(sys.stdin.read())))'],
            input=path.read_bytes(), capture_output=True, timeout=20)
        if result.returncode:
            raise ValueError('Codex 设置无法解析，保留目标设置')
        return json.loads(result.stdout)


def toml_text(value):
    """Serialize the scalar/list/table subset used by Codex config; reject losses."""
    def scalar(v):
        if isinstance(v, bool): return 'true' if v else 'false'
        if isinstance(v, str): return json.dumps(v, ensure_ascii=False)
        if isinstance(v, (int, float)): return repr(v)
        if isinstance(v, list): return '[' + ', '.join(scalar(x) for x in v) + ']'
        if isinstance(v, dict): return '{' + ', '.join(json.dumps(k) + ' = ' + scalar(x) for k, x in v.items()) + '}'
        raise ValueError('Codex 设置含当前转换器不支持的数据类型')
    lines = []
    def table(data, prefix):
        if prefix:
            lines.append('[' + '.'.join(json.dumps(k, ensure_ascii=False) for k in prefix) + ']')
        for k, v in data.items():
            if not isinstance(v, dict): lines.append(json.dumps(k, ensure_ascii=False) + ' = ' + scalar(v))
        for k, v in data.items():
            if isinstance(v, dict):
                lines.append(''); table(v, prefix + [k])
    table(value, [])
    return '\n'.join(lines) + '\n'


def merge_config(source, target, source_home, target_home):
    """Retain destination choices; add source tools and project configuration."""
    def mapped(value):
        if isinstance(value, dict):
            return {map_path(k, source_home, target_home): mapped(v) for k, v in value.items()}
        if isinstance(value, list): return [mapped(v) for v in value]
        return map_path(value, source_home, target_home)
    source = mapped(source)
    conflicts = []
    def merge(left, right, prefix=''):
        result = dict(left)
        for k, v in right.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = merge(result[k], v, prefix + k + '.')
            else:
                if k in result and result[k] != v:
                    conflicts.append(prefix + k)
                result[k] = v
        return result
    return merge(source, target), conflicts


def connection(path):
    c = sqlite3.connect(Path(path).as_uri() + '?mode=rw', uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    return c


def backup(source, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(connection(source)) as src, contextlib.closing(sqlite3.connect(destination)) as dst:
        src.backup(dst, pages=256)
        dst.execute('PRAGMA journal_mode=DELETE')
        if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('数据库快照校验失败：' + str(source))
    destination.chmod(0o600)


def quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def persistent_signature(path, tables):
    h = hashlib.sha256()
    with connection(path) as con:
        available = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in tables:
            if table not in available:
                continue
            info = list(con.execute('PRAGMA table_info(' + quote(table) + ')'))
            keys = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
            h.update(table.encode())
            for row in con.execute('SELECT * FROM ' + quote(table) + ' ORDER BY ' + ','.join(map(quote, keys))):
                h.update(json.dumps(list(row), ensure_ascii=False, default=lambda b: {'bytes_hex': b.hex()}).encode())
    return h.hexdigest()


def merge_database(source, existing, output, tables, source_home, target_home, attachment_paths=None):
    """Merge persistent records into a destination-schema copy, transactionally.

    Any divergent primary key fails the entire prepared database. Existing target
    records are compared unchanged; only source metadata path fields are remapped.
    History prose and tool output are retained byte for byte.
    """
    source, existing, output = Path(source), Path(existing), Path(output)
    if output.exists():
        raise ValueError('拒绝覆盖已有准备数据库')
    backup(existing if existing.exists() else source, output)
    report = {}
    attachments = sys.modules.get('atlas_codex_attachments')
    if attachments is None:
        from . import codex_attachments as attachments
    try:
        with contextlib.closing(connection(source)) as src, contextlib.closing(sqlite3.connect(output)) as dst:
            dst.row_factory = sqlite3.Row
            source_tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            target_tables = {r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if '_sqlx_migrations' in source_tables and '_sqlx_migrations' in target_tables:
                left = dict(src.execute('SELECT version,checksum FROM _sqlx_migrations'))
                right = dict(dst.execute('SELECT version,checksum FROM _sqlx_migrations'))
                if any(v not in right or right[v] != checksum for v, checksum in left.items()):
                    raise ValueError('目标 Codex 数据库迁移谱系不兼容，保留原始文件供升级后重试')
            dst.execute('PRAGMA foreign_keys=OFF')
            dst.execute('BEGIN IMMEDIATE')
            if not existing.exists():
                for table in target_tables - set(tables) - {'_sqlx_migrations', 'sqlite_sequence'}:
                    dst.execute('DELETE FROM ' + quote(table))
            for table in tables:
                if table not in source_tables:
                    continue
                if table not in target_tables:
                    raise ValueError('目标缺少持久化表：' + table)
                source_cols = [r[1] for r in src.execute('PRAGMA table_info(' + quote(table) + ')')]
                target_info = list(dst.execute('PRAGMA table_info(' + quote(table) + ')'))
                target_cols = {r[1] for r in target_info}
                if not set(source_cols) <= target_cols:
                    raise ValueError('目标缺少来源持久化字段：' + table)
                keys = [r[1] for r in sorted(target_info, key=lambda r: r[5]) if r[5]]
                if not keys:
                    raise ValueError('无法可靠合并无主键表：' + table)
                inserted = equal = 0
                interrupted = 0
                for row in src.execute('SELECT * FROM ' + quote(table)):
                    row = dict(row)
                    if table == 'thread_turns' and row.get('status') not in {'completed', 'failed', 'interrupted'}:
                        row['status'] = 'interrupted'
                        interrupted += 1
                    for col in PATH_COLUMNS.get(table, set()):
                        if col in row:
                            row[col] = map_path(row[col], source_home, target_home)
                    for col in JSON_PATH_COLUMNS.get(table, set()):
                        if row.get(col):
                            value = json.loads(row[col])
                            mapped = map_json_paths(value, source_home, target_home)
                            if mapped != value:
                                row[col] = json.dumps(mapped, ensure_ascii=False, separators=(',', ':'))
                    if table == 'thread_items' and row.get('item_type') in {'userMessage', 'imageView', 'fileChange', 'commandExecution'}:
                        item = json.loads(row['item_json'])
                        mapped = attachments.relocate_item(item, source_home, target_home, attachment_paths)
                        if mapped != item:
                            row['item_json'] = json.dumps(mapped, ensure_ascii=False, separators=(',', ':'))
                    where = ' AND '.join(quote(k) + '=?' for k in keys)
                    found = dst.execute('SELECT * FROM ' + quote(table) + ' WHERE ' + where, [row[k] for k in keys]).fetchone()
                    if found and existing.exists():
                        if any(found[col] != row[col] for col in source_cols):
                            raise ValueError('来源与目标有同标识但不同内容，需单独处理：' + table + '/' + str(row[keys[0]]))
                        equal += 1
                        continue
                    if found:  # No prior target: remap the source-schema baseline.
                        dst.execute('UPDATE ' + quote(table) + ' SET ' + ','.join(quote(c) + '=?' for c in source_cols) + ' WHERE ' + where,
                                    [row[c] for c in source_cols] + [row[k] for k in keys])
                    else:
                        dst.execute('INSERT INTO ' + quote(table) + '(' + ','.join(map(quote, source_cols)) + ') VALUES (' + ','.join('?' for _ in source_cols) + ')', [row[c] for c in source_cols])
                    inserted += 1
                report[table] = {'source_records': inserted + equal, 'inserted': inserted, 'identical': equal,
                                 'total': dst.execute('SELECT count(*) FROM ' + quote(table)).fetchone()[0],
                                 'source_running_turns_marked_interrupted': interrupted}
            if dst.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('合并后的数据库存在引用完整性错误')
            dst.commit()
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('合并后的数据库校验失败')
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return report


def clone(source, target):
    source, target = Path(source), Path(target)
    if target.exists() or target.is_symlink():
        raise ValueError('目标已存在，拒绝覆盖：' + str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        target.symlink_to(os.readlink(source))
        return
    if sys.platform == 'darwin':
        p = subprocess.run(['/bin/cp', '-cRp', str(source), str(target)], capture_output=True)
        if p.returncode:
            raise ValueError('APFS 副本建立失败：' + p.stderr.decode(errors='replace')[-600:])
    elif source.is_dir():
        shutil.copytree(source, target, symlinks=True, copy_function=shutil.copy2)
    else:
        shutil.copy2(source, target, follow_symlinks=False)


@contextlib.contextmanager
def writable_copy(path):
    """Temporarily edit a prepared file/directory, restoring its original mode."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError('不能通过软链接修改副本权限')
    mode = stat.S_IMODE(path.stat().st_mode)
    path.chmod(mode | stat.S_IWUSR)
    try:
        yield
    finally:
        path.chmod(mode)


def write_copy(path, content):
    with writable_copy(path):
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)


def repair_rollout_metadata(codex_home, source_home, target_home, max_record_bytes=128 * 1024**2):
    """Rebind typed session metadata in a derived profile, preserving prose bytes.

    Codex can rebuild SQLite cwd from JSONL metadata on startup. Both stores must
    agree. Large non-metadata records are copied in bounded chunks, never loaded
    as a complete rollout. Invalid records remain byte-identical and are reported.
    """
    import re
    codex_home = Path(codex_home)
    compressed = history_module().expand_compressed(codex_home, codex_home.parent / ('compressed-originals-' + uuid.uuid4().hex))
    changed, warnings = [], []
    for directory in ['sessions', 'archived_sessions']:
        for path in sorted((codex_home / directory).rglob('*.jsonl')):
            if path.is_symlink() or not path.resolve().is_relative_to(codex_home.resolve()):
                raise ValueError('会话日志链接越界')
            temporary = path.with_name(path.name + '.atlas-paths.tmp')
            if temporary.exists():
                raise ValueError('已有未完成的日志重绑定副本：' + str(temporary))
            count, line_number = 0, 0
            try:
                with path.open('rb') as src, temporary.open('xb') as dst:
                    temporary.chmod(0o600)
                    while True:
                        raw = src.readline(max_record_bytes + 1)
                        if not raw:
                            break
                        line_number += 1
                        if len(raw) > max_record_bytes:
                            warnings.append({'path': str(path), 'line': line_number, 'reason': 'record_size_limit'})
                            dst.write(raw)
                            while not raw.endswith(b'\n'):
                                raw = src.readline(1024**2)
                                if not raw:
                                    break
                                dst.write(raw)
                            continue
                        # Only the top-level discriminator near the start is
                        # eligible. Message/tool bodies are never rewritten.
                        if re.search(rb'"type"\s*:\s*"(?:session_meta|turn_context|event_msg)"', raw[:256]):
                            try:
                                record = json.loads(raw)
                                if record.get('type') in {'session_meta', 'turn_context', 'event_msg'}:
                                    mapped = map_json_paths(record.get('payload'), source_home, target_home)
                                    if mapped != record.get('payload'):
                                        record['payload'] = mapped
                                        raw = json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode() + (b'\n' if raw.endswith(b'\n') else b'')
                                        count += 1
                            except (ValueError, UnicodeError):
                                warnings.append({'path': str(path), 'line': line_number, 'reason': 'invalid_metadata_json'})
                        dst.write(raw)
                if count:
                    shutil.copystat(path, temporary)
                    with writable_copy(path.parent):
                        os.replace(temporary, path)
                    changed.append({'path': str(path), 'records': count})
            finally:
                temporary.unlink(missing_ok=True)
    with sqlite3.connect(codex_home / 'state_5.sqlite') as c:
        rows = c.execute('SELECT id,cwd,rollout_path FROM threads').fetchall()
        mapped_rows = [(map_path(cwd, source_home, target_home), map_path(rollout, source_home, target_home), ident)
                      for ident, cwd, rollout in rows if map_path(cwd, source_home, target_home) != cwd or map_path(rollout, source_home, target_home) != rollout]
        c.executemany('UPDATE threads SET cwd=?,rollout_path=? WHERE id=?', mapped_rows)
    offsets = history_module().repair_offsets(codex_home) if (codex_home / 'thread_history_1.sqlite').exists() else None
    return {'status': 'prepared' if not warnings else 'needs_attention', 'files': changed, 'offsets': offsets, 'compressed_logs': compressed,
            'state_rows': len(mapped_rows), 'warnings': warnings, 'historical_message_bytes_preserved': True,
            'scope': 'typed_paths_and_ordinal_derived_byte_offsets', 'checked_at': time.time()}


def repair_references(roots, source_home, target_home):
    """Change only filesystem references, not code, historical prose or user data."""
    changed, external = [], []
    for root in roots:
        for base, dirs, names in os.walk(root, followlinks=False):
            base = Path(base)
            for name in dirs + names:
                path = base / name
                if path.is_symlink():
                    before = os.readlink(path)
                    after = map_path(before, source_home, target_home)
                    if before != after:
                        with writable_copy(path.parent):
                            path.unlink(); path.symlink_to(after)
                        changed.append({'path': str(path), 'kind': 'symlink', 'before': before, 'after': after})
                    elif os.path.isabs(before) and not path.exists():
                        external.append({'path': str(path), 'target': before})
                elif path.is_file() and name == 'pyvenv.cfg':
                    before = path.read_text()
                    lines = []
                    for line in before.splitlines(keepends=True):
                        key, separator, value = line.partition('=')
                        if separator and key.strip() in {'home', 'executable', 'base-prefix', 'base-exec-prefix', 'base-executable'}:
                            mapped = map_path(value.strip(), source_home, target_home)
                            if mapped != value.strip():
                                value = value.replace(value.strip(), mapped, 1)
                        lines.append(key + separator + value)
                    after = ''.join(lines)
                    if before != after:
                        write_copy(path, after)
                        changed.append({'path': str(path), 'kind': 'python_environment'})
                elif path.is_file() and path.parent.name == 'bin' and (path.parent.parent / 'pyvenv.cfg').is_file():
                    with path.open('rb') as f:
                        first = f.readline(4096)
                    prefix = ('#!' + source_home + '/').encode()
                    if first.startswith(prefix):
                        after = ('#!' + target_home).encode() + first[len(('#!' + source_home).encode()):]
                        content = path.read_bytes()
                        write_copy(path, after + content[len(first):])
                        changed.append({'path': str(path), 'kind': 'python_entrypoint'})
                elif path.is_file() and path.suffix == '.pth' and path.parent.name == 'site-packages':
                    before = path.read_text()
                    after = ''.join(map_path(line, source_home, target_home) for line in before.splitlines(keepends=True))
                    if before != after:
                        write_copy(path, after)
                        changed.append({'path': str(path), 'kind': 'python_search_path'})
                elif path.is_file() and (name == '.git' or (name in {'gitdir', 'commondir'} and '.git' in path.parts)):
                    before = path.read_text()
                    prefix = 'gitdir: ' if before.startswith('gitdir: ') else ''
                    raw = before[len(prefix):].strip()
                    after = map_path(raw, source_home, target_home)
                    if raw != after:
                        write_copy(path, prefix + after + '\n')
                        changed.append({'path': str(path), 'kind': 'git_pointer', 'before': raw, 'after': after})
                elif path.is_file() and name in {'config', 'config.worktree'} and '.git' in path.parts:
                    for key in ['core.worktree', 'include.path']:
                        found = subprocess.run(['git', 'config', '--file', str(path), '--get', key], capture_output=True)
                        if found.returncode == 0:
                            before = found.stdout.decode().strip()
                            after = map_path(before, source_home, target_home)
                            if before != after:
                                with writable_copy(path.parent), writable_copy(path):
                                    subprocess.run(['git', 'config', '--file', str(path), key, after], check=True, capture_output=True)
                                changed.append({'path': str(path), 'kind': key, 'before': before, 'after': after})
                elif path.is_file() and name == 'alternates' and '.git' in path.parts:
                    before = path.read_text()
                    after = '\n'.join(map_path(line, source_home, target_home) for line in before.splitlines()) + '\n'
                    if before != after:
                        write_copy(path, after)
                        changed.append({'path': str(path), 'kind': 'git_alternates'})
    return {'changed': changed, 'external_broken_links': external}


def merge_global_state(source, target, source_home, target_home):
    """Keep target account/device state; merge only workspace UI lists and names."""
    result = dict(target)
    for key, value in source.items():
        if key == 'local-projects' and isinstance(value, dict):
            result[key] = {**map_json_paths(value, source_home, target_home), **result.get(key, {})}
        elif key == 'projectless-thread-ids' and isinstance(value, list):
            result[key] = list(dict.fromkeys(result.get(key, []) + value))
        elif key == 'sidebar-project-thread-orders' and isinstance(value, dict):
            orders = dict(result.get(key, {}))
            for project, threads in value.items():
                if isinstance(threads, list):
                    orders[project] = list(dict.fromkeys(orders.get(project, []) + threads))
            result[key] = orders
        elif ('thread-workspace-root-hints:' in key or 'thread-projectless-output-directories:' in key) and isinstance(value, dict):
            mapped = {k: map_path(v, source_home, target_home) for k, v in value.items()}
            result[key] = {**mapped, **result.get(key, {})}
        elif ('saved-workspace-roots' in key or 'active-workspace-roots' in key) and isinstance(value, list):
            mapped = [map_path(x, source_home, target_home) for x in value if isinstance(x, str)]
            result[key] = list(dict.fromkeys(result.get(key, []) + mapped))
        elif 'workspace-root-labels' in key and isinstance(value, dict):
            mapped = {map_path(k, source_home, target_home): v for k, v in value.items()}
            result[key] = {**mapped, **result.get(key, {})}
    return result


class NativeCodex:
    """Bounded, local app-server RPC. Never send prompts or start model turns."""
    def __init__(self, executable, codex_home, log):
        self.log = open(log, 'ab')
        os.chmod(log, 0o600)
        self.proc = subprocess.Popen([str(executable), 'app-server', '--stdio'],
            env={**os.environ, 'CODEX_HOME': str(codex_home)}, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.log)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        self.buffer = b''
        self.ident = 0
        try:
            self.request('initialize', {'clientInfo': {'name': 'atlas_migration_validator', 'version': '0.7.0'},
                                       'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized', 'params': {}})
        except BaseException:
            self.close()
            raise

    def send(self, value):
        self.proc.stdin.write(json.dumps(value).encode() + b'\n')
        self.proc.stdin.flush()

    def request(self, method, params, timeout=60):
        self.ident += 1
        self.send({'id': self.ident, 'method': method, 'params': params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                response = json.loads(line)
                if response.get('id') == self.ident:
                    if 'error' in response:
                        raise ValueError('Codex ' + method + ': ' + str(response['error'])[:600])
                    return response.get('result')
            if not self.selector.select(min(2, max(0, deadline - time.monotonic()))):
                if self.proc.poll() is not None:
                    raise ValueError('原生 Codex 验证进程提前退出')
                continue
            data = os.read(self.proc.stdout.fileno(), 65536)
            if not data:
                raise ValueError('原生 Codex 验证连接关闭')
            self.buffer += data
            if len(self.buffer) > 64 * 1024**2:
                raise ValueError('原生响应超过本次元数据验收的内存上限')
        raise ValueError('原生 Codex 请求超时：' + method)

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill(); self.proc.wait(timeout=5)
        self.selector.close()
        self.log.close()


def native_acceptance(codex_home, evidence, source_ids, destination_ids, full=False):
    codex_home, evidence = Path(codex_home), Path(evidence)
    candidates = [Path('/Applications') / (app + '.app/Contents/Resources/codex-cli/bin/codex') for app in ['ChatGPT', 'Codex']]
    found = shutil.which('codex')
    if found:
        candidates.append(Path(found))
    executable = next((p for p in candidates if p.is_file()), None)
    if not executable:
        raise ValueError('目标尚未安装可用的 Codex 原生程序')
    client = NativeCodex(executable, codex_home, evidence / 'native-validation.log')
    try:
        listed = {}
        expected = set(source_ids) | set(destination_ids)
        with connection(codex_home / 'state_5.sqlite') as c:
            rows = [dict(r) for r in c.execute('SELECT * FROM threads')]
        attached = {r['id'] for r in rows if r.get('attached_thread_parent_id')}
        parents = set()
        for row in rows:
            try:
                source = json.loads(row.get('source', ''))
                parent = source.get('subagent', {}).get('thread_spawn', {}).get('parent_thread_id')
                if parent:
                    parents.add(parent)
            except (ValueError, AttributeError):
                pass
        filters = [{}] + [{'parentThreadId': p} for p in sorted(parents)]
        filters += [{'attachedToThreadId': p} for p in sorted({r['attached_thread_parent_id'] for r in rows if r.get('attached_thread_parent_id')})]
        for scope, archived in ((scope, archived) for scope in filters for archived in [False, True]):
            cursor = None
            while True:
                result = client.request('thread/list', {'limit': 100, 'archived': archived, 'cursor': cursor,
                    **scope,
                    'currentAccountOnly': False, 'useStateDbOnly': True,
                    'sourceKinds': ['cli', 'vscode', 'exec', 'appServer', 'subAgent', 'subAgentReview',
                                    'subAgentCompact', 'subAgentThreadSpawn', 'subAgentOther', 'unknown']})
                for item in result.get('data', []):
                    listed[item['id']] = item
                cursor = result.get('nextCursor')
                if not cursor:
                    break
        # Spawned and attached chats are queried under their actual parents.
        missing = sorted(expected - listed.keys())
        errors, checked = [], []
        for ident in sorted(expected):
            try:
                result = client.request('thread/read', {'threadId': ident, 'includeTurns': False})
                thread = result['thread']
                if thread.get('id') != ident:
                    raise ValueError('原生读取返回的会话标识与请求不符')
                checked.append({'id': ident, 'cwd': thread.get('cwd'), 'history_mode': thread.get('historyMode')})
            except ValueError as exc:
                errors.append({'id': ident, 'error': str(exc)})
        history_samples = []
        with connection(codex_home / 'thread_history_1.sqlite') as c:
            history_ids = [r[0] for r in c.execute('SELECT thread_id FROM thread_turns GROUP BY thread_id ORDER BY count(*) DESC')]
            samples = [ident for ident in history_ids if ident in expected]
            # Older deleted/unregistered chats can retain history rows without
            # an active state index. Preserve these rows; do not invent chats or
            # claim that the native app can open them.
            orphan_history = sorted(set(history_ids) - expected)
            item_counts = dict(c.execute('SELECT thread_id,count(*) FROM thread_items GROUP BY thread_id'))
        for ident in samples:
            try:
                result = client.request('thread/turns/list', {'threadId': ident, 'limit': 1, 'itemsView': 'summary'})
                items = client.request('thread/items/list', {'threadId': ident, 'limit': 2})
                if not result.get('data'):
                    raise ValueError('数据库中有历史轮次，但原生接口返回空历史')
                if item_counts.get(ident) and not items.get('data'):
                    raise ValueError('数据库中有会话内容，但原生接口返回空内容')
                history_samples.append({'id': ident, 'returned_turns': len(result.get('data', [])),
                                        'returned_items': len(items.get('data', [])),
                                        'item_types': [r.get('type') for r in items.get('data', [])]})
            except ValueError as exc:
                errors.append({'id': ident, 'error': str(exc)})
        resume_test, skills = None, None
        candidates = [r for r in checked if r['id'] in source_ids and r.get('cwd') and Path(r['cwd']).is_dir()]
        if candidates:
            with connection(codex_home / 'thread_history_1.sqlite') as c:
                sizes = dict(c.execute('SELECT thread_id,count(*) FROM thread_items GROUP BY thread_id'))
            candidate = min(candidates, key=lambda r: sizes.get(r['id'], 0) or 10**9)
            try:
                response = client.request('thread/resume', {'threadId': candidate['id'], 'cwd': candidate['cwd'], 'excludeTurns': True}, timeout=120)
                if response.get('thread', {}).get('id') != candidate['id'] or response.get('cwd') != candidate['cwd']:
                    raise ValueError('原生接续返回的会话或目录与请求不符')
                resume_test = {'id': response['thread']['id'], 'cwd': response['cwd'], 'status': 'resumed_without_new_turn'}
                found = client.request('skills/list', {'cwds': [candidate['cwd']], 'forceReload': True}, timeout=120)
                skills = [{'cwd': row.get('cwd'), 'count': len(row.get('skills', [])),
                           'errors': row.get('errors', [])} for row in found.get('data', [])]
                if not any(row['cwd'] == candidate['cwd'] for row in skills):
                    errors.append({'component': 'skills', 'error': '原生技能发现未返回待验收工作目录'})
                if any(row['errors'] for row in skills):
                    errors.append({'component': 'skills', 'error': '部分技能未通过原生发现，详见技能报告'})
            except ValueError as exc:
                errors.append({'component': 'resume_or_skills', 'error': str(exc)})
        elif source_ids:
            errors.append({'component': 'resume', 'error': '没有可接续的迁入工程目录'})
        comprehensive, offsets = None, None
        if full:
            def progress(value):
                path = evidence / 'history-validation-progress.json'
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps({**value, 'updated_at': time.time()}))
                temporary.chmod(0o600)
                os.replace(temporary, path)
            comprehensive = history_module().native_full_history(client, codex_home, progress)
            offsets = history_module().offset_plan(codex_home)
            orphan_history = comprehensive['unregistered_history_ids']
            if comprehensive['status'] != 'passed' or offsets['status'] != 'passed':
                errors.append({'component': 'full_history', 'error': '全量原生字段或日志偏移校验未通过'})
        return {'status': 'passed' if not missing and not errors else 'failed',
                'full_history': comprehensive, 'offsets': offsets,
                'source_threads': len(source_ids), 'preserved_target_threads': len(destination_ids),
                'listed_threads': len(listed), 'read_threads': len(checked), 'missing_ids': missing,
                'attached_threads': len(expected & attached),
                'parent_groups_checked': len(filters) - 1,
                'unregistered_history_ids': orphan_history,
                'errors': errors, 'threads': checked, 'history_samples': history_samples,
                'resume': resume_test, 'skills': skills,
                'scope': 'native_thread_list_read_history_resume_and_skills', 'checked_at': time.time(),
                'model_turns_started': False, 'runtime_dependencies_validated': False}
    finally:
        client.close()


def rebase_git_metadata(connection, source_home, target_home):
    count = 0
    for ident, raw in connection.execute('SELECT id,git_json FROM projects').fetchall():
        if not raw:
            continue
        original = json.loads(raw)
        mapped = map_json_paths(original, source_home, target_home)
        if mapped != original:
            connection.execute('UPDATE projects SET git_json=? WHERE id=?', (json.dumps(mapped, ensure_ascii=False), ident))
            count += 1
    return count


def rebase_atlas(catalog, source_home, target_home, workspaces=()):
    """Rebind current local paths and identity while keeping historical records."""
    catalog = Path(catalog)
    fields = {'projects': ['path'], 'assets': ['repo_root'],
        'repositories': ['source_root', 'common_dir', 'archive_path'],
        'repository_archives': ['archive_path'], 'project_repositories': ['checkout_root'],
        'project_index_runs': ['manifest'], 'knowledge_items': ['path']}
    counts, imported = {}, []
    with sqlite3.connect(catalog / 'catalog.sqlite3') as c:
        c.execute('PRAGMA foreign_keys=ON')
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        prior = c.execute("SELECT value FROM settings WHERE key='node_id'").fetchone()
        alias_row = c.execute("SELECT value FROM settings WHERE key='source_path_aliases'").fetchone()
        aliases = json.loads(alias_row[0]) if alias_row else []
        aliases = [{**a, 'target': map_path(a['target'], source_home, target_home)} for a in aliases]
        aliases.append({'source': source_home, 'target': target_home})
        c.execute("INSERT OR REPLACE INTO settings VALUES('source_path_aliases',?)", (json.dumps(aliases),))
        node_id = uuid.uuid4().hex
        counts['projects.git_json'] = rebase_git_metadata(c, source_home, target_home)
        for table, columns in fields.items():
            if table not in tables:
                continue
            available = {r[1] for r in c.execute('PRAGMA table_info(' + quote(table) + ')')}
            for col in set(columns) & available:
                q = quote(col)
                result = c.execute('UPDATE ' + quote(table) + ' SET ' + q + '=? || substr(' + q + ',?) WHERE substr(' + q + ',1,?)=?',
                    (target_home, len(source_home)+1, len(source_home)+1, source_home+'/'))
                counts[table+'.'+col] = result.rowcount
        existing_paths = [Path(r[0]) for r in c.execute('SELECT path FROM projects')]
        for row in workspaces:
            relative = Path(row['relative'])
            if relative.is_absolute() or '..' in relative.parts or not relative.parts or relative.parts[0] in {'.codex', '.cache', '.local', 'Library'}:
                continue
            path = Path(target_home) / relative
            infrastructure = [catalog] + [catalog.parent / name for name in ['archive', 'backups', 'tools', 'app']]
            if not path.is_relative_to(target_home) or path == catalog.parent or any(path.is_relative_to(p) for p in infrastructure) or not path.is_dir():
                continue
            if any(path.is_relative_to(p) or p.is_relative_to(path) for p in existing_paths):
                continue  # An umbrella directory should not duplicate its projects.
            if not any(p.is_file() and p.name != '.DS_Store' for p in path.iterdir()) and not (path / '.git').exists():
                continue
            ident = str(uuid.uuid5(uuid.NAMESPACE_URL, source_home + '/' + relative.as_posix()))
            c.execute('INSERT INTO projects(id,name,path,host) VALUES(?,?,?,?)', (ident, path.name, str(path), 'local'))
            existing_paths.append(path)
            imported.append({'id': ident, 'path': str(path)})
        c.execute("INSERT OR REPLACE INTO settings VALUES('node_id',?)", (node_id,))
        c.execute("DELETE FROM settings WHERE key IN ('node_label','watch_status','provenance_status') OR key LIKE 'watch_git:%' OR key LIKE 'watch_initialized:%' OR key LIKE 'session:%'")
        c.execute("INSERT OR REPLACE INTO settings VALUES('codex_source_root',?)", (target_home+'/.codex',))
        if 'project_locations' in tables:
            for ident, path in c.execute('SELECT id,path FROM projects').fetchall():
                if Path(path).is_dir():
                    c.execute("UPDATE project_locations SET node_id=?,mode='workspace',source_node=? WHERE project_id=?",
                              (node_id, prior[0] if prior else None, ident))
                    c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('watch_dirty:'+ident, '1'))
        if 'knowledge_items' in tables and prior:
            c.execute('UPDATE knowledge_items SET node_id=? WHERE node_id=?', (node_id, prior[0]))
        if 'sessions' in tables:
            # Atlas authentication sessions are never valid on the new node.
            cols = {r[1] for r in c.execute('PRAGMA table_info(sessions)')}
            if 'token_hash' in cols:
                c.execute('DELETE FROM sessions')
        if c.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Atlas 路径重绑定后发现引用完整性错误')
    return {'node_id': node_id, 'source_node_id': prior[0] if prior else None, 'updated': counts,
            'imported_workspaces': imported}


def accepted_git_upgrade(source_refs, target_refs, source_head, target_head, receipt, is_ancestor):
    """Only exact recorded additive/fast-forward changes can supersede a snapshot."""
    if not receipt:
        return False
    def refs(raw):
        return {line.split(' ',1)[1]:line.split(' ',1)[0] for line in raw.decode().splitlines()}
    old,new=refs(source_refs),refs(target_refs)
    changes={k:{'before':old.get(k),'after':new.get(k)} for k in old.keys()|new.keys() if old.get(k)!=new.get(k)}
    if changes!=receipt.get('refs') or any(not v['after'] for v in changes.values()):return False
    for ref,change in changes.items():
        if not ref.startswith(('refs/heads/','refs/remotes/')):return False
        if change['before'] and not is_ancestor(change['before'],change['after']):return False
    before,after=source_head.decode().strip(),target_head.decode().strip()
    if before!=after:
        if receipt.get('head')!={'before':before,'after':after} or not is_ancestor(before,after):return False
    return True


def workspace_acceptance(root, source_home, target_home, source_missing_transient_workspaces=()):
    root, home = Path(root), Path(target_home)
    incoming = root / 'incoming'
    repositories = []
    with (root / 'evidence/source-manifest.jsonl').open() as manifest:
        for line in manifest:
            row = json.loads(line)
            if Path(row['path']).name == '.git':
                repositories.append(Path(row['path']).parent)
    def git(cwd, *args, gitdir=None):
        command = ['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null']
        if gitdir:
            command += ['--git-dir='+str(gitdir), '--work-tree='+str(cwd)]
        else:
            command += ['-C', str(cwd)]
        return subprocess.run(command + list(args), capture_output=True, timeout=180)
    reports, errors, skipped = [], [], []
    upgrade_path=root/'evidence/git-upgrades.json'
    upgrades=json.loads(upgrade_path.read_text()).get('repositories',{}) if upgrade_path.exists() else {}
    for rel in sorted(set(repositories)):
        original_marker = incoming / rel / '.git'
        gitdir = original_marker
        if original_marker.is_symlink():
            value = os.readlink(original_marker)
            gitdir = Path(map_path(value, source_home, str(incoming))) if os.path.isabs(value) else original_marker.parent / value
        if gitdir.is_file():
            value = gitdir.read_text().strip()
            if not value.startswith('gitdir: '):
                skipped.append({'path': rel.as_posix(), 'reason': 'source_marker_not_gitdir'})
                continue
            value = value[8:]
            gitdir = Path(map_path(value, source_home, str(incoming))) if os.path.isabs(value) else original_marker.parent / value
        if not gitdir.resolve().is_relative_to(incoming.resolve()):
            errors.append({'path': rel.as_posix(), 'error': 'Git 公共目录未包含在快照中'})
            continue
        source_head = git(incoming / rel, 'rev-parse', '--verify', 'HEAD', gitdir=gitdir)
        source_refs = git(incoming / rel, 'for-each-ref', '--format=%(objectname) %(refname)', gitdir=gitdir)
        if source_refs.returncode:
            skipped.append({'path': rel.as_posix(), 'reason': 'source_git_unreadable'})
            continue
        target = home / rel
        target_head = git(target, 'rev-parse', '--verify', 'HEAD')
        target_refs = git(target, 'for-each-ref', '--format=%(objectname) %(refname)')
        integrity = git(target, 'fsck', '--full', '--no-reflogs')
        same = (source_head.returncode == target_head.returncode and source_head.stdout == target_head.stdout
                and source_refs.stdout == target_refs.stdout and not target_refs.returncode)
        upgrade=upgrades.get(rel.as_posix())
        upgraded=(not source_head.returncode and not target_head.returncode and not target_refs.returncode and
            accepted_git_upgrade(source_refs.stdout,target_refs.stdout,source_head.stdout,target_head.stdout,upgrade,
                lambda before,after:git(target,'merge-base','--is-ancestor',before,after).returncode==0))
        item = {'path': str(target), 'head': target_head.stdout.decode(errors='replace').strip(),
                'refs_equal': same, 'documented_upgrade':upgrade if upgraded else None,
                'references_accepted':bool(same or upgraded),'fsck_exit': integrity.returncode}
        if not (same or upgraded) or integrity.returncode:
            item['error'] = integrity.stderr.decode(errors='replace')[-1200:] or 'Git HEAD / refs 不一致且没有可验证的升级记录'
            errors.append(item)
        reports.append(item)
    with connection(home / '.codex/state_5.sqlite') as c:
        roots = [{'path': row[0], 'exists': Path(row[0]).is_dir()} for row in c.execute('SELECT DISTINCT path FROM project_roots')]
        missing_cwd = [dict(row) for row in c.execute('SELECT id,cwd,git_sha FROM threads') if not Path(row['cwd']).is_dir()]
    baseline = {(r['id'], r['cwd']) for r in source_missing_transient_workspaces if r.get('source_exists') is False}
    transient = [r for r in missing_cwd if (r['id'], r['cwd']) in baseline]
    missing_cwd = [r for r in missing_cwd if (r['id'], r['cwd']) not in baseline]
    return {'status': 'passed' if not errors and all(r['exists'] for r in roots) and not missing_cwd else 'needs_attention',
            'repositories': reports, 'git_errors': errors, 'source_git_markers_skipped': skipped,
            'project_roots': roots, 'missing_thread_workspaces': missing_cwd,
            'source_missing_transient_workspaces': transient,
            'scope': 'project_directories_git_head_refs_and_full_object_integrity',
            'project_builds_executed': False, 'checked_at': time.time()}


def recover_historical_worktree(home, thread_id, repository):
    """Recover a source-missing checkout from its recorded commit on the receiver."""
    home, repository = Path(home).resolve(), Path(repository)
    with connection(home / '.codex/state_5.sqlite') as c:
        row = c.execute('SELECT cwd,git_sha FROM threads WHERE id=?', (thread_id,)).fetchone()
    if not row or not row['git_sha']:
        raise ValueError('会话没有可恢复的提交记录')
    destination = Path(row['cwd'])
    if not destination.is_relative_to(home / '.codex/worktrees') or destination == home / '.codex/worktrees':
        raise ValueError('仅恢复会话已记录的缺失工作树')
    if destination.exists() or destination.is_symlink():
        raise ValueError('工作树已存在，不覆盖')
    if not repository.resolve().is_relative_to(home) or not repository.is_dir():
        raise ValueError('仓库必须位于目标用户目录')
    if not destination.parent.resolve().is_relative_to(home):
        raise ValueError('工作树父级链接越界')
    command = ['git', '-c', 'core.hooksPath=/dev/null', '-C', str(repository)]
    commit = subprocess.run(command + ['rev-parse', '--verify', row['git_sha'] + '^{commit}'], capture_output=True)
    if commit.returncode:
        raise ValueError('指定仓库没有该历史提交')
    sha = commit.stdout.decode().strip()
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(command + ['worktree', 'add', '--detach', '--force', str(destination), sha],
                            capture_output=True, timeout=300)
    if result.returncode:
        raise ValueError('历史工作树恢复失败：' + result.stderr.decode(errors='replace')[-1000:])
    return {'thread_id': thread_id, 'path': str(destination), 'repository': str(repository), 'commit': sha,
            'status': 'reconstructed_committed_state', 'branch': None, 'source_changed': False,
            'uncommitted_source_state_recovered': False, 'recovered_at': time.time()}
