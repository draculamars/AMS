import contextlib
import json
import os
import sqlite3
from pathlib import Path

DATA = Path(os.environ.get('ASSET_DATA_DIR', Path(__file__).resolve().parents[1] / 'data')).resolve()

SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE, host TEXT DEFAULT 'local',
 git_json TEXT DEFAULT '{}', scan_at TEXT, scan_error TEXT, excluded_json TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS assets(
 id INTEGER PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), relpath TEXT NOT NULL,
 name TEXT NOT NULL, ext TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER NOT NULL, mtime REAL,
 git_state TEXT NOT NULL, repo_root TEXT, present INTEGER DEFAULT 1, tags TEXT DEFAULT '', note TEXT DEFAULT '',
 managed INTEGER DEFAULT 0, UNIQUE(project_id,relpath));
CREATE INDEX IF NOT EXISTS assets_filter ON assets(project_id,kind,present);
CREATE TABLE IF NOT EXISTS versions(
 id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(id), sha256 TEXT NOT NULL,
 size INTEGER NOT NULL, created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audits(
 id INTEGER PRIMARY KEY, action TEXT NOT NULL, detail TEXT NOT NULL,
 created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
CREATE TABLE IF NOT EXISTS plans(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, body TEXT NOT NULL, status TEXT DEFAULT 'pending',
 created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, done INTEGER DEFAULT 0,
 total INTEGER DEFAULT 0, detail TEXT DEFAULT '', result TEXT DEFAULT '{}',
 created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
CREATE TABLE IF NOT EXISTS model_settings(
 id TEXT PRIMARY KEY, title TEXT, entry_asset_id INTEGER, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS model_snapshots(
 id TEXT PRIMARY KEY, model_id TEXT NOT NULL, body TEXT NOT NULL,
 created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
CREATE INDEX IF NOT EXISTS model_snapshot_lookup ON model_snapshots(model_id,created_at);
CREATE TABLE IF NOT EXISTS model_relations(
 model_id TEXT NOT NULL, source_id INTEGER NOT NULL REFERENCES assets(id),
 target_id INTEGER NOT NULL REFERENCES assets(id), relation TEXT NOT NULL,
 PRIMARY KEY(model_id,source_id,target_id,relation));
CREATE TABLE IF NOT EXISTS repository_archives(
 repository_id TEXT NOT NULL, fingerprint TEXT NOT NULL, body TEXT NOT NULL, archive_path TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(repository_id,fingerprint)
);
CREATE TABLE IF NOT EXISTS repositories(
 id TEXT PRIMARY KEY, source_root TEXT NOT NULL, common_dir TEXT NOT NULL,
 body TEXT NOT NULL, indexed_at TEXT NOT NULL, archive_path TEXT, archive_at TEXT);
CREATE TABLE IF NOT EXISTS project_repositories(
 project_id TEXT NOT NULL REFERENCES projects(id), repository_id TEXT NOT NULL REFERENCES repositories(id),
 prefix TEXT NOT NULL, checkout_root TEXT NOT NULL, head TEXT, branch TEXT, role TEXT,
 PRIMARY KEY(project_id,prefix));
CREATE TABLE IF NOT EXISTS project_directories(
 project_id TEXT NOT NULL REFERENCES projects(id), path TEXT NOT NULL,
 PRIMARY KEY(project_id,path));
CREATE TABLE IF NOT EXISTS project_index_runs(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), created_at TEXT NOT NULL,
 body TEXT NOT NULL, manifest TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS project_runs_lookup ON project_index_runs(project_id,created_at);
CREATE TABLE IF NOT EXISTS project_relations(
 id TEXT PRIMARY KEY, source_project TEXT NOT NULL, source_path TEXT NOT NULL,
 target_project TEXT, target_path TEXT, reference TEXT NOT NULL, kind TEXT NOT NULL,
 status TEXT NOT NULL, evidence TEXT NOT NULL, manual INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS project_relation_source ON project_relations(source_project,source_path);
CREATE INDEX IF NOT EXISTS project_relation_target ON project_relations(target_project,target_path);
CREATE TABLE IF NOT EXISTS project_documents(
 project_id TEXT NOT NULL, path TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
 truncated INTEGER DEFAULT 0, PRIMARY KEY(project_id,path));
CREATE TABLE IF NOT EXISTS project_index_state(
 project_id TEXT PRIMARY KEY REFERENCES projects(id), body TEXT NOT NULL, indexed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS project_locations(
 project_id TEXT PRIMARY KEY REFERENCES projects(id), node_id TEXT NOT NULL, mode TEXT NOT NULL,
 source_node TEXT, source_path TEXT, refreshed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mirror_files(
 project_id TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
 PRIMARY KEY(project_id,path));
CREATE TABLE IF NOT EXISTS development_sessions(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS development_turns(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, project_id TEXT NOT NULL, started_at TEXT NOT NULL,
 body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS development_project ON development_turns(project_id,started_at);
CREATE TABLE IF NOT EXISTS development_artifacts(
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, project_id TEXT NOT NULL, path TEXT NOT NULL,
 action TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS development_path ON development_artifacts(project_id,path);
CREATE INDEX IF NOT EXISTS development_artifact_turn ON development_artifacts(turn_id);
CREATE TABLE IF NOT EXISTS source_edit_plans(
 id TEXT PRIMARY KEY, asset_id INTEGER NOT NULL, before_sha TEXT NOT NULL, after_sha TEXT NOT NULL,
 body TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS project_preferences(project_id TEXT PRIMARY KEY, favorite INTEGER DEFAULT 0, note TEXT DEFAULT '');
'''

@contextlib.contextmanager
def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA / 'catalog.sqlite3', timeout=60)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()

def blob_root():
    return Path(os.environ.get('ASSET_BLOB_DIR', DATA / 'blobs')).expanduser().resolve()

def init(recover_jobs=False):
    with connect() as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript(SCHEMA)
        old_sql=con.execute("SELECT sql FROM sqlite_master WHERE name='versions'").fetchone()[0]
        if 'UNIQUE(asset_id,sha256,reason)' in old_sql:
            con.executescript("""BEGIN IMMEDIATE;
                ALTER TABLE versions RENAME TO versions_legacy;
                CREATE TABLE versions(id INTEGER PRIMARY KEY,asset_id INTEGER NOT NULL REFERENCES assets(id),sha256 TEXT NOT NULL,size INTEGER NOT NULL,
                  created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),reason TEXT NOT NULL);
                INSERT INTO versions SELECT * FROM versions_legacy;
                DROP TABLE versions_legacy;
                COMMIT;""")
        con.execute("CREATE INDEX IF NOT EXISTS versions_asset ON versions(asset_id,id)")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS snapshot_once ON versions(asset_id,sha256) WHERE reason='snapshot'")
        con.execute("UPDATE assets SET kind='model' WHERE ext IN ('.brep','.usda','.usdc','.xacro','.fcstd','.sldasm','.sldprt','.f3d','.f3z','.iam','.ipt','.scad') AND kind<>'model'")
        if recover_jobs:
            con.execute("UPDATE jobs SET status='interrupted',detail='服务重启中断了任务，可重新执行' WHERE status IN ('queued','running')")
    from . import vault
    vault.init()
    blob_root().mkdir(parents=True, exist_ok=True)
    (DATA / 'tmp').mkdir(exist_ok=True)
    os.chmod(DATA, 0o700)
    os.chmod(DATA / 'catalog.sqlite3', 0o600)

def audit(con, action, detail):
    con.execute('INSERT INTO audits(action,detail) VALUES(?,?)', (action, json.dumps(detail, ensure_ascii=False)))

def setting(key, default=None):
    with connect() as con:
        r = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return r['value'] if r else default
