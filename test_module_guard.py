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
OLD_SCAN_COMMIT = '024d632'     # scan v2: 有旋转/crop_from, 但还没有 box_ratio
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
    for f in ('app.py', 'db.py', 'llm.py', 'ocr.py', 'scan.py', 'web_search.py',
              'backup.py', 'diagram.py', 'quiz.py'):
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


def old_scan_text():
    return subprocess.run(['git', 'show', OLD_SCAN_COMMIT + ':scan.py'], cwd=HERE,
                          capture_output=True, text=True, encoding='utf-8').stdout


def run_case(d, preload_old, mod='quiz', probe_attr='phase_options', nav='', quiet=False):
    """preload_old=True 表示先 import 旧模块占住 sys.modules, 再把磁盘换成新版
    nav: 跑完首页后再切到哪个页面(旧模块提示只在该页渲染, 不导航就收不到)"""
    code = r'''
import os, sys, json
d = sys.argv[1]; preload = sys.argv[2] == '1'
mod = sys.argv[3]; attr = sys.argv[4]; nav = sys.argv[5]
sys.path.insert(0, d)
os.chdir(d)
if preload:
    m = __import__(mod)
    before = hasattr(m, attr)
    open(os.path.join(d, mod + '.py'), 'w', encoding='utf-8').write(
        open(os.path.join(d, 'new_' + mod + '.py'), encoding='utf-8').read())
from streamlit.testing.v1 import AppTest
at = AppTest.from_file(os.path.join(d, 'app.py'), default_timeout=90)
at.run()
if nav:
    # 画框区要先有"已上传的照片"才会走到自检那一段, 造一张占位图
    os.makedirs(os.path.join(d, 'uploads'), exist_ok=True)
    from PIL import Image
    Image.new('RGB', (600, 800), 'white').save(os.path.join(d, 'uploads', '_probe.jpg'))
    at.session_state['img_rel'] = '_probe.jpg'
    for r in at.sidebar.radio:
        hit = [o for o in list(getattr(r, 'options', []) or []) if nav in str(o)]
        if hit:
            r.set_value(hit[0]); at.run(); break
out = {'exc': [(e.type, str(e.value)[:150]) for e in at.exception],
       'err': [e.value[:400] for e in at.error],
       'warn': [w.value[:400] for w in at.warning],
       'sub': [s.value for s in at.subheader][:4]}
if preload:
    # importlib.reload 是就地更新同一个对象, 所以这里拿到的还是原来那个
    m2 = __import__(mod)
    out['before'] = before
    out['after'] = hasattr(m2, attr)
print('@@' + json.dumps(out, ensure_ascii=False))
'''
    r = subprocess.run([PY, '-c', code, d, '1' if preload_old else '0', mod, probe_attr, nav],
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

print('=== E. scan 磁盘旧 + 内存旧(2026-10-28 线上真实翻车现场, 不能崩成红 traceback) ===')
d = make_dir('e')
with open(os.path.join(d, 'scan.py'), 'w', encoding='utf-8') as f:
    f.write(old_scan_text())
r = run_case(d, preload_old=False, mod='scan', probe_attr='box_ratio', nav='拍照录入')
ck(r is not None, 'E 能跑起来')
if r:
    ck(not r['exc'], 'E 没有抛异常(不是整页 AttributeError)', r['exc'])
    msg = ''.join(r['err'] + r['warn'])
    ck('旧模块' in msg, 'E 提示里点名"旧模块"', msg[:100])
    ck('Reboot' in msg, 'E 提示里给了 Reboot 办法', msg[:100])

print('=== F. scan 磁盘新 + 内存旧(线上最常见, 要能自愈) ===')
d = make_dir('f')
with open(os.path.join(d, 'scan.py'), 'w', encoding='utf-8') as f:
    f.write(old_scan_text())
shutil.copy(os.path.join(HERE, 'scan.py'), os.path.join(d, 'new_scan.py'))
r = run_case(d, preload_old=True, mod='scan', probe_attr='box_ratio')
ck(r is not None, 'F 能跑起来')
if r:
    ck(r.get('before') is False, 'F 起点确实是旧模块(没有 box_ratio)')
    ck(r.get('after') is True, 'F 自动 reload 后拿到新模块')
    ck(not r['exc'], 'F 没有抛异常', r['exc'])
    ck(not r['err'] and not r['warn'], 'F 没有报警(自愈成功, 用户无感)',
       (r['err'] + r['warn'])[:2])

print('=== G. 结构断言: 名单覆盖 + 版本要求同步(这条是防复发的核心) ===')
src = io.open(os.path.join(HERE, 'app.py'), encoding='utf-8').read()


def _top_list(name):
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return [e.value for e in node.value.elts]
    return None


def _top_int(name):
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            try:
                return ast.literal_eval(node.value)
            except Exception:
                return None
    return None


import re
import scan as _scan
import llm as _llm

_scan_need = _top_list('_SCAN_NEED')
ck(_scan_need is not None, '找得到 _SCAN_NEED 定义')
if _scan_need:
    used = {n for n in re.findall(r'\bscan\.([A-Za-z_][A-Za-z0-9_]*)', src)
            if n not in ('py', 'MODULE_VER')}
    miss = sorted(used - set(_scan_need))
    ck(not miss, 'app.py 里每个 scan.xxx 调用都在自检名单里', miss)
    ck('box_ratio' in _scan_need, 'box_ratio 在名单里(2026-10-28 漏的就是它)')
    ck(all(hasattr(_scan, n) for n in _scan_need), '名单里每个名字 scan 都真有',
       [n for n in _scan_need if not hasattr(_scan, n)])

# 版本号必须两边同步: 加函数只 bump 模块、忘了改 app.py 的要求值 → 旧模块又能混过去
ck(_top_int('_SCAN_WANT') == getattr(_scan, 'MODULE_VER', None),
   'app.py 的 _SCAN_WANT 等于 scan.MODULE_VER',
   '%s vs %s' % (_top_int('_SCAN_WANT'), getattr(_scan, 'MODULE_VER', None)))
ck(_top_int('_LLM_WANT') == getattr(_llm, 'MODULE_VER', None),
   'app.py 的 _LLM_WANT 等于 llm.MODULE_VER',
   '%s vs %s' % (_top_int('_LLM_WANT'), getattr(_llm, 'MODULE_VER', None)))

print('\n===== 汇总: 通过 %d / 失败 %d =====' % (len(PASS), len(FAIL)))
for f in FAIL:
    print('  ✗', f)
sys.exit(1 if FAIL else 0)
