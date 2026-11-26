import base64
import copy
import json
import shutil
import sqlite3

import pytest

from asset_manager import codex_links as links, codex_history as history, codex_profile as profile
from test_codex_history import delta_fixture


@pytest.mark.parametrize('before,after', [
    ('![图](/old/a.png)', '![图](/new/a.png)'),
    ('[文档](</old/中文 a.html> "保留 /old/标题")', '[文档](</new/%E4%B8%AD%E6%96%87%20a.html> "保留 /old/标题")'),
    ('[代码](/old/a.py:12#L12)', '[代码](/new/a.py:12#L12)'),
    ('[图](/old/a(b).png)', '[图](/new/a%28b%29.png)'),
    (r'[图](/old/a\(b\).png)', '[图](/new/a%28b%29.png)'),
    ('![图][r]\n[r]: /old/a.png "标题"', '![图][r]\n[r]: /new/a.png "标题"'),
    ('<file:///old/a.png>', '<file:///new/a.png>'),
    ('[文件](file://localhost/old/a%20b.png?x=1#preview)', '[文件](file://localhost/new/a%20b.png?x=1#preview)'),
    ('[![图](/old/a.png)](/old/page.html)', '[![图](/new/a.png)](/new/page.html)'),
])
def test_rewrite_only_markdown_destinations(before, after):
    result, edits = links.rewrite(before, '/old', '/new')
    assert result == after and edits
    assert links.rewrite(result, '/old', '/new') == (result, [])


@pytest.mark.parametrize('text', [
    '普通路径 /old/a.png', '`![图](/old/a.png)`',
    '```md\n![图](/old/a.png)\n```', '> ```\n> ![图](/old/a.png)\n> ```',
    '    ![图](/old/a.png)', '<!-- ![图](/old/a.png) -->',
    '<pre>![图](/old/a.png)</pre>', '[网络](https://host/old/a.png)',
    '[聊天](codex://threads/old)', '[相对](a.png)', '[邻居](/older/a.png)',
    '[越界](/old/../secret.png)', '[坏语法](/old/a.png', r'\[普通](/old/a.png)',
])
def test_non_destinations_are_exactly_preserved(text):
    assert links.rewrite(text, '/old', '/new') == (text, [])


def test_alias_and_record_mirrors_do_not_modify_tool_or_user_content():
    body = '![图](/tmp/expired.png) 和 /old/path'
    aliases = {'/tmp/expired.png': '/new/.codex/attachments/picture.png'}
    wanted = '![图](/new/.codex/attachments/picture.png) 和 /old/path'
    records = [
        {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {'type': 'agentMessage', 'text': body}}},
        {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': body}},
        {'type': 'event_msg', 'payload': {'type': 'task_complete', 'last_agent_message': body}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type':'output_text','text':body}]}},
    ]
    for record in records:
        original = copy.deepcopy(record)
        changed = links.record(record, '/old', '/new', aliases)
        assert wanted in json.dumps(changed, ensure_ascii=False)
        assert record == original
    for kind in ['user', 'tool']:
        record = {'type': 'response_item', 'payload': {'type':'message','role':kind,'content':[{'type':'text','text':body}]}}
        assert links.record(record, '/old', '/new', aliases) == record
    tool = {'type':'commandExecution','command':body,'arguments':{'path':'/old/a'}}
    assert links.item(tool, '/old', '/new') == tool


def test_database_log_offset_and_preservation_are_consistent(tmp_path):
    home = delta_fixture(tmp_path)
    text = '正文 /old/a.png\n![图片](/old/a.png)\n`[例子](/old/a.png)`'
    with sqlite3.connect(home/'thread_history_1.sqlite') as c:
        c.execute('UPDATE thread_items SET item_json=?', (json.dumps({'type':'agentMessage','text':text}),))
    for log in (home/'sessions').glob('*.jsonl'):
        records = [json.loads(line) for line in log.read_text().splitlines()]
        records[2]['payload'] = {'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}
        log.write_text(''.join(json.dumps(r)+'\n' for r in records))
    before = tmp_path/'before'; shutil.copytree(home, before)
    assert history.repair_item_paths(home, '/old', '/new') == {'agentMessage':2}
    profile.repair_rollout_metadata(home, '/old', '/new')
    assert history.offset_plan(home)['status'] == 'passed'
    assert history.preservation(before, home, '/old', '/new')['status'] == 'passed'
    for log in (home/'sessions').glob('*.jsonl'):
        record = json.loads(log.read_text().splitlines()[2])
        assert record['payload']['content'][0]['text'] == links.rewrite(text, '/old', '/new')[0]
    with sqlite3.connect(home/'thread_history_1.sqlite') as c:
        c.execute('UPDATE thread_items SET item_json=?', (json.dumps({'type':'agentMessage','text':'changed prose'}),))
    assert history.preservation(before, home, '/old', '/new')['status'] == 'failed'


def test_audit_reads_native_bytes_and_separately_reports_missing_resources(tmp_path):
    home=tmp_path/'profile'; home.mkdir()
    png=tmp_path/'image.png'; png.write_bytes(b'example bytes')
    text=f'![good]({png})\n![missing]({tmp_path}/missing.png)\n![old](/old/image.png)'
    with sqlite3.connect(home/'thread_history_1.sqlite') as c:
        c.execute('CREATE TABLE thread_items(thread_id,turn_id,item_id,item_type,item_json)')
        c.execute('INSERT INTO thread_items VALUES(?,?,?,?,?)', ('t','u','i','agentMessage',json.dumps({'type':'agentMessage','text':text})))
    class Client:
        corrupt=False
        def request(self, method, params, **kwargs):
            assert method=='fs/readFile' and params=={'path':str(png)}
            return {'dataBase64':base64.b64encode(b'wrong' if self.corrupt else png.read_bytes()).decode()}
    client=Client()
    assert links.audit(home, '/old', '/new', client)['status']=='failed'
    history.repair_item_paths(home, '/old', '/new')
    report=links.audit(home, '/old', '/new', client)
    assert report['status']=='passed' and report['native_checked']
    assert len(report['missing_resources'])==2 and not report['all_resources_available']
    assert report['available_media'][0]['native_read']=='passed'
    assert not report['visual_rendering_verified']
    client.corrupt=True
    assert links.audit(home, '/old', '/new', client)['status']=='failed'
