# -*- coding: utf-8 -*-
"""
验证「新界面 + 进程里的旧模块」不再整页红崩。

背景: Streamlit 只热重载主脚本 app.py, import 进来的 quiz.py 不重载。
线上开着旧进程时, 新的 app.py 调 quiz.phase_options 会 AttributeError, 用户只看到英文 traceback。
修法: app.py 启动时探一遍接口 -> 缺了自动 importlib.reload -> 还缺才说人话停下。

三个场景(每个都跑在独立子进程里, 免得 sys.modules 互相污染):
  A. 磁盘旧 + 内存旧  -> 不能崩, 要有人话提示(并给出重启办法)
  B. 磁盘新 + 内存旧  -> 要自动 reload 救回来, 页面正常
  C. 磁盘新 + 内存新  -> 正常
再顺带校验: app.py 里用到的每个 quiz.xxx 都在自检名单里(以后新增调用不会漏检)。
"""
import ast
import io
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
OLD_COMMIT = '172fba7'          # 手动指定章节/讲解图都还没有的那个版本
PASS, FAIL = [], []


def ck(cond, name, extra=''):
    (PASS if cond else FAIL).append(name + ((' | ' + str(extra)) if extra else ''))
    print(('  OK  ' if cond else ' FAIL ') + name + ((' | ' + str(extra)) if extra else ''))


def make_dir(tag):
    d = os.path.join(tempfile.gettempdir(), 'guard_' + tag)
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(os.path.join(d, 'data'))
    os.makedirs(os.path.join(d, '.streamlit'))
    for f in ('app.py', 'db.py', 'llm.py', 'ocr.py', 'web_search.py', 'backup.py', 'diagram.py'):
        shutil.copy(os.path.join(HERE, f), d)
    for f in os.listdir(os.path.join(HERE, 'data')):
        if f.endswith('.json'):
            shutil.copy(os.path.join(HERE, 'data', f), os.path.join(d, 'data'))
    sec = os.path.join(HERE, '.streamlit', 'secrets.toml')
    if os.path.exists(sec):
        shutil.copy(sec, os.path.join(d, '.streamlit'))
    return d


def old_quiz_text():
    return subprocess.run(['git', 'show', OLD_COMMIT + ':quiz.py'], cwd=HERE,
                          capture_output=True, text=True, encoding='utf-8').stdout


def run_case(d, preload_old, quiet=False):
    """preload_old=True 表示先 import 旧 quiz 占住 sys.modules, 再把磁盘换成新版"""
    code = r'''
import os, sys, json
d = sys.argv[1]; preload = sys.argv[2] == '1'
sys.path.insert(0, d)
os.chdir(d)
if preload:
    import quiz as _q
    before = hasattr(_q, 'phase_options')
    open(os.path.join(d, 'quiz.py'), 'w', encoding='utf-8').write(
        open(os.path.join(d, 'new_quiz.py'), encoding='utf-8').read())
from streamlit.testing.v1 import AppTest
at = AppTest.from_file(os.path.join(d, 'app.py'), default_timeout=90)
at.run()
out = {'exc': [(e.type, str(e.value)[:150]) for e in at.exception],
       'err': [e.value[:120] for e in at.error],
       'sub': [s.value for s in at.subheader][:4]}
if preload:
    import quiz as _q2
    out['before'] = before
    out['after'] = hasattr(_q2, 'phase_options')
print('@@' + json.dumps(out, ensure_ascii=False))
'''
    r = subprocess.run([PY, '-c', code, d, '1' if preload_old else '0'],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    for ln in (r.stdout or '').splitlines():
        if ln.startswith('@@'):
            import json
            return json.loads(ln[2:])
    if not quiet:
        print('   子进程输出异常:', (r.stdout or '')[-300:], (r.stderr or '')[-300:])
    return None


print('=== A. 磁盘旧 + 内存旧(最坏情况, 不能崩成英文 traceback) ===')
d = make_dir('a')
with open(os.path.join(d, 'quiz.py'), 'w', encoding='utf-8') as f:
    f.write(old_quiz_text())
r = run_case(d, preload_old=False)
ck(r is not None, 'A 能跑起来')
if r:
    ck(not r['exc'], 'A 没有抛异常(不是红 traceback)', r['exc'])
    ck(bool(r['err']) and '重启' in ''.join(r['err']), 'A 给出人话提示(告诉怎么重启)',
       (r['err'][0][:60] if r['err'] else '无提示'))

print('=== B. 磁盘新 + 内存旧(线上最常见, 要能自愈) ===')
d = make_dir('b')
with open(os.path.join(d, 'quiz.py'), 'w', encoding='utf-8') as f:
    f.write(old_quiz_text())
shutil.copy(os.path.join(HERE, 'quiz.py'), os.path.join(d, 'new_quiz.py'))
r = run_case(d, preload_old=True)
ck(r is not None, 'B 能跑起来')
if r:
    ck(r.get('before') is False, 'B 起点确实是旧模块(没有 phase_options)')
    ck(r.get('after') is True, 'B 自动 reload 后拿到新模块')
    ck(not r['exc'], 'B 没有抛异常', r['exc'])
    ck(not r['err'], 'B 没有报错提示(自愈成功)', r['err'])
    ck(any('今天该练到哪' in s for s in r['sub']), 'B 页面正常渲染到进度卡', r['sub'])

print('=== C. 磁盘新 + 内存新(正常情况) ===')
d = make_dir('c')
shutil.copy(os.path.join(HERE, 'quiz.py'), os.path.join(d, 'quiz.py'))
r = run_case(d, preload_old=False)
ck(r is not None and not r['exc'], 'C 正常无异常', (r or {}).get('exc'))
ck(r is not None and not r['err'], 'C 无误报', (r or {}).get('err'))

print('=== D. 自检名单要覆盖 app.py 里所有 quiz.xxx 调用 ===')
src = io.open(os.path.join(HERE, 'app.py'), encoding='utf-8').read()
tree = ast.parse(src)
need = None
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == '_QUIZ_NEED' for t in node.targets):
        need = [e.value for e in node.value.elts]
        break
ck(need is not None, '找得到 _QUIZ_NEED 定义')
if need:
    import re
    used = {n for n in re.findall(r'\bquiz\.([A-Za-z_][A-Za-z0-9_]*)', src) if n != 'py'}
    miss = sorted(used - set(need))
    ck(not miss, '没有漏检的 quiz 调用', miss)
    print('     名单 %d 项, app.py 实际调用 %d 项' % (len(need), len(used)))

print('\n===== 汇总: 通过 %d / 失败 %d =====' % (len(PASS), len(FAIL)))
for f in FAIL:
    print('  ✗', f)
sys.exit(1 if FAIL else 0)
