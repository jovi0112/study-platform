# -*- coding: utf-8 -*-
"""
界面验证: 交卷后判卷报告里有没有讲解图、错题本 tab 里有没有图。
用临时库, 不碰真实数据; 会真调 AI(和线上一致)。
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
tmpdir = tempfile.mkdtemp(prefix='appdia_')

import db
db.DB_PATH = os.path.join(tmpdir, 'study.db')      # 必须在 AppTest.run 之前

from streamlit.testing.v1 import AppTest

fails = []
def check(name, cond, extra=''):
    print(('  [OK]   ' if cond else '  [FAIL] ') + name + (('  -> ' + str(extra)) if extra else ''))
    if not cond:
        fails.append(name)

at = AppTest.from_file('app.py', default_timeout=300)
at.run()
check('首屏无异常', (not at.exception), [e.value for e in at.exception])

print('== 出卷(关掉 AI 补充, 让流程稳定) ==')
at.checkbox(key='qz_use_ai').uncheck().run()
b = [x for x in at.button if '刷新' in x.label]
check('有出卷按钮', bool(b))
b[0].click().run()
check('出卷无异常', (not at.exception), [e.value for e in at.exception])

print('== 全部填错误答案后交卷 ==')
txt = [x for x in at.text_input if x.key and x.key.startswith('qz_q_')]
rad = [x for x in at.radio if x.key and x.key.startswith('qz_q_')]
print(f'   填空/解答框 {len(txt)} 个, 单选 {len(rad)} 个')
for t in txt:
    t.set_value('错误答案')
for r in rad:
    if len(r.options) > 1:
        r.set_value(r.options[1])
at.run()
sub = [x for x in at.button if '交卷' in x.label]
check('有交卷按钮', bool(sub))
sub[0].click().run()
check('判卷无异常', (not at.exception), [e.value for e in at.exception])
print('   小标题:', [s.value for s in at.subheader][:3])
print('   提示:', [w.value[:60] for w in at.warning][:2], [s.value[:50] for s in at.success][:2])

import quiz
papers = quiz.list_papers(limit=1)
pid = papers[0]['id'] if papers else 0
wrong = [i for i in quiz.get_paper(pid)['items'] if i.get('correct') != 1] if pid else []
withdia = [i for i in wrong if i.get('diagram_json')]
print(f'   卷 {pid}: 错 {len(wrong)} 道, 其中有图 {len(withdia)} 道')
check('判卷后错题都有了讲解图', len(withdia) == len(wrong) and len(wrong) > 0,
      f'{len(withdia)}/{len(wrong)}')

ms = db.list_mistakes(limit=100)
check('错题都进了错题本', len(ms) == len(wrong), f'{len(ms)} vs {len(wrong)}')
check('错题本的记录带图', all(m.get('diagram_json') for m in ms), [m['id'] for m in ms if not m.get('diagram_json')])

print('== 切到错题本页面 ==')
nav = [r for r in at.sidebar.radio]
check('有侧边导航', bool(nav), [r.label for r in nav])
if nav:
    opts = nav[0].options
    book = [o for o in opts if '错题本' in str(o)]
    check('导航里有错题本', bool(book), opts)
    if book:
        nav[0].set_value(book[0]).run()
        check('错题本页无异常', (not at.exception), [e.value for e in at.exception])
        # tabs 数量: 每条错题 7 个
        n_items = len(ms)
        print(f'   tab 总数 {len(at.tabs)} (错题 {n_items} 条 x 7)')
        check('每条错题有 7 个 tab', len(at.tabs) >= n_items * 7, len(at.tabs))
        # st.tabs 在 AppTest 里读标签文字不方便, 用区块标题兜底
        caps = [c.value for c in at.caption if '解法主线' in str(c.value)]
        print('   讲解图说明条数:', len(caps))

print()
print('FAILS:', fails if fails else '无')
sys.exit(1 if fails else 0)
