"""Rebase rendered Markdown destinations, never prose or executable examples.

Offsets refer to the original string so labels, titles and whitespace stay exact.
Supported forms are inline links/images, reference definitions and file: autolinks.
No Markdown or HTML is executed while discovering resources.
"""
import base64
import collections
import copy
import hashlib
import html
import json
import os
import posixpath
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

MEDIA = {'.png': 'image', '.jpg': 'image', '.jpeg': 'image', '.gif': 'image',
         '.webp': 'image', '.svg': 'image', '.avif': 'image', '.bmp': 'image',
         '.mp4': 'video', '.webm': 'video', '.mov': 'video',
         '.mp3': 'audio', '.m4a': 'audio', '.wav': 'audio', '.ogg': 'audio'}
ESCAPE = re.compile(r'\\([!"#$%&\'()*+,\-./:;<=>?@\[\]\\^_`{|}~])')


def _mask_code(text):
    mask = bytearray(len(text))
    for match in re.finditer(r'<!--[\s\S]*?(?:-->|$)|<(pre|code|script|style)(?:\s[^>]*)?>[\s\S]*?</\1\s*>', text, re.I):
        mask[match.start():match.end()] = b'\1' * (match.end() - match.start())
    offset, fence = 0, None
    for line in text.splitlines(keepends=True):
        # Quote prefixes do not make a fenced example executable Markdown.
        body = re.sub(r'^(?: {0,3}> ?)+', '', line)
        marker = re.match(r' {0,3}(`{3,}|~{3,})(.*)', body)
        protected = fence is not None or bool(marker) or body.startswith(('    ', '\t'))
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
        elif marker:
            fence = (marker[1][0], len(marker[1]))
        if protected:
            mask[offset:offset + len(line)] = b'\1' * len(line)
        offset += len(line)
    # Protect inline code only when a matching delimiter exists.
    i = 0
    while i < len(text):
        if mask[i] or text[i] != '`' or (i and text[i - 1] == '\\'):
            i += 1
            continue
        end = i + 1
        while end < len(text) and text[end] == '`':
            end += 1
        pattern = re.compile(r'(?<!`)' + re.escape(text[i:end]) + r'(?!`)')
        close = next((m for m in pattern.finditer(text, end) if not mask[m.start()]), None)
        if close:
            mask[i:close.end()] = b'\1' * (close.end() - i)
            i = close.end()
        else:
            i = end
    return mask


def _destination(text, start):
    n = len(text)
    while start < n and text[start] in ' \t\n':
        start += 1
    if start >= n:
        return None
    if text[start] == '<':
        end = start + 1
        while end < n:
            if text[end] in '\r\n':
                return None
            if text[end] == '\\':
                end += 2
                continue
            if text[end] == '>':
                return start + 1, end, end + 1
            end += 1
        return None
    end, depth = start, 0
    while end < n:
        char = text[end]
        if char == '\\' and end + 1 < n:
            end += 2
            continue
        if char in ' \t\r\n' or (char == ')' and not depth):
            break
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif char == '<':
            return None
        end += 1
    return (start, end, end) if end > start and not depth else None


def destinations(text):
    """Yield exact destination spans. Code fences and inline code are excluded."""
    mask = _mask_code(text)
    found, stack, i = {}, [], 0
    while i < len(text):
        if mask[i]:
            i += 1
            continue
        if text[i] == '\\':
            i += 2
            continue
        if text[i] == '[':
            stack.append(i)
        elif text[i] == ']' and stack:
            opener = stack.pop()
            if text[i + 1:i + 2] == '(':
                span = _destination(text, i + 2)
                if span:
                    a, b, end = span
                    tail = re.match(r'\s*(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\((?:\\.|[^)\\])*\))?\s*\)', text[end:])
                    if tail and not any(mask[a:b]):
                        found[(a, b)] = 'image' if opener and text[opener - 1] == '!' else 'link'
                        i = end + tail.end()
                        continue
        i += 1
    for match in re.finditer(r'^ {0,3}\[(?:\\.|[^\]\\\n])+\]:[ \t]*', text, re.M):
        if mask[match.start()]:
            continue
        span = _destination(text, match.end())
        if span and not any(mask[span[0]:span[1]]):
            found[span[:2]] = 'reference'
    for match in re.finditer(r'<(file://[^<>\s]+)>', text):
        if not any(mask[match.start():match.end()]):
            found[match.span(1)] = 'autolink'
    return [{'start': a, 'end': b, 'destination': text[a:b], 'syntax': syntax}
            for (a, b), syntax in sorted(found.items())]


def local_destination(raw):
    value = html.unescape(ESCAPE.sub(r'\1', raw))
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in {'', 'file'} or parts.netloc not in {'', 'localhost'}:
        return None
    path = unquote(parts.path)
    if not path.startswith('/') or path.startswith('//') or '\x00' in path:
        return None
    return {'path': path, 'scheme': parts.scheme, 'netloc': parts.netloc,
            'query': parts.query, 'fragment': parts.fragment}


def mapped_destination(raw, source_home, target_home, aliases=None):
    parsed = local_destination(raw)
    if not parsed:
        return raw
    path = parsed['path']
    mapped = (aliases or {}).get(path)
    if mapped is None and source_home and (path == source_home or path.startswith(source_home + '/')):
        normalized = posixpath.normpath(path)
        if normalized != source_home and not normalized.startswith(source_home + '/'):
            return raw  # Never turn traversal into a different host resource.
        mapped = target_home + path[len(source_home):]
    if not mapped or mapped == path:
        return raw
    result = ('file://' + parsed['netloc'] if parsed['scheme'] else '') + quote(mapped, safe='/:@-._~')
    if parsed['query']:
        result += '?' + parsed['query']
    if parsed['fragment']:
        result += '#' + parsed['fragment']
    return result


def rewrite(text, source_home, target_home, aliases=None):
    edits = []
    for span in destinations(text):
        after = mapped_destination(span['destination'], source_home, target_home, aliases)
        if after != span['destination']:
            edits.append({**span, 'after': after})
    for edit in reversed(edits):
        text = text[:edit['start']] + edit['after'] + text[edit['end']:]
    return text, edits


def item(value, source_home, target_home, aliases=None):
    if value.get('type') != 'agentMessage' or not isinstance(value.get('text'), str):
        return value
    changed, edits = rewrite(value['text'], source_home, target_home, aliases)
    return {**value, 'text': changed} if edits else value


def record(value, source_home, target_home, aliases=None):
    """Only assistant display text, including its event/response mirrors."""
    payload = value.get('payload', {})
    if value.get('type') != 'event_msg' and not (value.get('type') == 'response_item' and
            payload.get('type') == 'message' and payload.get('role') == 'assistant'):
        return value
    value = copy.deepcopy(value)
    payload = value.get('payload', {})
    if value.get('type') == 'event_msg':
        if isinstance(payload.get('item'), dict):
            payload['item'] = item(payload['item'], source_home, target_home, aliases)
        for kind, field in [('agent_message', 'message'), ('task_complete', 'last_agent_message')]:
            if payload.get('type') == kind and isinstance(payload.get(field), str):
                payload[field] = rewrite(payload[field], source_home, target_home, aliases)[0]
    if value.get('type') == 'response_item' and payload.get('type') == 'message' and payload.get('role') == 'assistant':
        for part in payload.get('content', []):
            if part.get('type') in {'output_text', 'text'} and isinstance(part.get('text'), str):
                part['text'] = rewrite(part['text'], source_home, target_home, aliases)[0]
    return value


def resources(text):
    for span in destinations(text):
        parsed = local_destination(span['destination'])
        if parsed:
            path = parsed['path']
            # Preserve a literal colon filename if it actually exists.
            if not Path(path).exists():
                path = re.sub(r':\d+(?::\d+)?$', '', path)
            yield {**span, 'path': path, 'media': MEDIA.get(Path(path).suffix.lower(), 'file')}


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 ** 2), b''):
            digest.update(block)
    return digest.hexdigest()


def audit(codex_home, source_home=None, target_home=None, client=None):
    """Check rewritten addresses and read available media through native Codex.

    Missing resources are explicit gaps. They are not labelled recovered or
    decoded. The original raw snapshot remains the evidence for lost files.
    """
    db = Path(codex_home) / 'thread_history_1.sqlite'
    counts, paths, errors, remaining, gaps = collections.Counter(), {}, [], [], []
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as c:
        c.execute('PRAGMA query_only=ON')
        for thread, turn, ident, raw in c.execute("SELECT thread_id,turn_id,item_id,item_json FROM thread_items WHERE item_type='agentMessage'"):
            value = json.loads(raw)
            for ref in resources(value.get('text', '')):
                counts[ref['media']] += 1
                location = {'thread_id': thread, 'turn_id': turn, 'item_id': ident, **ref}
                if source_home and mapped_destination(ref['destination'], source_home, target_home) != ref['destination']:
                    remaining.append(location)
                paths.setdefault(ref['path'], {'path': ref['path'], 'media': ref['media'], 'references': []})['references'].append(location)
    reads = []
    for path, detail in paths.items():
        file = Path(path)
        if not file.exists():
            gaps.append(detail)
            continue
        if not file.is_file():
            continue
        if not os.access(file, os.R_OK):
            errors.append({'path': path, 'reason': 'not_readable'})
            continue
        if detail['media'] == 'file':
            continue
        try:
            check = {'path': path, 'media': detail['media'], 'bytes': file.stat().st_size, 'sha256': sha(file)}
        except OSError as exc:
            errors.append({'path': path, 'reason': str(exc)[:400]})
            continue
        if client:
            try:
                if check['bytes'] <= 16 * 1024 ** 2:
                    response = client.request('fs/readFile', {'path': path}, timeout=60)
                    data = base64.b64decode(response['dataBase64'], validate=True)
                    if len(data) != check['bytes'] or hashlib.sha256(data).hexdigest() != check['sha256']:
                        raise ValueError('native_file_bytes_differ')
                    check['native_read'] = 'passed'
                else:
                    metadata = client.request('fs/getMetadata', {'path': path}, timeout=60)
                    if not metadata.get('isFile'):
                        raise ValueError('native_metadata_not_file')
                    check['native_read'] = 'metadata_only_large_file'
            except (ValueError, KeyError, TypeError, OSError) as exc:
                errors.append({'path': path, 'reason': str(exc)[:400]})
                check['native_read'] = 'failed'
        reads.append(check)
    return {'status': 'failed' if errors or remaining else 'passed', 'schema_version': 1,
            'scope': 'assistant_markdown_local_destinations_and_available_native_media_reads',
            'reference_counts': dict(counts), 'unique_paths': len(paths), 'remaining_old_references': remaining,
            'missing_resources': gaps, 'available_media': reads, 'errors': errors,
            'native_checked': client is not None, 'all_resources_available': not gaps,
            'checked_at': time.time(), 'visual_rendering_verified': False}
