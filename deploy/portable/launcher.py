"""Offline launcher; keeps a process lock until the database closes cleanly."""
import fcntl
import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import webbrowser
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def contained(root, relative):
    p = PurePosixPath(relative)
    if p.is_absolute() or '..' in p.parts or not p.parts or '\\' in relative:
        raise ValueError('Invalid portable relative path')
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError('Portable path escapes its root')
    return target


def application(root):
    package = root / 'app' / 'atlas.zip'
    expected = (root / 'app' / 'atlas.sha256').read_text().strip()
    if digest(package) != expected:
        raise ValueError('Application archive checksum mismatch')
    cache = Path(os.environ['ATLAS_RUNTIME_DIR']) / 'apps' / expected
    if (cache / '.complete').exists():
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache.parent) as tmp:
        folder = Path(tmp) / 'app'
        folder.mkdir()
        with zipfile.ZipFile(package) as archive:
            manifest = json.loads(archive.read('RELEASE-MANIFEST.json'))
            for name, sha in manifest['files'].items():
                path = contained(folder, name)
                content = archive.read(name)
                if hashlib.sha256(content).hexdigest() != sha:
                    raise ValueError('Application member checksum mismatch: ' + name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            (folder / 'RELEASE-MANIFEST.json').write_text(json.dumps(manifest))
        (folder / '.complete').touch()
        os.replace(folder, cache)
    return cache


def health(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:' + str(int(port)) + '/api/health', timeout=2) as r:
        return json.load(r)


def host_key():
    return hashlib.sha256((socket.gethostname() + ':' + str(os.getuid()) + ':' + platform.system()).encode()).hexdigest()


def stop(root):
    path = root / 'session.json'
    if not path.is_file():
        print('Atlas is already stopped.')
        return
    state = read_json(path)
    if state.get('host') != host_key():
        raise ValueError('Session belongs to another host. No process was signalled.')
    try:
        remote = health(state['port'])
    except OSError:
        print('No matching Atlas service is listening. No process was signalled.')
        return
    if remote.get('portable_session') != state['nonce'] or remote.get('portable_pid')!=state.get('pid') or not isinstance(state.get('pid'),int) or state['pid']<=1:
        raise ValueError('Port now belongs to a different service. No process was signalled.')
    catalog = contained(root, state['dataset']) / 'catalog' / 'catalog.sqlite3'
    with closing(sqlite3.connect(catalog.as_uri() + '?mode=ro', uri=True)) as c:
        if c.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone():
            raise ValueError('Wait for active archive/preview jobs to finish before stopping')
    os.kill(state['pid'], signal.SIGTERM)
    for _ in range(120):
        if not path.exists():
            print('Atlas stopped; catalog saved. You may now eject the disk using the operating system.')
            return
        time.sleep(0.25)
    raise ValueError('Service is still closing. Keep the disk attached and inspect its terminal.')


def start(root, no_browser=False):
    config = read_json(root / 'portable.json')
    if config.get('format') != 1:
        raise ValueError('Unsupported portable format')
    catalog = contained(root, config['active_dataset']) / 'catalog'
    database = catalog / 'catalog.sqlite3'
    if not database.is_file():
        raise ValueError('Portable catalog is missing')
    # The kernel releases this lock on process exit, including a crash.
    lock = (root / '.session.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        state = read_json(root / 'session.json')
        if state.get('host') == host_key() and health(state['port']).get('portable_session') == state['nonce']:
            url = f"http://127.0.0.1:{state['port']}"
            print('Atlas is already running: ' + url)
            if not no_browser:
                webbrowser.open(url)
            return
        raise ValueError('This portable library is already in use')
    app = application(root)
    os.environ.update(ATLAS_PORTABLE_ROOT=str(root), ASSET_DATA_DIR=str(catalog), ASSET_BLOB_DIR=str(root / 'archive'),
                      ATLAS_PORTABLE_DEVICE=str(root.stat().st_dev),
                      ASSET_WATCH_ENABLED='0', ASSET_ALLOWED_HOSTS='localhost,127.0.0.1', ASSET_LOCAL_BOOTSTRAP='1',
                      ASSET_COOKIE_NAME='atlas_portable_' + config['id'][:12], ASSET_SECURE_COOKIE='0',
                      PYTHONDONTWRITEBYTECODE='1', ASSET_CODEX_HOME=str(root / 'no-host-codex-source'))
    os.environ.pop('PYTHONPATH', None)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(app))
    with closing(sqlite3.connect(database)) as con:
        if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Catalog quick_check failed. Refusing to start.')
        con.execute('PRAGMA journal_mode=DELETE').fetchall()
        # Old absolute archive locations are deployment metadata, not source paths.
        for table in ['repositories', 'repository_archives']:
            rows = con.execute('SELECT rowid,body FROM ' + table).fetchall()
            for rowid, body in rows:
                key = json.loads(body)['fingerprint']
                path = contained(catalog, 'git-history/objects/' + key)
                con.execute('UPDATE ' + table + ' SET archive_path=? WHERE rowid=?', (str(path) if path.is_dir() else None, rowid))
        con.commit()
    sock = socket.socket()
    for port in range(8770, 8800):
        try:
            sock.bind(('127.0.0.1', port))
            break
        except OSError:
            continue
    else:
        raise ValueError('Ports 8770–8799 are busy; existing services were left unchanged')
    nonce = uuid.uuid4().hex
    os.environ['ATLAS_PORTABLE_SESSION'] = nonce
    session = {'host': host_key(), 'pid': os.getpid(), 'port': port, 'nonce': nonce, 'dataset': config['active_dataset']}
    atomic_json(root / 'session.json', session)
    url = f'http://127.0.0.1:{port}'
    print('Atlas 随行资产库：' + url, flush=True)
    print('运行期间请保持磁盘连接。停止：网页「安全停止」或 Stop Atlas.command / sh stop.sh', flush=True)
    def open_ready():
        for _ in range(120):
            try:
                if health(port).get('portable_session') == nonce:
                    if not no_browser:
                        webbrowser.open(url)
                    return
            except OSError:
                pass
            time.sleep(0.5)
    threading.Thread(target=open_ready, daemon=True).start()
    import uvicorn
    # Uvicorn re-raises captured signals after orderly shutdown. Keep the Python
    # process alive long enough to close/checkpoint the portable database below.
    signal.signal(signal.SIGTERM,lambda *_: None)
    signal.signal(signal.SIGINT,lambda *_: None)
    try:
        uvicorn.Server(uvicorn.Config('asset_manager.server:app', host='127.0.0.1', port=port, workers=1, timeout_graceful_shutdown=60)).run(sockets=[sock])
    finally:
        sock.close()
        # Finite background archive/conversion jobs may outlive their HTTP request.
        for worker in threading.enumerate():
            if worker.name.startswith('atlas-job-') and worker.is_alive():
                print('等待资产任务结束后保存目录库…',flush=True)
                worker.join()
        # Preserve a consistent recovery copy after every orderly stop.
        backups = root / 'backups'
        backups.mkdir(exist_ok=True)
        temporary = backups / 'catalog-clean-stop.sqlite3.tmp'
        with closing(sqlite3.connect(database)) as src, closing(sqlite3.connect(temporary)) as dst:
            src.backup(dst)
            result = dst.execute('PRAGMA quick_check').fetchone()[0]
            if result != 'ok':
                raise ValueError('Shutdown database check failed; keep the disk attached')
        os.replace(temporary, backups / 'catalog-clean-stop.sqlite3')
        atomic_json(root / 'reports' / 'last-stop.json', {'status': 'clean', 'time': time.time(), 'database_sha256': digest(database)})
        (root / 'session.json').unlink(missing_ok=True)
        lock.close()
        print('服务已停止，目录库已保存。现在可以通过操作系统弹出磁盘。', flush=True)


def main():
    root = Path(sys.argv[1]).resolve()
    action = sys.argv[2] if len(sys.argv) > 2 else 'start'
    if action == 'stop':
        stop(root)
    elif action == 'verify':
        app = application(root)
        subprocess.run([sys.executable, str(app / 'scripts/export-portable.py'), str(root), '--verify'], check=True)
    elif action == 'start':
        start(root, '--no-browser' in sys.argv or os.environ.get('ATLAS_NO_BROWSER') == '1')
    else:
        raise ValueError('Use start, stop or verify')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print('Atlas: ' + str(error), file=sys.stderr)
        sys.exit(1)
