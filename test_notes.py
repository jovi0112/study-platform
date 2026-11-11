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

# ============ 5. 看图失败的处理（2026-11-11 线上 41 页只认出 2 页那次） ============
# 事故回顾：云端 41 页里 39 页失败，日志显示「第 38 页看图失败：」后面什么都没有，
# 最后停在「结构断言没过」，把接口故障说成了模型手气不好。下面几条就是钉这几处。
print('--- 看图失败的处理 ---')
ok(F.err_text(Exception()).startswith('Exception: '), '空异常也有类型名，不会只剩一个冒号')
ok('Timeout' in F.err_text(TimeoutError()), '内置 TimeoutError（str 为空）也能显示类型')
ok(F.err_text(ValueError('第 3 页返回的不是 JSON')) == 'ValueError: 第 3 页返回的不是 JSON',
   '普通异常文本照原样带出来')
src_f = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'notes_forge.py'),
             encoding='utf-8').read()
ok('{raw[:80]!r}' in src_f, '非 JSON 报错用 !r 打印，纯空白回复不会显示成空白')
msg = F.vision_guard(2, 41, [{'error': 'TimeoutError: '}, {'error': 'TimeoutError: '},
                             {'error': '_EmptyAnswer: 第 23 页模型返回空内容'}])
ok(msg and '看图通道故障' in msg and '只认出 2 页' in msg, f'识别率 5% 触发人话: {(msg or "")[:30]}')
ok('_EmptyAnswer' in msg and msg.count('TimeoutError') == 1, '错误去重后只列 3 条以内')
ok(F.vision_guard(38, 41, []) is None, '识别率 93% 不拦（正常出几页失败可以接受）')
ok(F.vision_guard(0, 0, []) is None, '总页数 0 不拦（避免除零）')

calls = []


def _fake_profile_one(page, course, no, title='', on_log=None, subject='math',
                      mode='small', model=None, rate_waits=None, **kw):
    calls.append((int(no), mode))
    if mode == 'small':                       # 模拟"并发时挂"
        raise F._EmptyAnswer(f'第 {no} 页模型返回空内容（finish_reason=stop）')
    return {'no': int(no), 'role': 'content', 'belongs': 'this', 'topic': 't',
            'examples': [], 'points': ['a'], 'handwriting': False, 'page_text': ''}


_real_po = F.profile_one
F.profile_one = _fake_profile_one
try:
    pg = [{'no': i, 'img': b'x', 'img_small': b'y', 'text': ''} for i in (1, 2, 3)]
    out = F.profile_pages(pg, '课', title='t', workers=3)
finally:
    F.profile_one = _real_po
ok(len(out['pages']) == 3 and not out['failed'],
   f'并发全挂 → 单线程 tiny 补跑全救回（{len(out["pages"])} 页）')
modes = [m for n, m in calls]
ok(modes.count('small') == 3 and modes.count('tiny') == 3,
   f'补跑用 tiny 小图，不是把原图再发一遍: {modes}')

calls.clear()


def _dead(page, course, no, title='', on_log=None, subject='math',
          mode='small', model=None, rate_waits=None, **kw):
    calls.append((int(no), mode))
    raise TimeoutError()


F.profile_one = _dead
try:
    pg = [{'no': i, 'img': b'x', 'img_small': b'y', 'text': ''} for i in range(1, 11)]
    out2 = F.profile_pages(pg, '课', title='t', workers=4)
finally:
    F.profile_one = _real_po
ok(len(calls) <= 14, f'10 页全挂时只补探 1 页（tiny→big→替补模型），总调用 {len(calls)} 次')
ok(len(out2['failed']) == 10, '探路失败 → 10 页失败记录全部保留（不吞）')
ok(all('TimeoutError' in f['error'] for f in out2['failed']), '每个失败页都带异常类型')
ok(len(F._shrink_jpg(b'not-a-jpeg')) == len(b'not-a-jpeg'), '坏图原样返回，压图不抛异常')

# ============ 6. 云端真实根因（智谱 glm-4.6v-flash）：思考吃光 token + 图片格式 ============
# 2026-11-11 线上实测：云端配的是 glm-4.6v-flash @ open.bigmodel.cn，不是本地那套 Qwen。
# 该模型默认开思考，只给 700 tokens → 正文空字符串 → 39/41 页作废。下面几条钉住适配逻辑。
print('--- 云端看图模型适配 ---')
ok(F._img_url('AAA', 'raw') == 'AAA', 'raw 风格发裸 base64（智谱口径）')
ok(F._img_url('AAA', 'dataurl').startswith('data:image/jpeg;base64,'), 'dataurl 风格带前缀（OpenAI/SiliconFlow 口径）')


class _E400(Exception):
    status_code = 400


ok(F._is_bad_request(_E400('bad')), '400 认成"参数不被接受"')
ok(not F._is_bad_request(TimeoutError()), '超时不算参数问题（不许降级成换参数）')

import types as _types
import openai as _openai

calls4 = []


class _FakeCompletions:
    def __init__(self, script):
        self.script = script

    def create(self, **kw):
        calls4.append(kw)
        step = self.script.pop(0) if self.script else ('json', 'stop')
        kind, finish = step
        if kind == 'raise400':
            raise _E400('unknown field: thinking')
        content = '' if kind == 'empty' else '{"role":"content","topic":"去分母","points":["a"]}'
        msg = _types.SimpleNamespace(content=content, reasoning_content=None)
        return _types.SimpleNamespace(choices=[_types.SimpleNamespace(message=msg, finish_reason=finish)],
                                      usage=_types.SimpleNamespace(completion_tokens=42), model='fake')


class _FakeOpenAI:
    def __init__(self, script):
        self.chat = _types.SimpleNamespace(completions=_FakeCompletions(script))


_real_openai = _openai.OpenAI
_page = {'no': 3, 'img': b'x' * 100, 'img_small': b'y' * 80, 'text': ''}
try:
    # 场景 A：带 thinking 参数被 400 → 去掉再问 → 空且 length → 加倍 tokens → 终于出 JSON
    _script = [('raise400', 'stop'), ('empty', 'length'), ('json', 'stop')]
    _openai.OpenAI = lambda **kw: _FakeOpenAI(_script)
    r = F.profile_one(_page, '一元一次方程', '3', 't', subject='math')
    ok(r['topic'] == '去分母', '被 400 → 空答截断 → 加大预算，三步内拿到结果')
    ok('extra_body' in calls4[0] and 'extra_body' not in calls4[1], '第一次带 thinking=disabled，400 后自动去掉')
    ok(calls4[2]['max_tokens'] > calls4[1]['max_tokens'], '截断后 max_tokens 真的加大了')
    ok(calls4[2]['max_tokens'] >= 1200, f'数学基础预算 ≥1200（原来是 700，被思考吃光）: {calls4[2]["max_tokens"]}')

    # 场景 B：智谱地址 → 先发裸 base64
    calls4.clear()
    os.environ['STUDY_VISION_BASE_URL'] = 'https://open.bigmodel.cn/api/paas/v4'
    _openai.OpenAI = lambda **kw: _FakeOpenAI([('json', 'stop')])
    F.profile_one(_page, '一元一次方程', '4', 't', subject='math')
    url0 = calls4[0]['messages'][1]['content'][1]['image_url']['url']
    ok(not url0.startswith('data:'), f'智谱地址先发裸 base64: {url0[:16]}…')
finally:
    _openai.OpenAI = _real_openai
    os.environ.pop('STUDY_VISION_BASE_URL', None)

# 5.7 并发按服务商定；补跑有总时长上限；探路页不许被吞
os.environ['STUDY_VISION_BASE_URL'] = 'https://open.bigmodel.cn/api/paas/v4'
ok(F.default_workers() == 1,
   f'智谱地址默认串行 1 并发（2 并发就撞账号 QPS，41 页里 24 页作废）: {F.default_workers()}')
ok(abs(F.page_gap() - 1.0) < 1e-6, f'智谱系页间节流默认 1 秒: {F.page_gap()}')
os.environ['STUDY_VISION_GAP'] = '2.5'
ok(abs(F.page_gap() - 2.5) < 1e-6, 'STUDY_VISION_GAP 可覆盖节流间隔')
os.environ.pop('STUDY_VISION_GAP', None)
os.environ['STUDY_VISION_BASE_URL'] = 'https://api.siliconflow.cn/v1'
ok(F.default_workers() == 4, f'其它厂商默认 4 并发: {F.default_workers()}')
os.environ['STUDY_VISION_WORKERS'] = '3'
ok(F.default_workers() == 3, 'STUDY_VISION_WORKERS 可覆盖')
os.environ.pop('STUDY_VISION_WORKERS', None)
os.environ.pop('STUDY_VISION_BASE_URL', None)

calls.clear()


def _bad_first(page, course, no, title='', on_log=None, subject='math',
               mode='small', model=None, rate_waits=None, **kw):
    calls.append((int(no), mode))
    if int(no) <= 7:
        raise TimeoutError()
    return {'no': int(no), 'role': 'content', 'belongs': 'this', 'topic': 't',
            'examples': [], 'points': [], 'handwriting': False, 'page_text': ''}


F.profile_one = _bad_first
os.environ['STUDY_RESCUE_BUDGET'] = '0'      # 预算为 0：第一页试完就不再补
try:
    pg = [{'no': i, 'img': b'x', 'img_small': b'y', 'text': ''} for i in range(1, 11)]
    out3 = F.profile_pages(pg, '课', title='t', workers=4)
finally:
    F.profile_one = _real_po
    os.environ.pop('STUDY_RESCUE_BUDGET', None)
ok(len(out3['failed']) == 7, f'7/10 失败时失败清单完整（探路页也在内）: {len(out3["failed"])}')
ok(all('超过补跑时间上限' in f['error'] or 'TimeoutError' in f['error'] for f in out3['failed']),
   '超预算的页带明确说明，不静默')

# ============ 7. 结构断言：app.py 的 notes.xxx 调用 ⊆ 守卫名单 ============
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
ok(int(getattr(N, '_FORGE_WANT', 0)) <= int(getattr(F, 'MODULE_VER', 0)),
   f'notes._FORGE_WANT({getattr(N, "_FORGE_WANT", "?")}) ≤ notes_forge.MODULE_VER({getattr(F, "MODULE_VER", "?")})'
   ' —— 否则线上会一直判"模块太旧"，页面进不去')
ok(int(getattr(F, 'MODULE_VER', 0)) >= 2, 'notes_forge 有版本号（selfcheck 那次没 reload 就是因为没有它）')
_m = _re.search(r'_NOTES_WANT\s*=\s*(\d+)', need_src)
ok(_m and int(_m.group(1)) == int(N.MODULE_VER),
   f'app.py 的 _NOTES_WANT 等于 notes.MODULE_VER（bump 了忘了同步的话，页面会一直判"模块太旧"）'
   f' | {_m.group(1) if _m else "?"} vs {N.MODULE_VER}')

# ============ 7. 第三轮线上实录（2026-11-11 16:40 那场）：NoSessionContext / 截断 JSON / 429 ============
# 那场的结果：主并发 37/41 页报 NoSessionContext（异常无说明），单线程补跑立刻能过；
# 另有 6 页「返回的不是 JSON」，内容其实是好的但被 ```json 围栏 + max_tokens 截断；
# 其余大半是 429「该模型当前访问量过大」。三条都要钉住。
print('--- worker 线程日志 / 截断 JSON / 限流退避 ---')

# 7.1 截断 JSON 抢救：围栏 + 尾部缺 } —— 原样 json.loads 必失败，_repair_json 要能救
_trunc = '```json\n{\n "role": "summary",\n "belongs": "this",\n "topic": "一元一次方程"'
_r = F.extract_json(_trunc)
ok(isinstance(_r, dict) and _r.get('role') == 'summary',
   f'围栏 + 截断（缺尾括号）的 JSON 能被救回: {_r}')
_r2 = F.extract_json('{"role":"content","topic":"绝对","points":["a","b",]}')
ok(isinstance(_r2, dict) and _r2.get('topic') == '绝对', f'尾逗号也能修: {_r2}')
_r3 = F.extract_json('{"role":"content","topic":"半截字符串')
ok(isinstance(_r3, dict) and _r3.get('topic') == '半截字符串', f'截断在字符串中间 → 补引号: {_r3}')
ok(F.extract_json('完全不是 JSON 的一段话') is None, '真不是 JSON 就返回 None（不假装成功）')
ok(F._repair_json('{"a":1}}') is None, '括号交错时不硬猜（返回 None，宁可报错）')
# 裸换行：glm 抄板书时 page_text 里带真换行 → json.loads 报 Invalid control character
_nl = '```json\n{"role":"content","topic":"等式","page_text":"第一行\n第二行"}\n```'
_r4 = F.extract_json(_nl)
ok(isinstance(_r4, dict) and _r4.get('page_text') == '第一行\n第二行',
   f'字符串里的裸换行也能救（转义后仍是原换行）: {_r4}')
# 结尾孤立反斜杠：补的引号会被它吃掉
_r5 = F.extract_json('{"role":"content","topic":"abc\\')
ok(isinstance(_r5, dict), f'结尾孤立反斜杠 → 丢掉再补引号: {_r5}')

# 7.2 429 真退避：_retry 要按异常类型选等待计划
_slept, _logged = [], []
_orig_sleep = F.time.sleep
F.time.sleep = lambda s: _slept.append(s)
try:
    _n = {'i': 0}


    class _Rate(Exception):
        pass


    def _boom():
        _n['i'] += 1
        if _n['i'] < 3:
            raise _Rate('Error code: 429 - 该模型当前访问量过大，请您稍后再试')
        return 'ok'


    _out = F._retry(_boom, tries=3, waits=(0, 1, 2), rate_waits=(0, 12, 24),
                    on_log=_logged.append)
finally:
    F.time.sleep = _orig_sleep
ok(_out == 'ok', '限流两次后第三次成功')
ok(_slept == [12, 24], f'429 走 rate_waits（真等），不是秒级连发: {_slept}')
ok(len(_logged) == 2 and '429' in _logged[0], f'每次失败都留日志: {_logged[0][:40]}')
_slept.clear()
_n['i'] = 0
F.time.sleep = lambda s: _slept.append(s)
try:
    _n2 = {'i': 0}

    def _boom2():
        _n2['i'] += 1
        if _n2['i'] < 3:
            raise TimeoutError()
        return 'ok'

    F._retry(_boom2, tries=3, waits=(0, 1, 2), rate_waits=(0, 12))
finally:
    F.time.sleep = _orig_sleep
ok(_slept == [1, 2], f'非限流还是短等待（别把超时也睡成 12 秒）: {_slept}')

# 7.3 worker 线程不许调 on_log —— 用"线程 id 不对就炸"模拟 Streamlit 的 NoSessionContext
import threading as _th
_main = _th.current_thread()
_log_threads, _page_calls = [], []


def _thread_hostile(page, course, no, title='', on_log=None, subject='math',
                    mode='small', model=None, rate_waits=None, **kw):
    """替身：模拟 worker 线程里调日志（真 Streamlit 这里会抛 NoSessionContext）。"""
    if on_log:
        on_log(f'  第 {no} 页模拟一次失败日志')
    if mode == 'small' and int(no) == 2:
        raise TimeoutError()
    return {'no': int(no), 'role': 'content', 'belongs': 'this', 'topic': 't',
            'examples': [], 'points': [], 'handwriting': False, 'page_text': ''}


def _main_only_log(s):
    _log_threads.append(_th.current_thread())


F.profile_one = _thread_hostile
try:
    pg = [{'no': i, 'img': b'x', 'img_small': b'y', 'text': ''} for i in (1, 2, 3)]
    out4 = F.profile_pages(pg, '课', title='t', workers=3, on_log=_main_only_log)
finally:
    F.profile_one = _real_po
ok(len(out4['pages']) == 3 and not out4['failed'],
   f'并发里 worker 打日志不再拖死页面（{len(out4["pages"])} 页成功 / {len(out4["failed"])} 失败）')
ok(_log_threads and all(t is _main for t in _log_threads),
   f'所有 on_log 都在脚本线程里执行（worker 只写缓冲）: {len(_log_threads)} 条')

# 7.4 主并发的限流退避要比补跑短 —— 否则 41 页各睡 20 秒
_src_f = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'notes_forge.py'),
              encoding='utf-8').read()
ok('rate_waits=(0, 4, 8)' in _src_f and 'rate_waits=(0, 12, 24)' in _src_f,
   '主并发/补跑各用一档退避（4/8 秒 vs 12/24 秒）')
ok("STUDY_RESCUE_BUDGET', '420'" in _src_f, '补跑预算默认 420 秒（240 秒实测只够补 8 页）')

# ---------------------------------------------------------------
# 7.5 2026-11-11 晚：用户报「卡死了」→ 慢必须看得见、能停、能续
# ---------------------------------------------------------------
import pymupdf as _fitz
import tempfile as _tf
import pathlib as _pl
import time as _tm


def _mk_pdf(n):
    d = _fitz.open()
    for i in range(n):
        p = d.new_page()
        p.insert_text((72, 72), f'page {i + 1}')
    b = d.tobytes()
    d.close()
    return b


_pdf3 = _mk_pdf(3)
ok(F.pdf_pages(_pdf3) == 3, f'只数页数不渲染（3 页 PDF → {F.pdf_pages(_pdf3)} 页）')
_i3 = F.pdf_info(_pdf3)
ok(_i3['pages'] == 3 and _i3['fast_min'] > 0 and _i3['slow_min'] >= _i3['fast_min'],
   f"pdf_info 给得出页数与预计时长：{_i3['pages']} 页 / "
   f"{_i3['fast_min']}~{_i3['slow_min']} 分钟（{_i3['workers']} 并发）")
ok(F.estimate_minutes(60)[0] > F.estimate_minutes(41)[0],
   '页数越多预计时间越长（60 页 > 41 页），不写死一个数')

# 逐页增量缓存：第二次跑不该再调模型
_cd = _pl.Path(_tf.mkdtemp(prefix='pgcache_')) / 'pages'
_calls2 = []


def _fake_ok(page, course, no, title='', on_log=None, subject='math',
             mode='small', model=None, rate_waits=None):
    _calls2.append(int(no))
    return {'no': int(no), 'role': 'content', 'belongs': 'this', 'topic': 't',
            'examples': [], 'points': ['a'], 'handwriting': False, 'page_text': ''}


_orig_one = F.profile_one
os.environ['STUDY_VISION_GAP'] = '0'
F.profile_one = _fake_ok
try:
    _pages3 = [{'no': i + 1, 'text': '', 'img': b'x', 'img_small': b'x'} for i in range(3)]
    r1 = F.profile_pages(_pages3, '数学', workers=1, cache_dir=_cd)
    n_first = len(_calls2)
    r2 = F.profile_pages(_pages3, '数学', workers=1, cache_dir=_cd)
    n_second = len(_calls2) - n_first
    ok(len(r1['pages']) == 3 and not r1['failed'], f'首次跑 3 页成功（调用 {n_first} 次）')
    ok(n_second == 0 and len(r2['pages']) == 3,
       f'第二次直接复用逐页缓存，0 次调用（省的是真钱）：调用 {n_second} 次')
finally:
    F.profile_one = _orig_one

# 总时间上限：到点主动停，并明确列出没处理的页
_cd2 = _pl.Path(_tf.mkdtemp(prefix='pgcache2_')) / 'pages'
_calls3 = []


def _slow(page, course, no, title='', on_log=None, subject='math',
          mode='small', model=None, rate_waits=None):
    _tm.sleep(0.25)
    _calls3.append(int(no))
    return {'no': int(no), 'role': 'content', 'belongs': 'this', 'topic': 't',
            'examples': [], 'points': [], 'handwriting': False, 'page_text': ''}


F.profile_one = _slow
try:
    _pages5 = [{'no': i + 1, 'text': '', 'img': b'x', 'img_small': b'x'} for i in range(5)]
    r3 = F.profile_pages(_pages5, '数学', workers=1, cache_dir=_cd2, budget=0.4)
    _bud = [f for f in r3['failed'] if '超过总时间上限' in f['error']]
    ok(len(r3['pages']) >= 1 and _bud,
       f'总时间上限到点主动停：认出 {len(r3["pages"])} 页，{len(_bud)} 页标「超过总时间上限」')
    ok(0 < len(r3['pages']) < 5,
       f'不是全跑完才停（{len(r3["pages"])}/5），说明上限真的生效')
finally:
    F.profile_one = _orig_one

_src_app = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.py'),
                encoding='utf-8').read()
ok('notes.pdf_info(' in _src_app and '预计还需' in _src_app,
   '界面上传即显示页数/预计时长，进度条带「预计还需」')
ok("STUDY_TOTAL_BUDGET', '1500'" in _src_f, '总时间上限默认 1500 秒（可配，0=不限）')
ok("timeout=70" in _src_f, '单页超时 70 秒（90 秒 × 3 次会让页面静默 4.5 分钟）')

print(f'\nALL NOTES CHECKS OK ({PASS} 项)')
