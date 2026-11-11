"""
笔记工坊的回归测试（notes.py / notes_forge.py）。

三个层次：
1. notes_forge 管线关键断言（从 notes-forge/t_subjects.py 移植最易退化的几组）
   —— 这份 vendored 副本以后会独立演化，必须有它自己的护栏，不能"以为和原版一样"。
2. notes.py 存储纯逻辑：index 合并、key 生成、payload 组装。
3. push_files 的 GitHub 调用序列（mock _gh，不发真请求）：
   blob→tree→commit→ref 的顺序和参数错一点，产物就推不上去。

跑法： venv python test_notes.py （用临时环境，不碰真实 secrets 之外的东西）
"""
import json
import os
import sys

os.environ.setdefault('STUDY_LLM_API_KEY', 'sk-test-local')
os.environ.setdefault('STUDY_VISION_API_KEY', 'sk-test-local')

import notes_forge as F
import notes as N

PASS = 0


def ok(cond, msg):
    global PASS
    if not cond:
        print('FAIL:', msg)
        sys.exit(1)
    PASS += 1
    print(f'  ok {PASS:02d} {msg}')


# ============ 1. 管线关键断言（防 vendored 副本退化） ============
print('--- notes_forge 管线 ---')

cfg_m = F.subject_cfg('math')
ok('ex_prompt' not in cfg_m, '数学配置不被覆盖（fallback 到原常量）')
ok('题' in F.subject_cfg('english')['example_prefix'], '英语例题前缀含 页兜底「题」')
ok(F.subject_cfg('unknown-subj')['min_sections'] == F.SUBJECTS['math']['min_sections'],
   '未知科目落回数学，不抛异常')

pages_en = [
    {'no': i, 'topic': f't{i}', 'points': ['a'], 'role': 'content', 'belongs': 'this',
     'examples': [], 'page_text': ''}
    for i in range(1, 7)
]
pages_en[3].update(role='example', examples=['Task 2'])
pages_en[4].update(role='practice', examples=['练习1'])
pages_en[5].update(role='practice', examples=['随便写的没有题号'])   # 认不出 → 兜底按页
groups = F.group_examples(pages_en, subject='english')
nos = [g['no'] for g in groups]
ok('练习1' in nos and 'Task2' in nos,
   f'英语题号识别（空格归一成 Task2）: {nos}')
groups_fb = F.group_examples(pages_en[5:6], subject='english')   # 只有"认不出题号"的页
ok([g['no'] for g in groups_fb] == ['题1'], '无题号按页兜底 题1')

groups_ch = F.group_examples([p for p in pages_en[:3] if True], subject='chinese')
ok([g['no'] for g in groups_ch] == [], '无题号且已匹配不到 → 空（有匹配优先于兜底）')

mid = [{'no': i, 'role': 'content', 'belongs': 'prev', 'topic': 'x', 'points': ['y'],
        'page_text': ''} for i in range(1, 15)]
pulled = F.clamp_mid_prev(mid, head=10)
ok(pulled == [11, 12, 13, 14], f'中段 prev 误判拉回: {pulled}')

lesson = {'mindmap': {'branches': [{'t': 'A', 'link': 'sec-nope'},
                                   {'t': 'B', 'link': 'sec-b'}]},
          'sections': [{'id': 'sec-b', 'title': 'B'}]}
fixes = F.repair_links(lesson)
ok(fixes and lesson['mindmap']['branches'][0].get('link') is None
   and lesson['mindmap']['branches'][1]['link'] == 'sec-b',
   '导图死链摘除、活链保留')

secs = [{'id': 's1', 'title': 'x', 'pages': [2]},    # p2 = 本讲 content 页 → 保留
        {'id': 's2', 'title': 'y', 'pages': [1, 3]}]  # p1 prev + p3 example → 丢
pg2 = [
    {'no': 1, 'belongs': 'prev', 'role': 'content', 'topic': '', 'points': [], 'page_text': ''},
    {'no': 2, 'belongs': 'this', 'role': 'content', 'topic': '', 'points': [], 'page_text': ''},
    {'no': 3, 'belongs': 'this', 'role': 'example', 'topic': '', 'points': [], 'page_text': ''},
]
kept = F.drop_prev_only_sections(secs, pg2, on_log=None)
ok([s['id'] for s in kept] == ['s1'], '只有上一讲/例题页撑着的模块被丢弃')

# ============ 2. 存储纯逻辑 ============
print('--- notes.py 存储逻辑 ---')

ok(N.lesson_key_for('math', 8) == 'lesson08'
   and N.lesson_key_for('chinese', 8) == 'ch08'
   and N.lesson_key_for('english', 8) == 'en08', '三科 key 命名')
ok(N.gallery_sub_for('math', 8) == 'L8' and N.gallery_sub_for('chinese', 8) == 'C8'
   and N.gallery_sub_for('english', 8) == 'E8', '三科图片目录命名')

old_idx = [{'id': 'lesson06', 'subject': 'math', 'no': 6},
           {'id': 'ch08', 'subject': 'chinese', 'no': 8}]
newp = {'id': 'lesson08', 'subject': 'math', 'no': 8, 'title': 'T'}
merged = N._merge_index(old_idx, newp)
ids = [e['id'] for e in merged]
ok(ids == ['lesson06', 'lesson08', 'ch08'], f'index 合并按 科目→讲号 排序: {ids}')
merged2 = N._merge_index(merged, {'id': 'lesson08', 'subject': 'math', 'no': 8,
                                  'title': 'T2'})
ok(len(merged2) == 3 and [e for e in merged2 if e['id'] == 'lesson08'][0]['title'] == 'T2',
   '同 id 覆盖不重复（index.js 名单重复的教训）')

out = {'lesson': {'id': 'en08', 'no': '8', 'subject': 'english'}, 'board': {'sections': {}},
       'ingest': {'count': 61}}
pl = N.build_payload(out, updated='2026-11-11')
ok(pl['pages'] == 61 and pl['updated'] == '2026-11-11' and pl['board'] == {'sections': {}},
   'payload 一体存储 lesson+board+pages')

# ============ 3. push_files 调用序列（mock） ============
print('--- push_files 调用序列 ---')


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


calls = []


def fake_gh(method, path, payload=None, raw=None, timeout=60):
    calls.append((method, path, payload))
    if method == 'GET' and path.endswith('/repos/jovi0112/notes-data'):
        return FakeResp(200, {'name': 'notes-data'})
    if method == 'GET' and path.endswith('/git/ref/heads/main'):
        return FakeResp(200, {'object': {'sha': 'BASECOMMIT'}})
    if method == 'GET' and path.startswith('/repos/jovi0112/notes-data/git/commits/'):
        return FakeResp(200, {'tree': {'sha': 'BASETREE'}})
    if method == 'POST' and path.endswith('/git/blobs'):
        return FakeResp(201, {'sha': 'BLOB-' + str(len(calls))})
    if method == 'POST' and path.endswith('/git/trees'):
        return FakeResp(201, {'sha': 'NEWTREE'})
    if method == 'POST' and path.endswith('/git/commits'):
        return FakeResp(201, {'sha': 'NEWCOMMIT'})
    if method == 'PATCH' and path.endswith('/git/refs/heads/main'):
        return FakeResp(200, {'object': {'sha': 'NEWCOMMIT'}})
    raise AssertionError('意外调用: ' + method + ' ' + path)


N._gh = fake_gh
files = {'lessons/en08.json': json.dumps({'a': 1}).encode(),
         'pages/E8/p01.jpg': b'\xff\xd8jpgbytes',
         'index.json': '[]'}     # 文本走 utf-8 编码路径，图片走 base64
sha = N.push_files(files, 'test commit')
ok(sha == 'NEWCOMMIT', 'push_files 返回新 commit sha')

kinds = [(m, p.rsplit('/', 2)[-2] if '/git/' in p else p) for m, p, _ in calls]
blobs = [c for c in calls if c[1].endswith('/git/blobs')]
ok(len(blobs) == 3, '三个文件 = 三个 blob')
text_blob = [b for b in blobs if b[2]['encoding'] == 'utf-8']
bin_blob = [b for b in blobs if b[2]['encoding'] == 'base64']
ok(len(text_blob) == 1 and len(bin_blob) == 2, '文本 utf-8 / 二进制 base64 分开编码')
tree_call = [c for c in calls if c[1].endswith('/git/trees')][0]
ok(tree_call[2]['base_tree'] == 'BASETREE' and len(tree_call[2]['tree']) == 3,
   'tree 基于 base_tree 且含 3 项')
commit_call = [c for c in calls if c[1].endswith('/git/commits')][0]
ok(commit_call[2]['parents'] == ['BASECOMMIT'] and commit_call[2]['tree'] == 'NEWTREE',
   'commit 挂在远端当前 head 上（不分叉）')
patch_call = [c for c in calls if c[1].endswith('/git/refs/heads/main')][0]
ok(patch_call[2]['sha'] == 'NEWCOMMIT' and patch_call[2]['force'] is False,
   'ref 更新用新 commit 且不 force')

# ============ 4. 就绪自检（不联网也要给人话） ============
print('--- 自检 ---')
r = N.ready()
ok(r[0] is True or ('pymupdf' in r[1] or 'Secrets' in r[1]), f'ready() 返回人话: {r}')
ts = N.token_status()      # 本地没配 STUDY_BACKUP_TOKEN 的环境变量时也不许崩
ok(ts.get('ok') in (True, False), 'token_status() 不崩')

# ============ 5. 结构断言：app.py 的 notes.xxx 调用 ⊆ 守卫名单 ============
# 名单式自检天生会漏（scan 的 box_ratio 实测漏过），所以用源码扫描兜底：
# 只要 app.py 里调了名单外的 notes 函数，这里立刻红。
import re as _re
here = os.path.dirname(os.path.abspath(__file__))
app_src = open(os.path.join(here, 'app.py'), encoding='utf-8').read()
called = set(_re.findall(r'\bnotes\.(\w+)', app_src))
need_src = open(os.path.join(here, 'app.py'), encoding='utf-8').read()
m = _re.search(r'_NOTES_NEED\s*=\s*\(([^)]*)\)', need_src, _re.S)
need = set(_re.findall(r"'(\w+)'", m.group(1))) if m else set()
ok(bool(need), 'app.py 里有 _NOTES_NEED 守卫名单')
missing_in_list = called - need
ok(not missing_in_list,
   f'app.py 的 notes 调用都在守卫名单里（漏了: {missing_in_list}）')
ok(N.MODULE_VER >= 1, 'notes.MODULE_VER 存在（版本号兜底层）')

print(f'\nALL NOTES CHECKS OK ({PASS} 项)')
