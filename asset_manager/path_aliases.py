"""Resolve historical machine paths without rewriting historical text."""
import json
from . import db


def project_roots(projects, aliases=None):
    if aliases is None:
        aliases = json.loads(db.setting('source_path_aliases', '[]'))
    result = list(projects)
    for project in projects:
        for alias in aliases:
            current, previous = alias['target'].rstrip('/'), alias['source'].rstrip('/')
            path = project['path']
            if path == current or path.startswith(current + '/'):
                result.append({**project, 'path': previous + path[len(current):], 'historical_alias': True})
    return result
