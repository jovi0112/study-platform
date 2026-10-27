# -*- coding: utf-8 -*-
"""
端到端验证: 错题 -> 错题本 -> 讲解图
全在临时库跑, 不动孩子真实数据。
"""
import os, sys, tempfile, importlib, json, time
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
tmp = tempfile.mkdtemp(prefix='dia_')
import db
db.DB_PATH = os.path.join(tmp, 'study.db')
import quiz, diagram
importlib.reload(quiz)
quiz.DB_PATH = db.DB_PATH
quiz.init_quiz_db()
db.init_db()

ok = fail = 0
def check(name, cond, extra=''):
    global ok, fail
    if cond:
        ok += 1; print(f'  [OK]   {name}')
    else:
        fail += 1; print(f'  [FAIL] {name}  {extra}')

print('== 1. 出卷 + 全答错 + 判卷 ==')
r = quiz.pick_questions('数学', n=6, use_ai=False)
pid = quiz.create_paper('数学', r['questions'])
pid = pid['id'] if isinstance(pid, dict) else pid
items = quiz.get_paper(pid)['items']
for it in items:
    quiz.save_answer(it['id'], '错的')
rep = quiz.grade_paper(pid, '数学')
check('判卷返回 pairs', bool(rep.get('pairs')), rep.keys())
check('pairs 数量=错题数', len(rep.get('pairs') or []) == len(items), len(rep.get('pairs') or []))

print('== 2. 批量画图 ==')
t0 = time.time()
stat = quiz.gen_diagrams_for_paper(pid)
dt = time.time() - t0
print(f'   {dt:.1f}s  ok={stat["ok"]} failed={stat["failed"]} skipped={stat["skipped"]} errors={stat["errors"][:2]}')
check('每道错题都画出了图', stat['ok'] == len(items), f"{stat['ok']} vs {len(items)}")
check('画图总耗时可控(<60s)', dt < 60, f'{dt:.1f}s')

print('== 3. 第二次调用应全部命中缓存(不再花 AI 时间) ==')
t0 = time.time()
stat2 = quiz.gen_diagrams_for_paper(pid)
dt2 = time.time() - t0
print(f'   {dt2:.2f}s  ok={stat2["ok"]} skipped={stat2["skipped"]}')
check('二次调用很快(<1s)', dt2 < 1.0, f'{dt2:.2f}s')
check('二次调用不重复生成', stat2['ok'] == 0)

print('== 4. 同步到错题本 ==')
n = quiz.sync_diagrams_to_mistakes(rep['pairs'])
check('同步条数=错题数', n == len(items), f'{n} vs {len(items)}')
ms = db.list_mistakes(limit=200)
withdia = [m for m in ms if m.get('diagram_json')]
check('错题本记录带上了图', len(withdia) == len(items), f'{len(withdia)}/{len(ms)}')

print('== 5. 图片本身是否可用 ==')
bad_xml, no_text, tiny = [], [], []
for m in withdia:
    svg = quiz.render_mistake_diagram(m)
    if not svg:
        bad_xml.append(m['id']); continue
    try:
        root = ET.fromstring(svg)
    except Exception as e:
        bad_xml.append((m['id'], str(e)[:40])); continue
    texts = [e for e in root.iter('{http://www.w3.org/2000/svg}text')]
    if len(texts) < 4:
        no_text.append((m['id'], len(texts)))
    if len(svg) < 600:
        tiny.append(m['id'])
check('SVG 都是合法 XML', not bad_xml, bad_xml[:3])
check('每张图都有足够文字', not no_text, no_text[:3])
check('没有空图', not tiny, tiny[:3])

print('== 6. 图里到底写了什么(抽一张看) ==')
if withdia:
    svg = quiz.render_mistake_diagram(withdia[0])
    import re
    txts = [re.sub(r'<[^>]+>', '', x) for x in re.findall(r'<text[^>]*>.*?</text>', svg)]
    print('   ', ' | '.join(txts[:12]))

print('== 7. AI 不可用时的降级(仍要出图) ==')
item = quiz.get_paper(pid)['items'][0]
with quiz._conn() as conn:
    conn.execute('UPDATE quiz_items SET diagram_json=NULL WHERE id=?', (item['id'],))
import llm
_orig = llm.is_llm_enabled
llm.is_llm_enabled = lambda: False
try:
    t0 = time.time()
    res = quiz.gen_diagram(item['id'], force=True)
    dt3 = time.time() - t0
    print(f'   降级耗时 {dt3:.2f}s ok={res["ok"]} fallback={res.get("fallback")} err={res["error"][:40]}')
    check('AI 不可用也能出图(降级)', res['ok'] and bool(res['svg']), res.get('error'))
    check('降级说明了原因', bool(res.get('error')) or res.get('fallback') is True, res.get('error'))
    try:
        ET.fromstring(res['svg'])
        check('降级图也是合法 SVG', True)
    except Exception as e:
        check('降级图也是合法 SVG', False, str(e)[:50])
finally:
    llm.is_llm_enabled = _orig

print('== 8. 错题本里单独画图(拍照录入的老错题) ==')
mid = db.add_mistake(subject='数学', question='计算 (-3)² - 2×(-3) 的值',
                     answer='3', correct_answer='15',
                     explanation='先算乘方: (-3)²=9；再算乘法: 2×(-3)=-6；所以 9-(-6)=9+6=15。')
res2 = quiz.gen_diagram_for_mistake(mid)
check('老错题也能画图', res2['ok'] and bool(res2['svg']), res2.get('error'))
check('老错题的图存回库', bool(quiz.render_mistake_diagram(
    [x for x in db.list_mistakes(limit=50) if x['id'] == mid][0])))

print(f'\n汇总: {ok} 通过 / {fail} 失败')
