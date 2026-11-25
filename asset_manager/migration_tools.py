"""Install a verified runtime supplement, preserving target files and settings."""
import json
import os
import shlex
import subprocess
import time
from pathlib import Path


def install(body, agent, profile):
    root = agent.checked_root(body['root'])
    marker = json.loads((root / 'ATLAS-MIGRATION.json').read_text())
    receipt = json.loads((root / 'evidence/verification.json').read_text())
    if (marker['id'] != body['id'] or receipt['status'] != 'passed'
            or receipt['manifest_sha256'] != agent.digest(root / 'evidence/source-manifest.jsonl')):
        raise ValueError('工具副本尚未通过完整校验')
    home = Path.home()
    if body.get('verify_only'):
        report_path = root / 'evidence/tool-installation.json'
        report = json.loads(report_path.read_text())
        expected = [str(home / p) for p in body['roots']]
        if report.get('installed_paths') != expected or report['status'] not in {'passed', 'needs_attention'}:
            raise ValueError('没有可复验的本任务工具安装记录')
        repair_python_links(root, home, agent, profile, active=True)
        report['python_runtimes'] = verify_python_links(root, home, agent)
        settings = profile.load_toml(home / '.codex/config.toml')
        path = settings.get('shell_environment_policy', {}).get('set', {}).get('PATH', os.environ.get('PATH', ''))
        report['checks'] = check_tools(body['probes'], home, path)
        report['status'] = 'passed' if report['python_runtimes']['status'] == 'passed' and all(c['exit_code'] == 0 for c in report['checks']) else 'needs_attention'
        report['checked_at'] = time.time()
        agent.atomic_json(report_path, report)
        return report
    destinations = []
    for value in body['roots']:
        relative = Path(value)
        destination = home / relative
        if (relative.is_absolute() or '..' in relative.parts or relative == Path('.')
                or not destination.parent.resolve().is_relative_to(home.resolve())):
            raise ValueError('工具目录越界')
        if destination.exists() or destination.is_symlink():
            raise ValueError('目标目录已存在，不覆盖：' + str(destination))
        if not (root / 'incoming' / relative).is_dir():
            raise ValueError('工具来源目录缺失')
        destinations.append((relative, destination))
    node_bin = Path(body['node_bin'])
    if node_bin.is_absolute() or '..' in node_bin.parts or not any(node_bin.is_relative_to(r) for r, _ in destinations):
        raise ValueError('Node 运行路径不属于已校验目录')
    launch = home / Path(body.get('environment_entry', '.config/atlas/migration-env.sh'))
    if not launch.parent.resolve().is_relative_to(home.resolve()):
        raise ValueError('环境入口越界')
    if launch.exists() or launch.is_symlink():
        raise ValueError('环境入口已存在，不覆盖')
    config = home / '.codex/config.toml'
    if not config.is_file() or config.is_symlink():
        raise ValueError('请先完成原生 Codex 配置接续')
    before = root / 'evidence/target-config-before-tools.toml'
    if before.exists():
        raise ValueError('已有工具接入记录，请检查后再续作，不重复覆盖')
    probes = body['probes']
    if not probes:
        raise ValueError('补充运行环境需要明确的版本验收命令')
    for probe in probes:
        relative = Path(probe['relative'])
        if relative.is_absolute() or '..' in relative.parts or not any(relative.is_relative_to(r) for r, _ in destinations):
            raise ValueError('版本验收命令不属于已校验目录')
    report = {'status': 'installing', 'installed_paths': [], 'source_deleted': False, 'started_at': time.time()}
    for relative, destination in destinations:
        profile.clone(root / 'incoming' / relative, destination)
        report['installed_paths'].append(str(destination))
        agent.atomic_json(root / 'evidence/tool-installation.json', report)
    report['references'] = profile.repair_references([d for _, d in destinations], marker['source_home'], str(home))
    profile.clone(config, before)
    settings = profile.load_toml(config)
    variables = settings.setdefault('shell_environment_policy', {}).setdefault('set', {})
    prior_path = variables.get('PATH') or os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')
    entries = [profile.map_path(p, marker['source_home'], str(home)) for p in prior_path.split(':') if p]
    entries += [str(home / '.local/bin'), str(home / node_bin)]
    runtime_path = ':'.join(dict.fromkeys(entries))
    variables['PATH'] = runtime_path
    temp = config.with_name('config.atlas-tools.tmp')
    temp.write_text(profile.toml_text(settings)); temp.chmod(0o600)
    os.replace(temp, config)
    repair_python_links(root, home, agent, profile, active=True)
    runtime_checks = verify_python_links(root, home, agent)
    checks = check_tools(probes, home, runtime_path)
    launch.parent.mkdir(parents=True, exist_ok=True)
    launch.write_text('# Source this file to use migrated tools in a terminal.\nexport PATH=' + shlex.quote(runtime_path) + '\n')
    launch.chmod(0o600)
    report.update(status='passed' if runtime_checks['status'] == 'passed' and all(c['exit_code'] == 0 for c in checks) else 'needs_attention', checks=checks,
                  python_runtimes=runtime_checks, codex_environment_updated=True, environment_entry=str(launch), completed_at=time.time())
    agent.atomic_json(root / 'evidence/tool-installation.json', report)
    return report


def check_tools(probes, home, runtime_path):
    checks = []
    for probe in probes:
        args = [str(home / probe['relative']), *probe['args']]
        try:
            result = subprocess.run(args, env={**os.environ, 'PATH': runtime_path}, capture_output=True, timeout=45)
            checks.append({'tool': probe['name'], 'exit_code': result.returncode,
                           'output': (result.stdout + result.stderr).decode(errors='replace')[-1000:]})
        except Exception as exc:
            checks.append({'tool': probe['name'], 'exit_code': None, 'error': str(exc)})
    return checks


def python_identity(executable):
    program = ('import json,sys,sysconfig,platform,ssl,sqlite3;print(json.dumps({'
               '"version":list(sys.version_info[:3]),"executable":sys.executable,'
               '"base_prefix":sys.base_prefix,"prefix":sys.prefix,"architecture":platform.machine(),'
               '"soabi":sysconfig.get_config_var("SOABI"),"free_threaded":bool(sysconfig.get_config_var("Py_GIL_DISABLED"))}))')
    result = subprocess.run([str(executable), '-I', '-c', program], capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors='replace')[-1000:])
    return json.loads(result.stdout)


def repair_python_links(root, home, agent, profile, active=False):
    """Rebind missing base interpreters in prepared venvs to exact-version runtimes."""
    import re
    import sys
    import platform
    root, home = Path(root), Path(home)
    prepared = root / 'prepared'
    report_path = root / 'evidence/python-runtime-rebinding.json'
    manifest_sha = agent.digest(root / 'evidence/source-manifest.jsonl')
    previous = json.loads(report_path.read_text()) if report_path.exists() else {}
    prior = {r['path']: r for r in previous.get('changes', [])} if previous.get('manifest_sha256') == manifest_sha else {}
    def staged(relative):
        relative = Path(relative)
        if active:
            return home / relative
        return prepared / relative if relative.parts[0] == '.codex' else prepared / 'home' / relative
    def installed(path):
        path = Path(path)
        for base in [prepared / 'home', prepared]:
            if path.is_relative_to(base):
                return str(home / path.relative_to(base))
        return str(path)
    candidates, links = [Path(sys.executable)], []
    cache = home / '.cache/codex-runtimes'
    for executable in (cache / 'codex-primary-runtime/dependencies/python/bin').glob('python3.*'):
        if re.fullmatch(r'python3\.\d+', executable.name) and executable.is_file():
            candidates.append(executable)
    with (root / 'evidence/source-manifest.jsonl').open() as manifest:
        for line in manifest:
            row = json.loads(line)
            relative = Path(row['path'])
            if not re.fullmatch(r'python3?(?:\.\d+)?', relative.name) or relative.parent.name != 'bin':
                continue
            path = staged(relative)
            if row['type'] == 'symlink' and path.is_symlink():
                target = os.readlink(path)
                if relative.as_posix() in prior:
                    links.append((relative, path, prior[relative.as_posix()]['before']))
                elif os.path.isabs(target) and not Path(target).exists():
                    if Path(target).is_relative_to(home) and staged(Path(target).relative_to(home)).is_file():
                        continue
                    if (path.parent.parent / 'pyvenv.cfg').is_file():
                        links.append((relative, path, target))
            elif row['type'] == 'file' and path.is_file() and '.temp' not in relative.parts:
                if '.runtime' in relative.parts or any(p.startswith('.atlas-python-') for p in relative.parts) or relative.is_relative_to('.local/share/uv/python'):
                    with path.open('rb') as source:
                        if source.read(4) in {b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\x7fELF'}:
                            candidates.append(path)
    for _, path, _ in links:
        for pattern in ['.atlas-python-*/bin/python3.*', '.atlas-python-*/cpython-*/bin/python3.*']:
            candidates.extend(p for p in path.parent.parent.glob(pattern)
                              if re.fullmatch(r'python3\.\d+', p.name) and p.is_file())
    runtimes, failures = [], []
    for executable in dict.fromkeys(candidates):
        try:
            identity = python_identity(executable)
            if identity['architecture'] != platform.machine() or identity['free_threaded']:
                continue
            runtimes.append({**identity, 'executable': installed(executable),
                             'base_prefix': installed(identity['base_prefix']),
                             'inspection_executable': str(executable), 'inspection_base_prefix': identity['base_prefix']})
        except Exception as exc:
            failures.append({'path': str(executable), 'error': str(exc)})
    changes, unresolved = [], []
    for relative, path, before in links:
        config = path.parent.parent / 'pyvenv.cfg'
        text = config.read_text()
        fields = {k.strip(): v.strip() for line in text.splitlines() if '=' in line for k, v in [line.split('=', 1)]}
        match = re.match(r'(\d+)\.(\d+)\.(\d+)', fields.get('version') or fields.get('version_info') or '')
        expected = list(map(int, match.groups())) if match else None
        selected = next((r for r in runtimes if r['version'] == expected), None)
        if selected is None and expected:
            uv = next((p for p in [home / '.local/bin/uv', staged('.local/bin/uv')] if p.is_file()), None)
            if uv:
                import hashlib
                version = '.'.join(map(str, expected))
                base = path.parent.parent / ('.atlas-python-' + version)
                log = root / 'evidence' / ('python-install-' + hashlib.sha256(str(relative).encode()).hexdigest()[:12] + '.log')
                try:
                    with log.open('ab') as output:
                        log.chmod(0o600)
                        result = subprocess.run([str(uv), 'python', 'install', version, '--install-dir', str(base),
                            '--no-bin', '--no-config', '--no-progress'], stdout=output, stderr=subprocess.STDOUT, timeout=300)
                    if result.returncode:
                        raise ValueError('uv 安装失败，详见 ' + str(log))
                    for executable in base.glob('cpython-*/bin/python3.*'):
                        if not re.fullmatch(r'python3\.\d+', executable.name):
                            continue
                        identity = python_identity(executable)
                        if identity['version'] == expected and identity['architecture'] == platform.machine() and not identity['free_threaded']:
                            selected = {**identity, 'executable': installed(executable), 'base_prefix': installed(identity['base_prefix']),
                                'inspection_executable': str(executable), 'inspection_base_prefix': identity['base_prefix'],
                                'installed_by_uv': True, 'installation_log': str(log)}
                            runtimes.append(selected)
                            break
                except Exception as exc:
                    failures.append({'path': str(base), 'error': str(exc)})
        if selected is None:
            unresolved.append({'path': str(relative), 'target': before, 'required_version': expected})
            continue
        after = selected['executable']
        base_prefix = selected['base_prefix']
        if Path(selected['inspection_executable']).is_relative_to(cache):
            # Codex may refresh its cache. Keep an independent, exact-version
            # base inside this migrated environment instead of linking to it.
            base = path.parent.parent / ('.atlas-python-' + '.'.join(map(str, expected)))
            executable_relative = Path(selected['inspection_executable']).relative_to(selected['inspection_base_prefix'])
            if not base.exists():
                profile.clone(selected['inspection_base_prefix'], base)
            identity = python_identity(base / executable_relative)
            if identity['version'] != expected or identity['soabi'] != selected['soabi']:
                raise ValueError('独立 Python 基础运行时与所需版本不一致')
            after, base_prefix = installed(base / executable_relative), installed(base)
        replacements = {'home': str(Path(after).parent), 'executable': after, 'base-executable': after,
                        'base-prefix': base_prefix, 'base-exec-prefix': base_prefix}
        lines = []
        for line in text.splitlines(keepends=True):
            key, separator, value = line.partition('=')
            if separator and key.strip() in replacements:
                value = value.replace(value.strip(), replacements[key.strip()], 1)
            lines.append(key + separator + value)
        changes.append({'path': relative.as_posix(), 'before': before, 'after': after,
                        'version': expected, 'soabi': selected['soabi'],
                        'environment': relative.parent.parent.as_posix()})
        agent.atomic_json(report_path, {'status': 'preparing', 'changes': changes,
            'unresolved': unresolved, 'manifest_sha256': manifest_sha})
        profile.write_copy(config, ''.join(lines))
        if os.readlink(path) != after:
            with profile.writable_copy(path.parent):
                path.unlink(); path.symlink_to(after)
    report = {'status': 'prepared' if not unresolved else 'needs_attention', 'changes': changes,
              'unresolved': unresolved, 'available_runtimes': runtimes, 'candidate_errors': failures,
              'scope': 'missing_python_interpreter_links_in_migrated_virtual_environments', 'working_copy': 'active' if active else 'prepared',
              'original_snapshots_unchanged': True, 'prepared_at': time.time()}
    report['manifest_sha256'] = manifest_sha
    agent.atomic_json(root / 'evidence/python-runtime-rebinding.json', report)
    return report


def verify_python_links(root, home, agent):
    root, home = Path(root), Path(home)
    prepared = json.loads((root / 'evidence/python-runtime-rebinding.json').read_text())
    checks, errors = [], list(prepared['unresolved'])
    if prepared['status'] != 'prepared':
        errors.append({'error': 'Python 环境准备尚未完成'})
    for row in prepared['changes']:
        path = home / row['path']
        try:
            identity = python_identity(path)
            if identity['version'] != row['version'] or Path(identity['prefix']) != home / row['environment']:
                raise ValueError('解释器版本或虚拟环境前缀不匹配')
            checks.append({'path': str(path), **identity})
        except Exception as exc:
            errors.append({'path': str(path), 'error': str(exc)})
    result = {'status': 'passed' if not errors else 'failed', 'checks': checks, 'errors': errors,
              'scope': 'python_startup_version_prefix_ssl_sqlite', 'checked_at': time.time()}
    agent.atomic_json(root / 'evidence/python-runtime-acceptance.json', result)
    return result
