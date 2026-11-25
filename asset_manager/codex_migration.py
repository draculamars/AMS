"""Durable Atlas migration plans and bounded-memory SSH transfers.

Workers outlive web requests. No source deletion, blind destination overwrite,
password collection, or migration of live RAM/swap is provided by this module.
"""
import base64
import collections
import concurrent.futures
import contextlib
import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zlib
from pathlib import Path
from . import db, migration_agent as agent, codex_profile, codex_attachments, migration_tools

CODEx_TRANSIENT = {'ipc', 'tmp', '.tmp', 'app-server-control', 'thread-writer-locks',
                   'project-metadata-locks', 'process_manager', 'shell_snapshots',
                   'cache', 'models_cache.json', 'node_repl/active_execs'}
AUTH_FILES = {'auth.json', 'auth.lock', '.env'}
APP_CACHE = {'Cache', 'Code Cache', 'GPUCache', 'DawnGraphiteCache', 'DawnWebGPUCache',
             'ShaderCache', 'Crashpad', 'Cookies', 'Cookies-journal', 'Network', 'Session Storage'}


def desktop_archives(home):
    """Durable desktop preferences and custom executors; never copy login stores."""
    base = Path(home) / 'Library/Application Support/Codex'
    files = [base / name for name in ['Preferences', 'browser-sidebar-page-states.json', 'Default/Bookmarks']]
    for name in ['executor-plugins', 'storage-cache']:
        files += [p for p in (base / name).rglob('*') if p.is_file() and not p.is_symlink()]
    return sorted(str(p) for p in files if p.is_file())


def folder(ident):
    if not re.fullmatch('[a-f0-9]{32}', ident):
        raise ValueError('迁移任务标识无效')
    return db.DATA / 'codex-migrations' / ident


def read(ident):
    return json.loads((folder(ident) / 'plan.json').read_text(encoding='utf-8'))


def save(plan, **updates):
    plan.update(updates, updated_at=time.time())
    agent.atomic_json(folder(plan['id']) / 'plan.json', plan)


def listing():
    result = []
    for p in sorted((db.DATA / 'codex-migrations').glob('*/plan.json'), key=lambda p: p.stat().st_mtime, reverse=True):
        value = json.loads(p.read_text(encoding='utf-8'))
        if value.get('role') == 'tool_supplement':
            continue  # Supplemental receipts are presented by their parent task.
        result.append({k: value.get(k) for k in ['id', 'name', 'status', 'phase', 'detail', 'host', 'root',
                       'created_at', 'updated_at', 'progress', 'result', 'worker_pid']})
    return result


def ssh_arguments(plan):
    host = plan['host']
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*(?:@[A-Za-z0-9_][A-Za-z0-9_.-]*)?', host):
        raise ValueError('SSH 主机名或 user@host 无效')
    port = int(plan.get('port', 22))
    if not 1 <= port <= 65535:
        raise ValueError('SSH 端口无效')
    return ['ssh', '-p', str(port), '-oBatchMode=yes', '-oStrictHostKeyChecking=yes',
            '-oConnectTimeout=12', '-oServerAliveInterval=15', '-oServerAliveCountMax=3', host]


def remote(plan, action, timeout=90, **body):
    # Source is trusted application code, data travels on stdin, never in a shell expression.
    source = Path(agent.__file__).read_bytes()
    encoded = base64.b64encode(zlib.compress(source, 9)).decode()
    profile = base64.b64encode(zlib.compress(Path(codex_profile.__file__).read_bytes(), 9)).decode()
    attachments = base64.b64encode(zlib.compress(Path(codex_attachments.__file__).read_bytes(), 9)).decode()
    runtime_tools = base64.b64encode(zlib.compress(Path(migration_tools.__file__).read_bytes(), 9)).decode()
    program = ("import base64,zlib,types,sys;a=types.ModuleType('atlas_codex_attachments');sys.modules[a.__name__]=a;"
               "exec(zlib.decompress(base64.b64decode('" + attachments + "')),a.__dict__);m=types.ModuleType('atlas_codex_profile');"
               "sys.modules[m.__name__]=m;exec(zlib.decompress(base64.b64decode('" + profile + "')),m.__dict__);"
               "t=types.ModuleType('atlas_migration_tools');sys.modules[t.__name__]=t;"
               "exec(zlib.decompress(base64.b64decode('" + runtime_tools + "')),t.__dict__);"
               "exec(zlib.decompress(base64.b64decode('" + encoded + "')))")
    command = 'python3 -c ' + shlex.quote(program)
    if plan.get('target', {}).get('platform') == 'Darwin':
        command = '/usr/bin/caffeinate -i ' + command
    for attempt in range(3):
        result = subprocess.run(ssh_arguments(plan) + [command],
            input=json.dumps({'action': action, 'root': plan.get('root'), 'id': plan.get('id'), **body}).encode(),
            capture_output=True, timeout=timeout)
        if result.returncode != 255 or action not in {'inspect', 'prepare', 'mkdir', 'report'} or attempt == 2:
            break
        time.sleep(5 * (attempt + 1))
    if result.returncode:
        raise ValueError('目标操作失败：' + result.stderr.decode(errors='replace')[-1800:])
    return json.loads(result.stdout)


def resource_policies():
    return [
        {'kind': 'RAM / swap', 'action': 'rebuild', 'reason': '运行时内存和系统交换文件不可作为可恢复的 Codex 内容；不读取或复制内存转储。'},
        {'kind': 'Codex 记忆、会话、数据库', 'action': 'migrate', 'reason': '持久化记忆和上下文必须保存；SQLite 使用在线一致副本，保留原始快照。'},
        {'kind': '工程、Git、worktree、附件', 'action': 'migrate', 'reason': '包含未提交文件、Git 元数据、软链接和权限；迁移后验证引用和历史。'},
        {'kind': '技能与插件资源', 'action': 'migrate', 'reason': '已安装插件中的本地定制资源也可能位于 cache，不能统一当作垃圾跳过。'},
        {'kind': '浏览器图形缓存、临时锁、socket', 'action': 'rebuild', 'reason': '新机重建；任务状态和成果已单独归档。'},
        {'kind': '登录凭据、系统钥匙串', 'action': 'retain_target', 'reason': '保留目标登录，按需重新授权；不从源机复制 auth.json、登录 Cookie 或系统钥匙串。'},
        {'kind': '虚拟环境、编译与模型缓存', 'action': 'inspect', 'reason': '工程内资源先保留；失效绝对路径和外部运行依赖逐项核对，不能以缓存名称推断可删除。'},
    ]


def historical_workspaces(home, codex):
    found, missing = [], []
    path = codex / 'thread_history_1.sqlite'
    if path.exists():
        with contextlib.closing(agent.connect_existing(path)) as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_items'").fetchone():
                for row in c.execute("SELECT DISTINCT json_extract(item_json,'$.cwd') FROM thread_items WHERE item_type='commandExecution'"):
                    if not isinstance(row[0], str):
                        continue
                    candidate = Path(row[0])
                    if not candidate.is_absolute() or '..' in candidate.parts or not candidate.is_relative_to(home):
                        continue
                    rel = candidate.relative_to(home)
                    if len(rel.parts) < 2 or rel.parts[0] in {'Library', '.cache', '.local'}:
                        continue  # A diagnostic shell cwd is not permission to copy a whole profile.
                    (found if candidate.is_dir() else missing).append(str(candidate))
    return {'existing': sorted(set(found)), 'missing': sorted(set(missing)),
            'source': 'persisted_command_execution_working_directories'}


def missing_transient_workspaces(home):
    home = Path(home)
    database = home / '.codex/state_5.sqlite'
    if not database.exists():
        return []
    temporary = [Path('/tmp').resolve(), Path('/private/tmp').resolve(), Path(tempfile.gettempdir()).resolve()]
    missing = []
    with contextlib.closing(agent.connect_existing(database)) as c:
        columns = {r[1] for r in c.execute('PRAGMA table_info(threads)')}
        if not {'id', 'cwd', 'project_id'} <= columns:
            return []
        for ident, cwd in c.execute('SELECT id,cwd FROM threads WHERE project_id IS NULL'):
            path = Path(cwd)
            if (path.is_absolute() and not path.is_relative_to(home) and not path.exists()
                    and any(path.resolve().is_relative_to(base) for base in temporary)):
                missing.append({'id': ident, 'cwd': cwd, 'source_exists': False,
                    'reason': 'source_missing_transient_workspace', 'checked_at': time.time()})
    return missing


def source_roots(home, codex, discovery=None):
    roots = [codex]
    projectless_containers = set()
    config = codex / 'config.toml'
    if config.exists():
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        roots += [Path(p) for p in tomllib.loads(config.read_text()).get('projects', {})]
    for name in ['.cache/codex-runtimes', '.local/share/uv/python', '.nvm', '.local/bin', '.hf-cli']:
        runtime = home / name
        if runtime.exists():
            roots.append(runtime)
    # User launchers often point to packages outside engineering directories.
    # Preserve package directories under .local/share, without importing login
    # profiles from unrelated dot-directories or traversing the whole home.
    launchers = home / '.local/bin'
    if launchers.is_dir():
        share = home / '.local/share'
        for launcher in launchers.iterdir():
            if launcher.is_symlink():
                target = launcher.resolve()
                if target.is_relative_to(share) and len(target.relative_to(share).parts) > 1:
                    roots.append(share / target.relative_to(share).parts[0])
    for name in ['.cache/huggingface/hub', '.cache/huggingface/datasets', '.cache/huggingface/lerobot',
                 '.cache/torch/hub', '.cache/torch/checkpoints', '.cache/whisper', '.ollama/models', '.lmstudio/models']:
        path = home / name
        if path.is_dir():
            roots.append(path)
    historical = historical_workspaces(home, codex)
    roots += [Path(p) for p in historical['existing']]
    if discovery is not None:
        discovery.update(historical)
    database = codex / 'state_5.sqlite'
    if database.exists():
        with contextlib.closing(agent.connect_existing(database)) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'project_roots' in tables:
                roots += [Path(r[0]) for r in c.execute('SELECT DISTINCT path FROM project_roots')]
            if 'threads' in tables:
                roots += [Path(r[0]) for r in c.execute('SELECT DISTINCT cwd FROM threads') if r[0].startswith(str(home) + '/')]
    global_path = codex / '.codex-global-state.json'
    if global_path.is_file():
        data = json.loads(global_path.read_text())
        for key, value in data.items():
            if ('saved-workspace-roots' in key or 'active-workspace-roots' in key) and isinstance(value, list):
                roots += [Path(x) for x in value if isinstance(x, str)]
            elif key == 'local-projects' and isinstance(value, dict):
                roots += [Path(x) for p in value.values() if isinstance(p, dict) for x in p.get('rootPaths', []) if isinstance(x, str)]
            elif ('thread-workspace-root-hints:' in key or 'thread-projectless-output-directories:' in key) and isinstance(value, dict):
                for ident, v in value.items():
                    roots += [Path(x) for x in (v if isinstance(v, list) else [v]) if isinstance(x, str) and x.startswith(str(home)+'/')]
                    if 'thread-workspace-root-hints:' in key and ident in data.get('projectless-thread-ids', []) and isinstance(v, str):
                        projectless_containers.add(Path(v))
    expanded = []
    for root in roots:
        # Projectless output containers often already exist on the receiver.
        # Preserve each dated child rather than replacing the shared container.
        children = list(root.iterdir()) if root in projectless_containers and root.is_dir() else []
        expanded += children if children and all(p.is_dir() and not p.is_symlink() for p in children) else [root]
    return expanded


def user_node_bin(home, roots):
    """Resolve a captured user Node, including services without interactive PATH."""
    home = Path(home)
    def selected(path):
        return path.is_file() and any(path.is_relative_to(Path(r)) for r in roots)
    executable = shutil.which('node')
    if executable and selected(Path(executable)):
        return Path(executable).parent.relative_to(home).as_posix()
    alias = home / '.nvm/alias/default'
    value = alias.read_text().strip() if alias.is_file() else 'node'
    seen = set()
    for _ in range(8):
        if re.fullmatch(r'v?\d+(?:\.\d+){0,2}', value) or value in {'node', 'stable'}:
            break
        if value in seen or '..' in Path(value).parts or Path(value).is_absolute():
            return None
        seen.add(value)
        path = home / '.nvm/alias' / value
        if not path.is_file():
            return None
        value = path.read_text().strip()
    prefix = tuple(map(int, value.lstrip('v').split('.'))) if re.fullmatch(r'v?\d+(?:\.\d+){0,2}', value) else ()
    versions = []
    for path in (home / '.nvm/versions/node').glob('v*/bin/node'):
        match = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)', path.parent.parent.name)
        if match and selected(path):
            version = tuple(map(int, match.groups()))
            if version[:len(prefix)] == prefix:
                versions.append((version, path))
    return max(versions)[1].parent.relative_to(home).as_posix() if versions else None


def database_paths(roots, codex):
    paths = list(codex.glob('*.sqlite')) + list((codex / 'sqlite').glob('*.db'))
    if (db.DATA / 'catalog.sqlite3').exists() and any(db.DATA.is_relative_to(r) for r in roots):
        paths.append(db.DATA / 'catalog.sqlite3')
    return [p for p in paths if p.is_file() and not p.is_symlink()]


def active_files(plan):
    """Inspect open writers without stopping source applications or devices."""
    executable = shutil.which('lsof')
    if not executable or os.name == 'nt':
        return []
    result = subprocess.run([executable, '-nP', '-F', 'pftan', '-u', str(os.getuid())],
                            capture_output=True, text=True, timeout=60)
    roots = [Path(r['source']) for r in plan['roots']]
    current, found = {}, {}
    pid = ''
    for line in result.stdout.splitlines() + ['fEND']:
        if line.startswith('p'):
            pid = line[1:]
        if line.startswith('f'):
            if current.get('t') == 'REG' and current.get('a') in {'u', 'w'}:
                path = Path(current.get('n', ''))
                if path.is_absolute() and any(path.is_relative_to(r) for r in roots) and not filtered(plan, path) and path.is_file() and not path.is_symlink():
                    kind = 'runtime_lock' if path.name in {'agent.lock', 'server.lock', 'process.lock', '.dds-log.lock', 'server.pid'} else 'live_file'
                    found[str(path)] = {'path': str(path), 'kind': kind}
            current = {'pid': pid}
        elif line and line[0] in 'tan':
            current[line[0]] = line[1:]
    return sorted(found.values(), key=lambda r: r['path'])


def include_active_files(plan):
    inspection = active_files(plan)
    plan['active_file_inspection'] = inspection
    plan['frozen_files'] = [r['path'] for r in inspection if r['kind'] == 'live_file']
    for row in inspection:
        plan['excluded'].append({'path': row['path'], 'reason':
            '源机仍在运行的进程锁，在目标重建' if row['kind'] == 'runtime_lock' else '活动文件单独冻结副本，保持源机服务运行'})


def make_plan(body):
    home = Path(body.get('source_home') or Path.home()).expanduser().resolve()
    if home != Path.home().resolve():
        raise ValueError('来源必须是当前运行用户；其他节点请在该节点创建任务')
    codex = home / '.codex'
    probe = {'host': body.get('host', ''), 'port': body.get('port', 22)}
    target = remote(probe, 'inspect')
    if target['platform'] not in {'Darwin', 'Linux'}:
        raise ValueError('本版迁移接收器支持 macOS/Linux；目标桌面兼容性另行验收')
    ident = uuid.uuid4().hex
    root = body.get('root') or target['home'] + '/MigrationInbox/codex-' + ident[:12]
    root_path = Path(root)
    if not root_path.is_absolute() or '..' in root_path.parts or not root_path.is_relative_to(Path(target['home'])) or len(root_path.relative_to(target['home']).parts) < 2:
        raise ValueError('暂存目录必须位于目标用户的专用子目录')
    discovery = {}
    roots = source_roots(home, codex, discovery) + [Path(x).expanduser() for x in body.get('extra_roots', [])]
    # The desktop's browser profile is not an auth-portable application profile.
    # Its durable Codex state is in .codex; capture preferences separately on request.
    if body.get('include_atlas', True):
        roots += [db.DATA, db.blob_root(), db.DATA.parent / 'tools', db.DATA.parent / 'backups']
    missing = []
    normalized = []
    for path in roots:
        if not path.is_absolute() or '..' in path.parts or path == home or not path.is_relative_to(home):
            raise ValueError('来源目录必须是用户目录中的明确子目录：' + str(path))
        if not path.exists():
            missing.append(str(path))
            continue
        if path.is_symlink():
            raise ValueError('来源根目录为软链接，请明确实际来源：' + str(path))
        normalized.append(path)
    roots = []
    for path in sorted(set(normalized), key=lambda p: (len(p.parts), str(p))):
        if not any(path.is_relative_to(existing) for existing in roots):
            roots.append(path)
    databases = database_paths(roots, codex)
    excluded = []
    for name in sorted(CODEx_TRANSIENT | AUTH_FILES):
        excluded.append({'path': str(codex / name), 'reason': '运行时状态或来源认证；新机重建/重新授权'})
    for path in codex.glob('*.lock'):
        excluded.append({'path': str(path), 'reason': '运行时锁'})
    excluded.append({'path': str(db.DATA / 'codex-migrations'), 'reason': '任务控制目录，不递归归档自身'})
    for name in ['tmp', 'server.log', 'server-error.log', 'migration-server.log', 'server.pid', 'mutation.lock', 'vault-locks']:
        excluded.append({'path': str(db.DATA / name), 'reason': 'Atlas 运行日志、锁或临时状态；审计历史已保存在数据库中'})
    for name in ['sessions', 'archived_sessions', '.codex-global-state.json', '.codex-global-state.json.bak', 'session_index.jsonl']:
        excluded.append({'path': str(codex / name), 'reason': '活动会话及目录状态单独捕获稳定副本'})
    for path in databases:
        for suffix in ['', '-wal', '-shm', '-journal']:
            excluded.append({'path': str(path) + suffix, 'reason': '数据库单独生成一致快照'})
    rows = [{'source': str(p), 'relative': p.relative_to(home).as_posix(),
             'target': str(Path(target['home']) / p.relative_to(home))} for p in roots]
    agent.validate_staging_root(root_path, target['home'], rows)
    plan = {'id': ident, 'name': body.get('name') or 'Codex 跨机迁移', **probe, 'root': str(root_path),
            'source_home': str(home), 'target_home': target['home'], 'source': agent.inspect_home(home),
            'target': target, 'roots': rows, 'databases': [str(p) for p in databases],
            'excluded': excluded, 'missing_source_roots': sorted(set(missing)), 'policies': resource_policies(),
            'status': 'planned', 'phase': 'plan', 'detail': '方案已生成；来源保留，目标先隔离接收',
            'created_at': time.time(), 'progress': {}, 'source_deleted': False, 'activation': 'not_started'}
    plan['desktop_archives'] = desktop_archives(home)
    plan['historical_workspace_discovery'] = discovery
    plan['source_missing_transient_workspaces'] = missing_transient_workspaces(home)
    bins = []
    for name in ['node', 'npm', 'uv', 'uvx', 'hf', 'lark-cli']:
        executable = shutil.which(name)
        if executable:
            directory = Path(executable).parent
            if any(directory.is_relative_to(root) for root in roots):
                bins.append(directory.relative_to(home).as_posix())
    plan['tool_runtime'] = {'bin_dirs': list(dict.fromkeys(bins)), 'target_existing_settings_preferred': True}
    node_bin = user_node_bin(home, roots)
    if node_bin and node_bin not in plan['tool_runtime']['bin_dirs']:
        plan['tool_runtime']['bin_dirs'].append(node_bin)
    include_active_files(plan)
    if body.get('include_atlas', True):
        plan['atlas'] = {'catalog': db.DATA.relative_to(home).as_posix(),
                         'archive': db.blob_root().relative_to(home).as_posix(),
                         'application': Path(__file__).resolve().parents[1].relative_to(home).as_posix(),
                         'python_environment': Path(sys.prefix).relative_to(home).as_posix()}
    folder(ident).mkdir(parents=True, mode=0o700)
    save(plan)
    with db.connect() as c:
        db.audit(c, 'codex_migration_plan', {'id': ident, 'host': plan['host'], 'roots': len(rows)})
    return plan


@functools.lru_cache(maxsize=32)
def _exclusion_rules(paths):
    exact = frozenset(str(Path(p)) for p in paths)
    return exact, tuple(p.rstrip(os.sep) + os.sep for p in exact)


def filtered(plan, path):
    exact, descendants = _exclusion_rules(tuple(row['path'] for row in plan['excluded']))
    value = str(path)
    return value in exact or value.startswith(descendants)


def _rsync_once(plan, source, destination, log, checksum=False, root=None):
    args = ['rsync', '-aH', '--no-owner', '--no-group', '--partial', '--partial-dir=.atlas-partial',
            '--timeout=90', '--stats', '--progress']
    # Apple's openrsync -E fails restoring xattrs to read-only Git objects and
    # treats renamed single-file destinations as directories. Transfer xattrs
    # explicitly in the hash-verified manifest, then restore and verify them.
    if checksum:
        args.append('--checksum')
    if root:
        for row in plan['excluded']:
            p = Path(row['path'])
            if p.is_relative_to(root):
                args += ['--exclude=/' + p.relative_to(root).as_posix()]
    shell = shlex.join(ssh_arguments(plan)[:-1])
    if plan.get('target', {}).get('platform') == 'Darwin':
        args += ['--rsync-path=/usr/bin/caffeinate -i /usr/bin/rsync']
    args += ['-e', shell, str(source), plan['host'] + ':' + shlex.quote(destination)]
    with log.open('ab') as output:
        process = subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT)
        while process.poll() is None:
            if (folder(plan['id']) / 'cancel').exists():
                process.terminate()
                process.wait(timeout=15)
                raise InterruptedError('迁移已暂停；已传文件保留，可续传')
            plan['progress']['transfer_log_bytes'] = log.stat().st_size
            with log.open('rb') as progress_log:
                progress_log.seek(max(0, log.stat().st_size - 4096))
                recent = progress_log.read().decode(errors='replace')
            matches = list(re.finditer(r'\(xfer#(\d+), to-check=(\d+)/(\d+)\)', recent))
            if matches:
                match = matches[-1]
                plan['progress'].update(files_transferred_in_attempt=int(match[1]),
                    directory_entries=int(match[3]), last_activity_at=time.time())
            if log.stat().st_size > 64 * 1024**2:
                # Worker stdout uses O_APPEND. Bound diagnostic disk consumption.
                with log.open('wb'):
                    pass
            save(plan, transfer_pid=process.pid)
            time.sleep(2)
        if process.returncode:
            with log.open('rb') as f:
                f.seek(max(0, log.stat().st_size - 1600))
                tail = f.read().decode(errors='replace')
            raise ValueError('文件传输未完成，可续传：' + tail)


def rsync(plan, source, destination, log, checksum=False, root=None):
    for attempt in range(5):
        try:
            return _rsync_once(plan, source, destination, log, checksum, root)
        except ValueError as exc:
            retryable = any(word in str(exc).lower() for word in
                ['timed out', 'broken pipe', 'connection reset', 'connection closed', 'unexpected end of file', 'no route to host'])
            if not retryable or attempt == 4:
                raise
            seconds = min(10 * 2 ** attempt, 60)
            save(plan, detail='连接中断，%s 秒后第 %s 次断点续传' % (seconds, attempt + 1))
            for _ in range(seconds):
                if (folder(plan['id']) / 'cancel').exists():
                    raise InterruptedError('迁移已暂停，已传文件保留')
                time.sleep(1)


def snapshot_database(path, destination):
    if destination.exists():
        destination.unlink()
    with contextlib.closing(agent.connect_existing(path)) as source, contextlib.closing(sqlite3.connect(destination)) as target:
        source.backup(target, pages=256, sleep=0.01)
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('数据库快照完整性失败：' + str(path))
        target.execute('PRAGMA journal_mode=DELETE')
    destination.chmod(0o600)
    return {'sha256': agent.digest(destination), 'size': destination.stat().st_size, 'mode': 0o600,
            'xattrs': agent.attributes(path), 'captured_at': time.time()}


def snapshot_file(path, destination, preserve_mode=False):
    """Freeze append-only rollouts without holding a multi-GiB file in memory."""
    destination.unlink(missing_ok=True)
    if sys.platform == 'darwin':
        result = subprocess.run(['/bin/cp', '-c', '-p', str(path), str(destination)], capture_output=True)
        if result.returncode:
            raise ValueError('无法建立文件一致副本：' + result.stderr.decode(errors='replace')[-500:])
    else:
        before = path.stat()
        shutil.copy2(path, destination)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('活动会话正在变化，请暂停该会话后续传')
    if path.suffix == '.jsonl' and destination.stat().st_size:
        with destination.open('rb') as source:
            source.seek(-1, 2)
            if source.read(1) != b'\n':
                raise ValueError('会话末尾仍在写入，请稍后续传：' + str(path))
    mode = stat.S_IMODE(destination.stat().st_mode) if preserve_mode else 0o600
    destination.chmod(mode)
    return {'sha256': agent.digest(destination), 'size': destination.stat().st_size, 'mode': mode,
            'xattrs': agent.attributes(destination), 'captured_at': time.time()}


def _manifest_entry(p, home, relative_root):
    st = p.lstat()
    item = {'path': p.relative_to(home).as_posix(), 'mode': stat.S_IMODE(st.st_mode)}
    link = None
    if not p.is_symlink():
        item['xattrs'] = agent.attributes(p)
    if p.is_symlink():
        target = os.readlink(p)
        item.update(type='symlink', target=target)
        if os.path.isabs(target) or not p.exists():
            link = {'path': item['path'], 'target': target, 'broken_at_source': not p.exists()}
    elif stat.S_ISDIR(st.st_mode):
        item['type'] = 'directory'
    elif stat.S_ISREG(st.st_mode):
        item.update(type='file', size=st.st_size, sha256=agent.digest(p))
        if st.st_nlink > 1:
            item['hardlink_group'] = relative_root + ':' + str(st.st_dev) + ':' + str(st.st_ino)
        after = p.stat()
        if (st.st_size, st.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('清单捕获时文件变化，请续传重试：' + str(p))
    else:
        return None, None
    return item, link


def make_manifest(plan):
    home = Path(plan['source_home'])
    path = folder(plan['id']) / 'source-manifest.jsonl'
    files = 0
    size = 0
    links = []
    def paths():
        for row in plan['roots']:
            root = Path(row['source'])
            for base, dirs, names in os.walk(root, followlinks=False):
                base = Path(base)
                dirs[:] = sorted(d for d in dirs if not filtered(plan, base / d))
                for p in [base] + [base / d for d in dirs if (base / d).is_symlink()] + [base / n for n in sorted(names)]:
                    if filtered(plan, p):
                        continue
                    yield p, row.get('relative', root.relative_to(home).as_posix())
    # A bounded queue overlaps small-file I/O without retaining the whole tree
    # or file contents. Output order remains deterministic.
    with path.open('w', encoding='utf-8') as out, concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        pending = collections.deque()
        source = iter(paths())
        exhausted = False
        while pending or not exhausted:
            while len(pending) < 8 and not exhausted:
                try:
                    p, relative = next(source)
                    pending.append(pool.submit(_manifest_entry, p, home, relative))
                except StopIteration:
                    exhausted = True
            if not pending:
                break
            item, link = pending.popleft().result()
            if item is None:
                continue
            if link:
                links.append(link)
            out.write(json.dumps(item, ensure_ascii=False) + '\n')
            files += 1
            size += item.get('size', 0)
            if files % 200 == 0:
                if (folder(plan['id']) / 'cancel').exists():
                    raise InterruptedError('清单捕获已暂停，可续传')
                save(plan, progress={'entries_hashed': files, 'bytes_hashed': size})
        for item in plan.get('database_snapshots', []):
            out.write(json.dumps(item, ensure_ascii=False) + '\n')
            files += 1
            size += item.get('size', 0)
    path.chmod(0o600)
    agent.atomic_json(folder(plan['id']) / 'link-review.json', links)
    save(plan, manifest={'entries': files, 'bytes': size, 'sha256': agent.digest(path), 'link_reviews': len(links)})
    return path


def transfer(ident):
    plan = read(ident)
    location = folder(ident)
    with (location / 'worker.lock').open('a') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.write('0'); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (location / 'cancel').unlink(missing_ok=True)
        try:
            if sys.platform == 'darwin':
                subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            save(plan, status='running', worker_pid=os.getpid(), phase='prepare', detail='准备目标隔离目录')
            remote(plan, 'prepare', source_home=plan['source_home'], keep_awake=True)
            remote(plan, 'mkdir', paths=[r['relative'] for r in plan['roots']])
            log = location / 'transfer.log'
            if not plan.get('skip_initial_copy'):
                for i, row in enumerate(plan['roots']):
                    if i < plan.get('resume_copy_index', 0):
                        continue
                    save(plan, phase='copy', detail='传输 ' + row['relative'], progress={'root': i, 'roots': len(plan['roots'])})
                    rsync(plan, row['source'] + '/', plan['root'] + '/incoming/' + row['relative'] + '/', log, root=Path(row['source']))
            if not plan.get('reuse_snapshots'):
                save(plan, phase='databases', detail='逐个保存并发送数据库一致快照', database_snapshots=[], capture_started_at=time.time())
                for value in plan['databases']:
                    source = Path(value)
                    snapshot = location / 'database-snapshot.sqlite'
                    snapshot.unlink(missing_ok=True)  # Only this task's previous scratch snapshot.
                    reserve = 384 * 1024**2
                    if shutil.disk_usage(location).free < source.stat().st_size + reserve:
                        raise ValueError('本机暂存一份数据库的空间不足；保留文件，可调整暂存位置后续传')
                    info = snapshot_database(source, snapshot)
                    relative = source.relative_to(plan['source_home']).as_posix()
                    remote(plan, 'mkdir', paths=[str(Path(relative).parent)])
                    rsync(plan, snapshot, plan['root'] + '/incoming/' + relative, log)
                    plan['database_snapshots'].append({'path': relative, 'type': 'file', **info})
                    save(plan)
                    snapshot.unlink()
                codex = Path(plan['source_home']) / '.codex'
                persistent = []
                for name in ['sessions', 'archived_sessions']:
                    persistent += [p for p in (codex / name).rglob('*') if p.is_file() and not p.is_symlink()]
                    base = codex / name
                    if base.exists():
                        for directory in [base] + [p for p in base.rglob('*') if p.is_dir() and not p.is_symlink()]:
                            plan['database_snapshots'].append({'path': directory.relative_to(plan['source_home']).as_posix(),
                                'type': 'directory', 'mode': stat.S_IMODE(directory.stat().st_mode),
                                'xattrs': agent.attributes(directory), 'snapshot_directory': True})
                directories = [r['path'] for r in plan['database_snapshots'] if r['type'] == 'directory']
                if directories:
                    remote(plan, 'mkdir', paths=directories)
                persistent += [codex / name for name in ['.codex-global-state.json', '.codex-global-state.json.bak', 'session_index.jsonl'] if (codex / name).is_file()]
                persistent += [Path(p) for p in plan.get('desktop_archives', desktop_archives(plan['source_home']))]
                persistent += [Path(p) for p in plan.get('frozen_files', [])]
                for source in persistent:
                    snapshot = location / 'persistent-snapshot'
                    info = snapshot_file(source, snapshot, preserve_mode=str(source) in plan.get('frozen_files', []))
                    relative = source.relative_to(plan['source_home']).as_posix()
                    remote(plan, 'mkdir', paths=[str(Path(relative).parent)])
                    rsync(plan, snapshot, plan['root'] + '/incoming/' + relative, log)
                    plan['database_snapshots'].append({'path': relative, 'type': 'file', **info})
                    save(plan)
                    snapshot.unlink()
                save(plan, captured_at=time.time())
            save(plan, phase='manifest', detail='流式计算来源 SHA-256 清单；内存使用不随文件总大小增长')
            manifest = make_manifest(plan)
            save(plan, phase='reconcile', detail='核对初次复制后的文件差异')
            for row in plan['roots']:
                # Reconcile normal changes cheaply. The following full SHA-256
                # verification, not rsync timestamps, is the acceptance gate.
                rsync(plan, row['source'] + '/', plan['root'] + '/incoming/' + row['relative'] + '/', log, root=Path(row['source']))
            rsync(plan, manifest, plan['root'] + '/evidence/source-manifest.jsonl', log)
            save(plan, phase='verify', detail='目标逐文件 SHA-256、路径、权限与软链接校验')
            result = remote(plan, 'verify', timeout=24 * 3600, restore_xattrs=True,
                            manifest_sha256=plan['manifest']['sha256'], quarantine_extras=True)
            agent.atomic_json(location / 'verification.json', result)
            if result['status'] != 'passed':
                raise ValueError('目标校验未通过，禁止激活；详见验证报告')
            save(plan, status='ready_to_activate', phase='verified', detail='内容校验通过；尚需目标 Codex 接续验收', result=result)
        except InterruptedError as exc:
            save(plan, status='paused', detail=str(exc))
        except BaseException as exc:
            save(plan, status='failed', detail=str(exc)[:3000])
            raise


def start(ident):
    plan = read(ident)
    if plan['status'] == 'running':
        pid = plan.get('worker_pid')
        if pid:
            try:
                os.kill(pid, 0)
                raise ValueError('此迁移仍在执行')
            except ProcessLookupError:
                pass
    if plan['status'] not in {'planned', 'failed', 'paused', 'running'}:
        raise ValueError('当前阶段无需重新传输')
    if plan.get('phase') in {'databases', 'manifest', 'reconcile', 'verify'}:
        plan['skip_initial_copy'] = True
        plan['reuse_snapshots'] = plan['phase'] != 'databases' and bool(plan.get('captured_at'))
        plan.setdefault('desktop_archives', desktop_archives(plan['source_home']))
        save(plan)
    elif plan.get('phase') == 'copy':
        plan['resume_copy_index'] = plan.get('progress', {}).get('root', 0)
        save(plan)
    with (folder(ident) / 'worker.log').open('ab') as out:
        proc = subprocess.Popen([sys.executable, '-m', 'asset_manager.codex_migration', 'worker', ident],
            cwd=Path(__file__).resolve().parents[1], env={**os.environ, 'ASSET_DATA_DIR': str(db.DATA)},
            stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
    # The worker owns subsequent status writes. Do not race its first update.
    return {'id': ident, 'pid': proc.pid, 'status': 'started'}


def stop(ident):
    read(ident)
    (folder(ident) / 'cancel').touch(mode=0o600)
    return {'id': ident, 'status': 'pause_requested'}


def activation_worker(ident, activate=False):
    with (folder(ident) / 'worker.lock').open('a') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.write('0'); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _activation_worker(ident, activate)


def _activation_worker(ident, activate=False):
    plan = read(ident)
    try:
        save(plan, status='preparing_activation', detail='在远端副本中合并会话、项目和记忆；保留目标原有资料')
        action = 'activate' if activate else 'prepare_activation'
        if not activate:
            capture_external_attachments(plan)
        report = remote(plan, action, timeout=24 * 3600, roots=plan['roots'], allow_app_restart=activate,
                        refresh=not activate, attachments_sha256=plan.get('external_attachments', {}).get('manifest_sha256'),
                        tool_runtime=plan.get('tool_runtime', {}))
        save(plan, status='activated_pending_acceptance' if activate else 'activation_prepared',
             activation=report, detail='已接入目标 Codex，待原生功能验收' if activate else '接续副本已准备，原有环境尚未切换')
    except BaseException as exc:
        save(plan, status='activation_failed', detail=str(exc)[:3000])
        raise


def capture_external_attachments(plan):
    refs = remote(plan, 'attachment_references', timeout=120)
    roots = [Path(r['source']) for r in plan['roots']]
    remote(plan, 'prepare_attachment_inputs')
    result = {'files': [], 'missing_external_paths': [], 'captured_at': time.time()}
    location = folder(plan['id'])
    for row in refs:
        path = Path(row['path'])
        if any(path.is_relative_to(r) for r in roots):
            continue
        if not path.is_file():
            result['missing_external_paths'].append(row)
            continue
        snapshot = location / 'attachment-snapshot'
        info = snapshot_file(path, snapshot)
        name = hashlib.sha256(str(path).encode()).hexdigest()[:20] + '-' + info['sha256'][:20] + path.suffix[:12]
        rsync(plan, snapshot, plan['root'] + '/supplements/attachments/' + name, location / 'attachments-transfer.log')
        result['files'].append({'source': str(path), 'name': name, **info})
        snapshot.unlink()
    manifest = location / 'attachment-inputs.json'
    agent.atomic_json(manifest, result)
    rsync(plan, manifest, plan['root'] + '/evidence/attachment-inputs.json', location / 'attachments-transfer.log')
    save(plan, external_attachments={'copied': len(result['files']),
         'missing_before_migration': len(result['missing_external_paths']), 'manifest_sha256': agent.digest(manifest)})


def start_activation(ident, activate=False):
    plan = read(ident)
    required = 'activation_prepared' if activate else 'ready_to_activate'
    if plan['status'] not in {required, 'activation_failed'}:
        raise ValueError('当前阶段不能执行接续操作：' + str(plan['status']))
    with (folder(ident) / 'activation-worker.log').open('ab') as out:
        proc = subprocess.Popen([sys.executable, '-m', 'asset_manager.codex_migration',
            'activate' if activate else 'prepare-profile', ident], cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, 'ASSET_DATA_DIR': str(db.DATA)}, stdin=subprocess.DEVNULL,
            stdout=out, stderr=out, start_new_session=True)
    return {'id': ident, 'pid': proc.pid, 'status': 'started'}


def verify_native(ident):
    plan = read(ident)
    if plan['status'] not in {'activated_pending_acceptance', 'native_verified'}:
        raise ValueError('请先接入目标环境，再执行原生验收')
    report = remote(plan, 'accept_native', timeout=3600)
    agent.atomic_json(folder(ident) / 'native-acceptance.json', report)
    save(plan, status='native_verified' if report['status']=='passed' else 'activated_pending_acceptance',
         native_acceptance=report, detail='原生 Codex 会话和历史读取验收通过；工程运行依赖需单独验收' if report['status']=='passed' else '原生验收发现问题，保留全部原始副本')
    return report


def deploy_atlas(ident, port=8765):
    plan = read(ident)
    if plan['status'] not in {'activated_pending_acceptance', 'native_verified'}:
        raise ValueError('请先接入目标环境')
    if not plan.get('atlas'):
        home = Path(plan['source_home'])
        plan['atlas'] = {'catalog': db.DATA.relative_to(home).as_posix(),
            'archive': db.blob_root().relative_to(home).as_posix(),
            'application': Path(__file__).resolve().parents[1].relative_to(home).as_posix(),
            'python_environment': Path(sys.prefix).relative_to(home).as_posix()}
    release = folder(ident) / 'atlas-release.zip'
    package = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/package-release.py'), str(release)], capture_output=True, timeout=300)
    if package.returncode:
        raise ValueError('Atlas 发布包生成失败：' + package.stderr.decode(errors='replace')[-1000:])
    rsync(plan, release, plan['root'] + '/evidence/atlas-release.zip', folder(ident) / 'deployment-transfer.log')
    report = remote(plan, 'deploy_atlas', timeout=1800, atlas=plan['atlas'], port=port, plan=plan,
                    release_sha256=agent.digest(release))
    agent.atomic_json(folder(ident) / 'atlas-deployment.json', report)
    save(plan, atlas_deployment=report)
    return report


def reports(ident):
    plan = read(ident)
    if plan.get('role') == 'receiver':
        root = agent.checked_root(plan['root'])
        marker = json.loads((root / 'ATLAS-MIGRATION.json').read_text())
        if marker['id'] != ident:
            raise ValueError('接收回执与任务不匹配')
        return {p.name: json.loads(p.read_text()) for p in (root / 'evidence').glob('*.json')
                if p.name in {'verification.json', 'activation.json', 'native-acceptance.json',
                              'atlas-deployment.json', 'workspace-acceptance.json', 'completion.json',
                              'attachment-recovery.json', 'attachment-recovery-progress.json',
                              'tool-verification.json', 'tool-installation.json', 'python-runtime-rebinding.json', 'python-runtime-acceptance.json', 'rollout-path-rebinding.json'}}
    return remote(plan, 'report')


def install_tools(ident, supplement_id):
    plan, supplement = read(ident), read(supplement_id)
    if plan['status'] not in {'activated_pending_acceptance', 'native_verified'}:
        raise ValueError('请先接入目标 Codex')
    if supplement.get('parent_migration') != ident or supplement.get('verification', {}).get('status') != 'passed':
        raise ValueError('补充任务不属于本次迁移或未通过文件校验')
    node_bin = supplement.get('source_node_bin') or user_node_bin(plan['source_home'], [r['source'] for r in supplement['roots']])
    if not node_bin:
        raise ValueError('无法确定来源用户 Node 运行版本')
    save(supplement, source_node_bin=node_bin)
    candidates = [('node', node_bin + '/node', ['--version'])] + [
        (name, '.local/bin/' + name, ['version'] if name == 'hf' else ['--version'])
        for name in ['uv', 'uvx', 'lark-cli', 'hf', 'claude']]
    probes = [{'name': name, 'relative': relative, 'args': args} for name, relative, args in candidates
              if (Path(plan['source_home']) / relative).is_file()]
    entry = str(Path(plan['atlas']['catalog']).parent / 'tools/migration-env.sh') if plan.get('atlas') else '.config/atlas/migration-env.sh'
    report = remote(plan, 'install_tools', timeout=1800, supplement_root=supplement['root'], supplement_id=supplement_id,
        tool_roots=[r['relative'] for r in supplement['roots']], node_bin=node_bin, probes=probes, environment_entry=entry,
        verify_only=bool(supplement.get('tool_installation')))
    agent.atomic_json(folder(ident) / 'tool-installation.json', report)
    save(plan, tool_supplement={'id': supplement_id, 'status': report['status'],
         'entries': supplement['verification']['entries'], 'bytes': supplement['verification']['bytes']})
    save(supplement, status='tool_installed' if report['status'] == 'passed' else 'needs_attention', tool_installation=report)
    return report


def repair_paths(ident):
    plan = read(ident)
    if plan['status'] not in {'activated_pending_acceptance', 'native_verified'}:
        raise ValueError('请先接入目标环境')
    report = remote(plan, 'repair_active_paths', timeout=1800, allow_app_restart=True)
    agent.atomic_json(folder(ident) / 'rollout-path-rebinding.json', report)
    save(plan, status='activated_pending_acceptance', rollout_paths=report,
         native_acceptance=None, workspace_acceptance=None, detail='日志与数据库路径已重绑定，等待重新验收')
    return report


def repair_runtimes(ident):
    plan = read(ident)
    if plan['status'] != 'activation_prepared':
        raise ValueError('请先准备接续副本')
    report = remote(plan, 'repair_runtimes', timeout=600)
    plan['activation']['python_runtimes'] = report
    save(plan)
    return report


def verify_workspaces(ident):
    plan = read(ident)
    if plan['status'] not in {'activated_pending_acceptance', 'native_verified', 'complete'}:
        raise ValueError('请先接入目标环境')
    if 'source_missing_transient_workspaces' not in plan:
        plan['source_missing_transient_workspaces'] = missing_transient_workspaces(plan['source_home'])
        save(plan)
    report = remote(plan, 'verify_workspaces', timeout=7200,
                    source_missing_transient_workspaces=plan['source_missing_transient_workspaces'])
    agent.atomic_json(folder(ident) / 'workspace-acceptance.json', report)
    save(plan, workspace_acceptance=report)
    return report


def recover_worktree(ident, thread_id, repository):
    plan = read(ident)
    if plan['status'] not in {'activated_pending_acceptance', 'native_verified'}:
        raise ValueError('请先接入目标环境')
    report = remote(plan, 'recover_worktree', timeout=360, thread_id=thread_id, repository=repository)
    plan.setdefault('reconstructed_worktrees', []).append(report)
    save(plan)
    return report


def complete(ident):
    plan = read(ident)
    receipt = reports(ident)
    for name in ['verification.json', 'native-acceptance.json', 'workspace-acceptance.json']:
        if receipt.get(name, {}).get('status') != 'passed':
            raise ValueError('验收尚未通过：' + name)
    if plan.get('atlas') and receipt.get('atlas-deployment.json', {}).get('status') != 'running':
        raise ValueError('目标 Atlas 尚未完成部署')
    if plan.get('tool_supplement') and receipt.get('tool-installation.json', {}).get('status') != 'passed':
        raise ValueError('补充工具尚未通过目标运行验收')
    result = remote(plan, 'complete', timeout=60)
    save(plan, status='complete', phase='complete', completion=result,
         detail='文件、原生会话、工程 Git 与目标 Atlas 验收通过；源机保留，工程构建和外部设备未逐项执行')
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Atlas Codex 迁移')
    parser.add_argument('action', choices=['plan', 'start', 'worker', 'status', 'prepare-profile', 'activate', 'verify-native', 'deploy-atlas', 'verify-workspaces', 'repair-runtimes', 'repair-paths', 'install-tools', 'complete'])
    parser.add_argument('value', nargs='?')
    parser.add_argument('--supplement')
    args = parser.parse_args()
    if args.action == 'worker':
        transfer(args.value)
    elif args.action in {'prepare-profile', 'activate'}:
        activation_worker(args.value, args.action == 'activate')
    elif args.action == 'verify-native':
        print(json.dumps(verify_native(args.value),ensure_ascii=False))
    elif args.action == 'deploy-atlas':
        print(json.dumps(deploy_atlas(args.value),ensure_ascii=False))
    elif args.action == 'verify-workspaces':
        print(json.dumps(verify_workspaces(args.value),ensure_ascii=False))
    elif args.action == 'install-tools':
        if not args.supplement:
            parser.error('install-tools requires --supplement TASK_ID')
        print(json.dumps(install_tools(args.value, args.supplement), ensure_ascii=False))
    elif args.action == 'repair-paths':
        print(json.dumps(repair_paths(args.value), ensure_ascii=False))
    elif args.action == 'repair-runtimes':
        print(json.dumps(repair_runtimes(args.value), ensure_ascii=False))
    elif args.action == 'complete':
        print(json.dumps(complete(args.value),ensure_ascii=False))
    elif args.action == 'plan':
        print(json.dumps(make_plan(json.loads(Path(args.value).read_text())), ensure_ascii=False, indent=2))
    elif args.action == 'start':
        print(json.dumps(start(args.value)))
    else:
        print(json.dumps(read(args.value) if args.value else listing(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
