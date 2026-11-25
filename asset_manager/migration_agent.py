"""Standalone, standard-library migration inspection and verification on a destination.

This module is shipped by Atlas over SSH. No shell expansion or source credentials
are needed. The receipt distinguishes files on disk from a usable Codex profile.
"""
import base64
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path


def _mac_xattr(function, path, name=None, data=None):
    import ctypes
    import errno
    lib = ctypes.CDLL(None, use_errno=True)
    call = getattr(lib, function)
    call.restype = ctypes.c_ssize_t if function != 'setxattr' else ctypes.c_int
    if function == 'listxattr':
        args = [os.fsencode(path)]
        tail = [ctypes.c_int(0)]
    else:
        args = [os.fsencode(path), os.fsencode(name)]
        tail = [ctypes.c_uint32(0), ctypes.c_int(0)]
    if function == 'setxattr':
        buf = ctypes.create_string_buffer(data)
        result = call(*args, buf, ctypes.c_size_t(len(data)), *tail)
        if result < 0:
            raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()), str(path))
        return
    for _ in range(3):
        count = call(*args, None, ctypes.c_size_t(0), *tail)
        if count < 0:
            raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()), str(path))
        buf = ctypes.create_string_buffer(count)
        read = call(*args, buf, ctypes.c_size_t(count), *tail)
        if read >= 0:
            return buf.raw[:read]
        if ctypes.get_errno() != errno.ERANGE:
            break
    raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()), str(path))


def get_attribute(path, name):
    return _mac_xattr('getxattr', path, name) if sys.platform == 'darwin' else os.getxattr(path, name)


def set_attribute(path, name, data):
    if sys.platform == 'darwin':
        _mac_xattr('setxattr', path, name, data)
    else:
        os.setxattr(path, name, data)


def attributes(path):
    if sys.platform == 'darwin':
        names = [os.fsdecode(x) for x in _mac_xattr('listxattr', path).split(b'\0') if x]
    elif hasattr(os, 'listxattr'):
        names = os.listxattr(path)
    else:
        return {}
    return {name: base64.b64encode(get_attribute(path, name)).decode() for name in names}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w', encoding='utf-8') as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    temp.chmod(0o600)
    os.replace(temp, path)


def run(args, timeout=20):
    result = subprocess.run(args, capture_output=True, timeout=timeout)
    return result.stdout.decode('utf-8', errors='replace').strip() if not result.returncode else ''


def connect_existing(path):
    """query_only prevents application writes; rw lets SQLite create missing WAL shm.

    A WAL database with no shm cannot always be opened with mode=ro. mode=rw
    never creates a nonexistent database. Do not use immutable on a live database.
    """
    con = sqlite3.connect(Path(path).as_uri() + '?mode=rw', uri=True, timeout=30)
    con.execute('PRAGMA query_only=ON')
    return con


def inspect_home(home=None):
    home = Path(home or Path.home()).resolve()
    codex = home / '.codex'
    result = {'home': str(home), 'platform': platform.system(), 'architecture': platform.machine(),
              'free_bytes': shutil.disk_usage(home).free, 'codex_exists': codex.is_dir(),
              'databases': [], 'memory': {}, 'apps': []}
    if sys.platform == 'darwin':
        import plistlib
        result['memory'] = {'physical_bytes': int(run(['sysctl', '-n', 'hw.memsize']) or 0),
                            'swap': run(['sysctl', '-n', 'vm.swapusage']),
                            'pressure': run(['memory_pressure', '-Q']).splitlines()[-1:]}
        for name in ['ChatGPT', 'Codex']:
            p = Path('/Applications') / (name + '.app') / 'Contents/Info.plist'
            if p.is_file():
                d = plistlib.loads(p.read_bytes())
                result['apps'].append({'name': name, 'version': d.get('CFBundleShortVersionString'),
                                       'build': d.get('CFBundleVersion')})
    for p in sorted(list(codex.glob('*.sqlite')) + list((codex / 'sqlite').glob('*.db'))):
        item = {'path': p.relative_to(home).as_posix(), 'bytes': p.stat().st_size}
        try:
            c = connect_existing(p)
            tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in ['threads', 'projects', 'thread_turns', 'thread_items']:
                if table in tables:
                    item[table] = c.execute('SELECT count(*) FROM ' + table).fetchone()[0]
            if 'threads' in tables:
                item['cwd'] = [r[0] for r in c.execute('SELECT DISTINCT cwd FROM threads')]
                columns = {r[1] for r in c.execute('PRAGMA table_info(threads)')}
                if 'creator_account_id' in columns:
                    item['account_fingerprints'] = [hashlib.sha256(str(r[0]).encode()).hexdigest()
                        for r in c.execute('SELECT DISTINCT creator_account_id FROM threads WHERE creator_account_id IS NOT NULL')]
            if '_sqlx_migrations' in tables:
                item['migrations'] = [[r[0], bytes(r[1]).hex()] for r in c.execute('SELECT version,checksum FROM _sqlx_migrations ORDER BY version')]
            c.close()
        except Exception as exc:
            item['error'] = str(exc)
        result['databases'].append(item)
    return result


def checked_root(value, create=False):
    root = Path(value).expanduser()
    home = Path.home().resolve()
    if not root.is_absolute() or '..' in root.parts or root.is_symlink():
        raise ValueError('迁移暂存目录必须是规范绝对路径，不能是软链接')
    root = root.resolve()
    if not root.is_relative_to(home) or len(root.relative_to(home).parts) < 2:
        raise ValueError('迁移暂存目录必须位于目标用户的专用子目录')
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def validate_staging_root(root, target_home, roots, resolve_paths=False):
    """Never clone a destination profile into a descendant of itself."""
    root, home = Path(root), Path(target_home)
    if resolve_paths:
        root, home = root.resolve(), home.resolve()
    for row in roots:
        relative = Path(row['relative'])
        if relative.is_absolute() or '..' in relative.parts or relative == Path('.'):
            raise ValueError('接续目录越界')
        destination = home / relative
        if resolve_paths:
            destination = destination.resolve()
        if not destination.is_relative_to(home):
            raise ValueError('接续目录穿过外部链接')
        if root.is_relative_to(destination) or destination.is_relative_to(root):
            raise ValueError('暂存目录不能与接续目录重叠：' + str(relative))


def prepare(body):
    root = checked_root(body['root'], True)
    marker = root / 'ATLAS-MIGRATION.json'
    if marker.exists():
        existing = json.loads(marker.read_text())
        if existing['id'] != body['id'] or existing.get('source_home') != body['source_home']:
            raise ValueError('暂存目录属于另一个迁移任务')
    elif any(root.iterdir()):
        raise ValueError('拒绝使用已有资料的非空目录')
    else:
        atomic_json(marker, {'id': body['id'], 'source_home': body['source_home'], 'created_at': time.time()})
    (root / 'incoming').mkdir(exist_ok=True, mode=0o700)
    (root / 'evidence').mkdir(exist_ok=True, mode=0o700)
    if body.get('keep_awake') and sys.platform == 'darwin':
        power = root / 'evidence/power-assertion.json'
        existing = json.loads(power.read_text()) if power.exists() else {}
        same = existing.get('pid') and run(['ps', '-p', str(existing['pid']), '-o', 'lstart=']).strip() == existing.get('started')
        if not same:
            proc = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-t', '43200'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            atomic_json(power, {'pid': proc.pid, 'started': run(['ps', '-p', str(proc.pid), '-o', 'lstart=']).strip(),
                               'expires_after_seconds': 43200, 'purpose': 'migration_idle_sleep_assertion'})
    return {'root': str(root), 'free_bytes': shutil.disk_usage(root).free}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def quarantine_extra_files(root, manifest):
    """Keep interrupted-copy leftovers out of the activated snapshot, without loss.

    Use a disk-backed path set rather than retaining millions of names in RAM.
    Extra incoming files are moved into the task evidence area, never deleted.
    """
    index = root / 'evidence/manifest-paths.sqlite'
    con = sqlite3.connect(index)
    con.execute('CREATE TABLE IF NOT EXISTS paths(path TEXT PRIMARY KEY)')
    con.execute('DELETE FROM paths')
    with manifest.open() as source:
        for line in source:
            path = Path(json.loads(line)['path'])
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('清单路径越界')
            con.execute('INSERT OR IGNORE INTO paths VALUES(?)', (path.as_posix(),))
            for parent in path.parents:
                con.execute('INSERT OR IGNORE INTO paths VALUES(?)', (parent.as_posix(),))
    con.commit()
    incoming = root / 'incoming'
    quarantine = root / 'evidence' / ('incoming-extras-' + str(time.time_ns()))
    count = 0
    examples = []
    for base, dirs, files in os.walk(incoming, followlinks=False):
        base = Path(base)
        for name in list(dirs) + files:
            path = base / name
            rel = path.relative_to(incoming)
            if not con.execute('SELECT 1 FROM paths WHERE path=?', (rel.as_posix(),)).fetchone():
                target = quarantine / rel
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.rename(path, target)
                if name in dirs:
                    dirs.remove(name)
                count += 1
                if len(examples) < 100:
                    examples.append(rel.as_posix())
    con.close()
    return {'entries_moved': count, 'location': str(quarantine) if count else None, 'examples': examples}


def verify(body):
    root = checked_root(body['root'])
    manifest = root / 'evidence/source-manifest.jsonl'
    if body.get('manifest_sha256') and digest(manifest) != body['manifest_sha256']:
        raise ValueError('迁移清单本身的 SHA-256 不一致')
    extras = quarantine_extra_files(root, manifest) if body.get('quarantine_extras') else None
    done = 0
    total = 0
    errors = []
    hardlinks = {}
    started = time.time()
    with manifest.open(encoding='utf-8') as f:
        for line in f:
            item = json.loads(line)
            rel = Path(item['path'])
            if rel.is_absolute() or '..' in rel.parts:
                raise ValueError('清单路径越界')
            p = root / 'incoming' / rel
            if not p.parent.resolve().is_relative_to((root / 'incoming').resolve()):
                raise ValueError('清单父目录存在越界链接')
            try:
                if item['type'] == 'symlink':
                    if not p.is_symlink() or os.readlink(p) != item['target']:
                        raise ValueError('软链接目标不一致')
                elif item['type'] == 'directory':
                    if p.is_symlink() or not p.is_dir():
                        raise ValueError('目录缺失')
                    if item.get('snapshot_directory') and body.get('restore_xattrs'):
                        p.chmod(item['mode'])
                else:
                    if p.is_symlink() or not p.is_file() or p.stat().st_size != item['size'] or digest(p) != item['sha256']:
                        raise ValueError('大小或 SHA-256 不一致')
                    total += item['size']
                    if item.get('hardlink_group'):
                        identity = (p.stat().st_dev, p.stat().st_ino)
                        previous = hardlinks.setdefault(item['hardlink_group'], identity)
                        if identity != previous:
                            raise ValueError('目录内硬链接关系不一致')
                if not p.is_symlink() and stat.S_IMODE(p.stat().st_mode) != item['mode']:
                    raise ValueError('权限不一致')
                if not p.is_symlink() and item.get('xattrs'):
                    if sys.platform != 'darwin' and not hasattr(os, 'getxattr'):
                        raise ValueError('目标不支持扩展属性')
                    original_mode = stat.S_IMODE(p.stat().st_mode)
                    try:
                        if body.get('restore_xattrs'):
                            p.chmod(original_mode | stat.S_IWUSR)
                        for name, encoded in item['xattrs'].items():
                            expected = base64.b64decode(encoded, validate=True)
                            try:
                                actual = get_attribute(p, name)
                            except OSError:
                                actual = None
                            if actual != expected and body.get('restore_xattrs'):
                                set_attribute(p, name, expected)
                            if get_attribute(p, name) != expected:
                                raise ValueError('扩展属性不一致：' + name)
                    finally:
                        if body.get('restore_xattrs'):
                            p.chmod(original_mode)
            except Exception as exc:
                if len(errors) < 100:
                    errors.append({'path': item['path'], 'error': str(exc)})
            done += 1
            if done % 200 == 0:
                atomic_json(root / 'evidence/verification-progress.json', {'files': done, 'bytes': total, 'errors': len(errors), 'elapsed': time.time() - started})
    report = {'status': 'failed' if errors else 'passed', 'entries': done, 'bytes': total,
              'errors': errors, 'checked_at': time.time(), 'scope': 'file_bytes_paths_symlinks_modes_xattrs',
              'manifest_sha256': digest(manifest)}
    report['incoming_extras_preserved'] = extras
    atomic_json(root / 'evidence/verification.json', report)
    return report


def prepare_activation(body):
    import atlas_codex_profile as profile
    import atlas_codex_attachments as attachments
    root = checked_root(body['root'])
    validate_staging_root(root, Path.home(), body.get('roots', []), resolve_paths=True)
    receipt = json.loads((root / 'evidence/verification.json').read_text())
    if receipt['status'] != 'passed' or receipt.get('manifest_sha256') != digest(root / 'evidence/source-manifest.jsonl'):
        raise ValueError('内容尚未校验通过，禁止准备接续环境')
    marker = json.loads((root / 'ATLAS-MIGRATION.json').read_text())
    if marker['id'] != body['id']:
        raise ValueError('迁移任务标识不匹配')
    source_home, target_home = marker['source_home'], str(Path.home())
    recovered = attachments.recover(root, source_home, target_home, body.get('attachments_sha256'))
    atomic_json(root / 'evidence/attachment-recovery.json', recovered)
    src = root / 'incoming/.codex'
    target = Path.home() / '.codex'
    working = root / 'prepared/.codex'
    if working.exists():
        if not body.get('refresh') or (root / 'destination-original-codex').exists():
            raise ValueError('已有准备环境或已切换环境，拒绝覆盖')
        previous = root / 'preparation-history' / str(time.time_ns())
        previous.mkdir(parents=True, mode=0o700)
        os.rename(root / 'prepared', previous / 'prepared')
        if (root / 'destination-before-activation').exists():
            os.rename(root / 'destination-before-activation', previous / 'destination-before-activation')
    # Clone the source evidence. The original destination remains live and intact.
    profile.clone(src, working)
    def prepared_destination(relative):
        dest = working / relative
        if not dest.parent.resolve().is_relative_to(working.resolve()):
            raise ValueError('目标配置合并穿过来源软链接，需单独映射：' + str(relative))
        return dest
    inactive = root / 'prepared/inactive-source-state'
    inactive.mkdir(parents=True, exist_ok=True)
    for relative, item in recovered['files'].items():
        dest = prepared_destination(Path(relative).relative_to('.codex'))
        if not dest.exists():
            profile.clone(item['source'], dest)
        if digest(dest) != item['sha256']:
            raise ValueError('恢复附件的工作副本校验失败')
    for name in ['automations', 'sqlite']:
        if (working / name).exists():
            os.rename(working / name, inactive / name)
    preserve = root / 'destination-before-activation'
    preserve.mkdir(parents=True, exist_ok=True, mode=0o700)
    conflicts, merged = [], {}
    # Overlay destination-owned settings, login, and all unique target files.
    # Source versions of conflicting files remain in incoming/.codex.
    database_names = {p.name for p in target.glob('*.sqlite')} | {p.name for p in src.glob('*.sqlite')}
    runtime = {'ipc', 'tmp', '.tmp', 'app-server-control', 'thread-writer-locks',
               'project-metadata-locks', 'process_manager', 'shell_snapshots'}
    for base, dirs, names in os.walk(target, followlinks=False):
        base = Path(base)
        dirs[:] = [d for d in dirs if not (base == target and d in runtime)]
        for name in list(dirs):
            path = base / name
            if name.endswith('.app') and not path.is_symlink():
                relative = path.relative_to(target)
                dest = prepared_destination(relative)
                if dest.exists() or dest.is_symlink():
                    saved_bundle = inactive / 'source-bundles' / relative
                    saved_bundle.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(dest, saved_bundle)
                profile.clone(path, dest)
                dirs.remove(name)
            elif path.is_symlink():
                names.append(name)
                dirs.remove(name)
        for name in names:
            path = base / name
            if path.is_symlink():
                rel = path.relative_to(target)
                dest = prepared_destination(rel)
                if dest.exists() or dest.is_symlink():
                    saved = inactive / 'source-link-conflicts' / rel
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(dest, saved)
                profile.clone(path, dest)
                continue
            if not path.is_file():
                continue
            rel = path.relative_to(target)
            if rel.parts[0] == 'sqlite' and path.suffix == '.db':
                dest = prepared_destination(rel)
                dest.unlink(missing_ok=True)
                profile.backup(path, dest)
                continue
            if rel.parts[0] == 'sqlite' and name.endswith(('-wal', '-shm', '-journal')):
                continue
            if len(rel.parts) == 1 and (name in database_names or name.endswith(('-wal', '-shm', '-journal', '.lock'))):
                continue
            dest = prepared_destination(rel)
            if dest.exists() and digest(path) != digest(dest):
                conflicts.append({'path': str(rel), 'resolution': 'target_retained_source_in_evidence'})
            if dest.exists():
                dest.unlink()
            profile.clone(path, dest)
    for name in sorted(database_names):
        source_db, target_db, output = src / name, target / name, working / name
        output.unlink(missing_ok=True)
        if target_db.exists():
            profile.backup(target_db, preserve / name)
        if name in profile.DATABASE_TABLES and source_db.exists():
            merged[name] = profile.merge_database(source_db, preserve / name, output,
                profile.DATABASE_TABLES[name], source_home, target_home, recovered['aliases'])
        elif target_db.exists():
            profile.clone(preserve / name, output)
        # Source transient queue, goals and logs stay archived, never replayed.
    merged_config, config_conflicts = profile.merge_config(profile.load_toml(src / 'config.toml'),
        profile.load_toml(target / 'config.toml'), source_home, target_home)
    runtime_bins = body.get('tool_runtime', {}).get('bin_dirs', [])
    if runtime_bins:
        directories = []
        for value in runtime_bins:
            relative = Path(value)
            if relative.is_absolute() or '..' in relative.parts or not any(relative.is_relative_to(Path(r['relative'])) for r in body['roots']):
                raise ValueError('工具 PATH 不属于迁移目录')
            directories.append(str(Path(target_home) / relative))
        variables = merged_config.setdefault('shell_environment_policy', {}).setdefault('set', {})
        prior_path = variables.get('PATH') or os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')
        variables['PATH'] = ':'.join(dict.fromkeys(prior_path.split(':') + directories))
    if (working / 'config.toml').is_symlink():
        (working / 'config.toml').unlink()  # Prepared link only; incoming original remains.
    (working / 'config.toml').write_text(profile.toml_text(merged_config))
    (working / 'config.toml').chmod(0o600)
    agents_file = working / 'AGENTS.md'
    agents_text = agents_file.read_text() if agents_file.exists() else ''
    if agents_file.is_symlink():
        agents_file.unlink()  # Write the migration note to the copy, never its external referent.
    note_marker = '<!-- atlas-migration:' + body['id'] + ' -->'
    if note_marker not in agents_text:
        agents_file.write_text(agents_text.rstrip() + '\n\n' + note_marker + '\n'
            '本机已接收一次 Codex 迁移。原设备主目录前缀 `' + source_home + '` 对应本机 `' + target_home + '`。'
            '历史对话和文档原文可能保留旧路径，使用时按此前缀映射并检查文件实际存在。'
            '原始快照与迁移报告保存在 `' + str(root) + '`；原始历史内容没有全局替换。\n'
            '来源持久化记忆保留在 `' + target_home + '/.codex/memories`，目标已有新版记忆另行保留。'
            '接续旧工程时，按需查阅来源记忆、摘要和工程文件，并应用上述路径映射。\n')
    global_name = '.codex-global-state.json'
    source_global = json.loads((src / global_name).read_text()) if (src / global_name).exists() else {}
    target_global = json.loads((target / global_name).read_text()) if (target / global_name).exists() else {}
    atomic_json(working / global_name, profile.merge_global_state(source_global, target_global, source_home, target_home))
    profile.clone(working / global_name, working / (global_name + '.atlas-merged'))
    repairs = profile.repair_references([working], source_home, target_home)
    prepared_roots, retained_runtime = [], []
    for row in body.get('roots', []):
        relative = Path(row['relative'])
        if relative.is_absolute() or '..' in relative.parts or relative == Path('.'):
            raise ValueError('准备目录越界')
        if relative == Path('.codex'):
            continue
        destination = Path.home() / relative
        if relative.parts[:2] == ('.cache', 'codex-runtimes') and destination.is_dir():
            retained_runtime.append(str(destination))
            continue
        if destination.exists() or destination.is_symlink():
            raise ValueError('目标已有同名目录，原文件保持不变：' + str(destination))
        staged = root / 'prepared/home' / relative
        profile.clone(root / 'incoming' / relative, staged)
        prepared_roots.append(staged)
    project_repairs = profile.repair_references(prepared_roots, source_home, target_home)
    dependencies = []
    with profile.connection(working / 'state_5.sqlite') as c:
        for row in c.execute('SELECT DISTINCT cwd FROM threads'):
            path = Path(row[0])
            rel = path.relative_to(target_home) if path.is_relative_to(target_home) else None
            if not path.exists() and (rel is None or not (root / 'incoming' / rel).exists()):
                dependencies.append({'path': str(path), 'kind': 'missing_cwd'})
    report = {'status': 'prepared', 'merged': merged, 'settings_conflicts': conflicts,
              'attachments': {k: v for k, v in recovered.items() if k not in {'files', 'aliases'}},
              'config_target_preferred_keys': config_conflicts,
              'source_automations': 'archived_inactive_to_avoid_duplicate_execution',
              'references': repairs, 'dependencies': dependencies,
              'project_references': project_repairs, 'retained_destination_runtime': retained_runtime,
              'prepared_roots': [str(p.relative_to(root / 'prepared/home')) for p in prepared_roots],
              'manifest_sha256': receipt['manifest_sha256'],
              'prepared_at': time.time(), 'original_target_untouched': True,
              'note': '准备副本尚未激活；目标退出后需重新核对数据库是否变化'}
    atomic_json(root / 'evidence/activation.json', report)
    return report


def activate(body):
    """Switch only after preparation, target quiescence and destination recheck."""
    import atlas_codex_profile as profile
    root = checked_root(body['root'])
    report = json.loads((root / 'evidence/activation.json').read_text())
    if report['status'] != 'prepared':
        raise ValueError('尚未准备可接续环境')
    if report.get('manifest_sha256') != digest(root / 'evidence/source-manifest.jsonl'):
        raise ValueError('来源快照已更新，请重新准备接续环境')
    if not body.get('allow_app_restart'):
        raise ValueError('接续需要关闭并重启目标 Codex；请先核对接续方案')
    home = Path.home()
    current = home / '.codex'
    # Do not terminate an in-progress target conversation.
    history = current / 'thread_history_1.sqlite'
    if history.exists():
        with profile.connection(history) as c:
            active = c.execute("SELECT count(*) FROM thread_turns WHERE status NOT IN ('completed','interrupted','failed') AND completed_at IS NULL").fetchone()[0]
            if active:
                raise ValueError('目标仍有活动会话，请等待完成后再接续')
    if sys.platform != 'darwin':
        raise ValueError('本版自动桌面接续仅支持 macOS；Linux 可使用准备后的 CODEX_HOME，须单独完成原生验收')
    # Keep all target originals and restore already-renamed directories on error.
    processes = run(['ps', '-axo', 'comm='])
    reopened, installed = [], []
    original = root / 'destination-original-codex'
    if original.exists():
        raise ValueError('已有回滚目录，停止重复接续')
    try:
        for name in ['ChatGPT', 'Codex']:
            app = '/Applications/' + name + '.app/Contents/MacOS/'
            if app in processes:
                result = subprocess.run(['osascript', '-e', 'tell application "' + name + '" to quit'], capture_output=True, timeout=30)
                if result.returncode:
                    raise ValueError('目标应用未正常退出，保持原有环境')
                reopened.append(name)
        # Current desktop builds may retain their daemon after the GUI exits.
        # Use its own orderly stop command; never kill arbitrary codex processes.
        for name in reopened:
            cli = Path('/Applications') / (name + '.app/Contents/Resources/codex-cli/bin/codex')
            if cli.is_file():
                subprocess.run([str(cli), 'app-server', 'daemon', 'stop'], capture_output=True, timeout=30)
        for _ in range(30):
            processes = run(['ps', '-axo', 'comm='])
            if not any('/Applications/' + name + '.app/' in line and ('/codex' in line or '/MacOS/' + name == line[-len('/MacOS/' + name):])
                       for line in processes.splitlines() for name in ['ChatGPT', 'Codex']):
                break
            time.sleep(1)
        else:
            raise ValueError('Codex 写入进程尚未退出，未切换数据库')
        preserve = root / 'destination-before-activation'
        for name in profile.DATABASE_TABLES:
            dbpath = current / name
            before = preserve / dbpath.name
            if dbpath.exists() and before.exists():
                tables = profile.DATABASE_TABLES[name]
                if profile.persistent_signature(dbpath, tables) != profile.persistent_signature(before, tables):
                    raise ValueError('目标持久化内容在准备后发生变化，需刷新准备副本：' + dbpath.name)
        source_home = json.loads((root / 'ATLAS-MIGRATION.json').read_text())['source_home']
        mappings = body['roots']
        # Check every directory before creating any, preserving pre-existing projects.
        for row in mappings:
            relative = Path(row['relative'])
            if relative.is_absolute() or '..' in relative.parts or relative == Path('.'):
                raise ValueError('接续目录越界')
            if relative == Path('.codex'):
                continue
            dest = home / relative
            if relative.parts[:2] == ('.cache', 'codex-runtimes') and dest.is_dir():
                continue  # Keep the destination app's runtime; source remains archived.
            if dest.exists() or dest.is_symlink():
                raise ValueError('目标已有同名目录，拒绝覆盖：' + str(dest))
            if not dest.parent.resolve().is_relative_to(home.resolve()):
                raise ValueError('接续目录父级链接越界')
        report.update(status='activating', installed_paths=[], restarted_apps=reopened)
        atomic_json(root / 'evidence/activation.json', report)
        for row in mappings:
            relative = Path(row['relative'])
            if relative == Path('.codex'):
                continue
            dest = home / relative
            if relative.parts[:2] == ('.cache', 'codex-runtimes') and dest.is_dir():
                report.setdefault('retained_destination_runtime', []).append(str(dest))
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.rename(root / 'prepared/home' / relative, dest)
            installed.append(dest)
            report['installed_paths'].append(str(dest))
            atomic_json(root / 'evidence/activation.json', report)
        # Reconcile device/UI state after the app has quit normally.
        global_name = '.codex-global-state.json'
        original_global = current / global_name
        source_global = root / 'incoming/.codex' / global_name
        if original_global.exists() and source_global.exists():
            merged_global = profile.merge_global_state(json.loads(source_global.read_text()),
                json.loads(original_global.read_text()), source_home, str(home))
            atomic_json(root / 'prepared/.codex' / global_name, merged_global)
            atomic_json(root / 'prepared/.codex' / (global_name + '.bak'), merged_global)
        if original.exists():
            raise ValueError('已有回滚目录，停止重复接续')
        os.rename(current, original)
        try:
            os.rename(root / 'prepared/.codex', current)
        except BaseException:
            os.rename(original, current)
            raise
        report.update(status='activated_pending_acceptance', activated_at=time.time(), rollback=str(original))
        atomic_json(root / 'evidence/activation.json', report)
        return report
    except BaseException as exc:
        rollback_errors = []
        try:
            if original.exists():
                if current.exists():
                    os.rename(current, root / 'prepared/.codex')
                os.rename(original, current)
            for destination in reversed(installed):
                staged = root / 'prepared/home' / destination.relative_to(home)
                staged.parent.mkdir(parents=True, exist_ok=True)
                os.rename(destination, staged)
        except BaseException as rollback_error:
            rollback_errors.append(str(rollback_error))
        report.update(status='recovery_required' if rollback_errors else 'prepared',
                      last_error=str(exc), rollback_errors=rollback_errors)
        atomic_json(root / 'evidence/activation.json', report)
        raise
    finally:
        for name in reopened:
            subprocess.run(['open', '-a', name], capture_output=True, timeout=20)


def accept_native(body):
    import atlas_codex_profile as profile
    root = checked_root(body['root'])
    activation = json.loads((root / 'evidence/activation.json').read_text())
    if activation['status'] not in {'activated_pending_acceptance', 'accepted'}:
        raise ValueError('目标尚未接入迁移环境')
    def ids(path):
        if not path.exists():
            return []
        with profile.connection(path) as c:
            return [r[0] for r in c.execute('SELECT id FROM threads')]
    result = profile.native_acceptance(Path.home() / '.codex', root / 'evidence',
        ids(root / 'incoming/.codex/state_5.sqlite'), ids(root / 'destination-original-codex/state_5.sqlite'))
    recovered = root / 'evidence/attachment-recovery.json'
    if recovered.exists():
        recovery = json.loads(recovered.read_text())
        errors = []
        for relative, item in recovery['files'].items():
            path = Path.home() / relative
            if not path.is_file() or digest(path) != item['sha256']:
                errors.append(relative)
        result['attachments'] = {'checked_files': len(recovery['files']), 'errors': errors,
            'unresolved_source_user_images': recovery['unresolved_user_images'],
            'recovered_paths': recovery['recovered_paths']}
        if errors:
            result['status'] = 'failed'
    atomic_json(root / 'evidence/native-acceptance.json', result)
    if result['status'] == 'passed':
        activation['status'] = 'accepted'
        activation['native_acceptance'] = result
        atomic_json(root / 'evidence/activation.json', activation)
    return result


def deploy_atlas(body):
    import atlas_codex_profile as profile
    import urllib.request
    root = checked_root(body['root'])
    activation = json.loads((root / 'evidence/activation.json').read_text())
    if activation['status'] not in {'activated_pending_acceptance', 'accepted'}:
        raise ValueError('请先接入已校验的工程与 Codex 环境')
    home = Path.home()
    atlas = body['atlas']
    def under_home(value):
        relative = Path(value)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Atlas 部署路径越界')
        path = home / relative
        if not path.resolve().is_relative_to(home.resolve()):
            raise ValueError('Atlas 部署路径穿过外部软链接')
        return path
    source_app = under_home(atlas['application'])
    catalog, archive = under_home(atlas['catalog']), under_home(atlas['archive'])
    vault = catalog.parent
    if archive.parent != vault:
        raise ValueError('自动部署要求 catalog 与 archive 共用根目录；可改为手动服务配置')
    app = vault / 'app'
    if app.exists():
        raise ValueError('目标已有 Atlas 程序目录，请使用标准升级流程；不会覆盖既有部署')
    runtime_source = home / '.cache/codex-runtimes/codex-primary-runtime/dependencies/python'
    if not (runtime_source / 'bin/python3.12').is_file():
        raise ValueError('目标缺少 Python 3.12 运行时')
    runtime = vault / 'tools' / ('atlas-migration-' + body['id'][:12]) / 'python'
    profile.clone(runtime_source, runtime)
    python = runtime / 'bin/python3.12'
    def execute(args, timeout=120):
        result = subprocess.run([str(x) for x in args], capture_output=True, timeout=timeout)
        if result.returncode:
            raise ValueError('Atlas 部署命令失败：' + result.stderr.decode(errors='replace')[-1200:])
        return result.stdout.decode(errors='replace')
    execute([python, '-c', 'import sys,sqlite3;assert sys.version_info[:2]==(3,12)'])
    release = vault / 'backups' / ('atlas-migration-' + body['id'][:12] + '.zip')
    if body.get('release_sha256'):
        supplied = root / 'evidence/atlas-release.zip'
        if digest(supplied) != body['release_sha256']:
            raise ValueError('Atlas 发布包 SHA-256 不一致')
        profile.clone(supplied, release)
    else:
        execute([python, source_app / 'scripts/package-release.py', release], timeout=300)
    execute([python, source_app / 'scripts/install-release.py', release, '--root', vault], timeout=300)
    execute([python, '-m', 'venv', '--without-pip', app / '.venv'])
    source_env = under_home(atlas['python_environment'])
    source_packages = source_env / 'lib/python3.12/site-packages'
    target_packages = app / '.venv/lib/python3.12/site-packages'
    if not source_packages.is_dir():
        raise ValueError('缺少已迁移的 Atlas 离线依赖，需要按 requirements.lock 安装')
    target_packages.rmdir()  # Empty directory created above; never remove prior data.
    profile.clone(source_packages, target_packages)
    app_python = app / '.venv/bin/python'
    execute([app_python, '-c', 'import fastapi,uvicorn,multipart,sqlite3'])
    source_home = json.loads((root / 'ATLAS-MIGRATION.json').read_text())['source_home']
    rebased = profile.rebase_atlas(catalog, source_home, str(home), body.get('plan', {}).get('roots', []))
    port = int(body.get('port', 8765))
    if not 1024 <= port <= 65535:
        raise ValueError('部署端口无效')
    # Refuse a live port before installing a managed service.
    import socket
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', port)) == 0:
            raise ValueError('目标端口已被使用；程序已准备，服务尚未安装')
    execute([app_python, app / 'scripts/install-service.py', '--app', app, '--root', vault,
             '--name', 'atlas-migrated', '--port', str(port)], timeout=60)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    health = None
    for _ in range(30):
        try:
            health = json.load(opener.open('http://127.0.0.1:%s/api/health' % port, timeout=2))
            break
        except Exception:
            time.sleep(1)
    if not health:
        raise ValueError('服务尚未通过健康检查，请查看目标 catalog/server-error.log')
    report = {'status': 'running', 'app': str(app), 'catalog': str(catalog), 'archive': str(archive),
              'python': str(app_python), 'runtime_independent_of_codex_cache': True,
              'local_url_on_target': 'http://127.0.0.1:%s' % port, 'health': health,
              'node_rebinding': rebased, 'release_sha256': digest(release), 'installed_at': time.time()}
    atomic_json(root / 'evidence/atlas-deployment.json', report)
    if body.get('plan'):
        received = dict(body['plan'])
        received.update(role='receiver', status='received', phase='received', atlas_deployment=report,
            detail='此节点已接收迁移内容；原设备上的迁移任务继续保存完整执行记录',
            updated_at=time.time(), worker_pid=None)
        atomic_json(catalog / 'codex-migrations' / body['id'] / 'plan.json', received)
    return report


def main():
    body = json.load(sys.stdin)
    action = body['action']
    if action == 'inspect':
        result = inspect_home()
    elif action == 'prepare':
        result = prepare(body)
    elif action == 'verify':
        result = verify(body)
    elif action == 'prepare_activation':
        result = prepare_activation(body)
    elif action == 'activate':
        result = activate(body)
    elif action == 'accept_native':
        result = accept_native(body)
    elif action == 'probe_native':
        import atlas_codex_profile as profile
        root = checked_root(body['root'])
        with profile.connection(Path.home() / '.codex/state_5.sqlite') as c:
            existing = [r[0] for r in c.execute('SELECT id FROM threads')]
        result = profile.native_acceptance(Path.home() / '.codex', root / 'evidence', [], existing)
        atomic_json(root / 'evidence/native-probe.json', result)
    elif action == 'attachment_references':
        import atlas_codex_attachments as attachments
        root = checked_root(body['root'])
        result = attachments.references(root / 'incoming/.codex/thread_history_1.sqlite')
    elif action == 'prepare_attachment_inputs':
        root = checked_root(body['root'])
        if json.loads((root / 'ATLAS-MIGRATION.json').read_text())['id'] != body['id']:
            raise ValueError('任务标识不一致')
        (root / 'supplements/attachments').mkdir(parents=True, exist_ok=True, mode=0o700)
        result = {'ready': True}
    elif action == 'deploy_atlas':
        result = deploy_atlas(body)
    elif action == 'install_tools':
        import atlas_codex_profile as profile
        import atlas_migration_tools as runtime_tools
        root = checked_root(body['root'])
        marker = json.loads((root / 'ATLAS-MIGRATION.json').read_text())
        activation = json.loads((root / 'evidence/activation.json').read_text())
        if marker['id'] != body['id'] or activation['status'] not in {'activated_pending_acceptance', 'accepted'}:
            raise ValueError('请先接入目标环境')
        supplement = checked_root(body['supplement_root'])
        result = runtime_tools.install({**body, 'root': str(supplement), 'id': body['supplement_id'],
                                       'roots': body['tool_roots']}, sys.modules[__name__], profile)
        atomic_json(root / 'evidence/tool-verification.json', json.loads((supplement / 'evidence/verification.json').read_text()))
        atomic_json(root / 'evidence/tool-installation.json', result)
    elif action == 'verify_workspaces':
        import atlas_codex_profile as profile
        root = checked_root(body['root'])
        activation = json.loads((root / 'evidence/activation.json').read_text())
        if activation['status'] not in {'activated_pending_acceptance', 'accepted'}:
            raise ValueError('请先接入目标环境')
        source_home = json.loads((root / 'ATLAS-MIGRATION.json').read_text())['source_home']
        result = profile.workspace_acceptance(root, source_home, str(Path.home()))
        atomic_json(root / 'evidence/workspace-acceptance.json', result)
    elif action == 'recover_worktree':
        import atlas_codex_profile as profile
        root = checked_root(body['root'])
        activation = json.loads((root / 'evidence/activation.json').read_text())
        if activation['status'] not in {'activated_pending_acceptance', 'accepted'}:
            raise ValueError('请先接入目标环境')
        result = profile.recover_historical_worktree(Path.home(), body['thread_id'], body['repository'])
        atomic_json(root / 'evidence' / ('recovered-worktree-' + str(uuid.uuid4()) + '.json'), result)
    elif action == 'complete':
        root = checked_root(body['root'])
        checks = {name: json.loads((root / 'evidence' / name).read_text())
                  for name in ['verification.json', 'native-acceptance.json', 'workspace-acceptance.json']}
        if any(r['status'] != 'passed' for r in checks.values()):
            raise ValueError('仍有验收未通过')
        result = {'status': 'complete', 'checks': {k: r['status'] for k, r in checks.items()},
                  'source_deleted': False, 'completed_at': time.time(),
                  'project_builds_executed': False, 'original_snapshots_retained': True}
        atomic_json(root / 'evidence/completion.json', result)
        power = root / 'evidence/power-assertion.json'
        if power.exists():
            record = json.loads(power.read_text())
            if run(['ps', '-p', str(record['pid']), '-o', 'lstart=']).strip() == record.get('started'):
                import signal
                try:
                    os.kill(record['pid'], signal.SIGTERM)
                except ProcessLookupError:
                    pass
    elif action == 'mkdir':
        root = checked_root(body['root'])
        for relative in body['paths']:
            p = Path(relative)
            if p.is_absolute() or '..' in p.parts:
                raise ValueError('路径越界')
            dest = root / 'incoming' / p
            if not dest.resolve().is_relative_to((root / 'incoming').resolve()):
                raise ValueError('路径越界')
            dest.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = {'ok': True}
    elif action == 'report':
        root = checked_root(body['root'])
        result = {}
        for name in ['verification-progress.json', 'verification.json', 'activation.json', 'native-acceptance.json', 'atlas-deployment.json', 'workspace-acceptance.json', 'completion.json', 'attachment-recovery-progress.json', 'attachment-recovery.json', 'tool-verification.json', 'tool-installation.json']:
            p = root / 'evidence' / name
            if p.is_file():
                result[name] = json.loads(p.read_text())
    else:
        raise ValueError('未知迁移动作')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
