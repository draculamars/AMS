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
    checks = []
    for probe in probes:
        args = [str(home / probe['relative']), *probe['args']]
        try:
            result = subprocess.run(args, env={**os.environ, 'PATH': runtime_path}, capture_output=True, timeout=45)
            checks.append({'tool': probe['name'], 'exit_code': result.returncode,
                           'output': (result.stdout + result.stderr).decode(errors='replace')[-1000:]})
        except Exception as exc:
            checks.append({'tool': probe['name'], 'exit_code': None, 'error': str(exc)})
    launch.parent.mkdir(parents=True, exist_ok=True)
    launch.write_text('# Source this file to use migrated tools in a terminal.\nexport PATH=' + shlex.quote(runtime_path) + '\n')
    launch.chmod(0o600)
    report.update(status='passed' if all(c['exit_code'] == 0 for c in checks) else 'needs_attention', checks=checks,
                  codex_environment_updated=True, environment_entry=str(launch), completed_at=time.time())
    agent.atomic_json(root / 'evidence/tool-installation.json', report)
    return report
