# -*- coding: utf-8 -*-
"""
界面验证: 出卷后有限时倒计时; 时间到自动收卷; 没做完的进错题本能重做。
用临时库, 不碰真实数据。AI 补充关掉, 流程稳定。
"""
import os
import sys
import tempfile
import datetime
import json

sys.path.insert(0, os.path.dirname(__file__))
tmpdir = tempfile.mkdtemp(prefix='applimit_')

import db
db.DB_PATH = os.path.join(tmpdir, 'study.db')

from streamlit.testing.v1 import AppTest

fails = []


def check(name, cond, extra=''):
    print(('  [OK]   ' if cond else '  [FAIL] ') + name + (('  -> ' + str(extra)) if extra else ''))
    if not cond:
        fails.append(name)


at = AppTest.from_file('app.py', default_timeout=300)
at.run()
check('首屏无异常', not at.exception, [e.value for e in at.exception])

print('== 1. 出卷: 应带限时 ==')
at.checkbox(key='qz_use_ai').uncheck().run()
at.selectbox(key='qz_size').set_value('小测 · 10 题').run()
_b = [x for x in at.button if '刷新' in x.label]
check('有限时选择器', any('限时' in s.label for s in at.selectbox),
      [s.label for s in at.selectbox])
check('出卷按钮在', bool(_b))
_b[0].click().run()
check('出卷无异常', not at.exception, [e.value for e in at.exception])

import quiz
pid = at.session_state['qz_pid']
paper = quiz.get_paper(pid)['paper']
check('卷子写了限时', int(paper.get('limit_sec') or 0) == 900, paper.get('limit_sec'))
check('卷子写了到点时刻', bool(paper.get('deadline')), paper.get('deadline'))
_info = quiz.paper_time_left(pid)
check('剩余时间接近 15 分钟', 880 <= _info['left'] <= 900, _info['left'])

_md = ' '.join((m.value or '') for m in at.markdown)
check('页面显示倒计时', '⏳' in _md or '⏳' in ' '.join(str(x) for x in _md), _md[:120])
check('有交卷按钮(答案即时落库, 不靠表单一次提交)',
      any('交卷' in (b.label or '') for b in at.button),
      [b.label for b in at.button][:6])

print('== 2. 先写一题, 再把时间调到过期 -> 应自动收卷 ==')
_item0 = quiz.get_paper(pid)['items'][0]
quiz.save_answer(_item0['id'], '先写的答案')
with quiz._conn() as c:
    c.execute("UPDATE quiz_papers SET deadline=? WHERE id=?",
              ((datetime.datetime.now() - datetime.timedelta(seconds=3)).isoformat(timespec='seconds'), pid))
at.run()
check('自动收卷无异常', not at.exception, [e.value for e in at.exception])
_p = quiz.get_paper(pid)['paper']
check('卷子状态变成已交', _p['status'] == 'done', _p['status'])
check('结束原因记成 timeout', _p.get('close_reason') == 'timeout', _p.get('close_reason'))
_err = ' '.join(e.value for e in at.error)
check('页面明确告诉孩子时间到自动收卷', '时间到' in _err, _err[:90])

_items = quiz.get_paper(pid)['items']
_blank = [i for i in _items if i.get('unanswered') == 1]
check('空着的题都标成未作答', len(_blank) == len(_items) - 1, f'{len(_blank)}/{len(_items)}')
check('未作答原因写明 timeout', all(i.get('miss_reason') == 'timeout' for i in _blank))

_mist = db.list_mistakes(limit=50)
check('错题全进了错题本', len(_mist) == len(_items) - (1 if quiz.is_correct(
    {'answer': _item0['answer'], 'alt': json.loads(_item0['alt_json'] or '[]'),
     'qtype': _item0['qtype'],
     'options': json.loads(_item0['options_json']) if _item0['options_json'] else None},
    _item0['user_answer']) else 0), len(_mist))
_to = [m for m in _mist if m.get('miss_reason') == 'timeout']
check('错题本里带「时间到未作答」', len(_to) == len(_blank), f'{len(_to)} vs {len(_blank)}')
check('错题本记录带题型(能重做)', all(m.get('qtype') for m in _mist),
      [(m['id'], m.get('qtype')) for m in _mist])

print('== 3. 错题本页面: 有重做入口, 能做对 ==')
_nav = [r for r in at.sidebar.radio]
if _nav:
    _opts = _nav[0].options
    _book = [o for o in _opts if '错题本' in str(o)]
    if _book:
        _nav[0].set_value(_book[0]).run()
check('切到错题本无异常', not at.exception, [e.value for e in at.exception])
_tab_labels = []
for t in at.tabs:
    pass
_rd_btns = [b for b in at.button if '提交这次的答案' in (b.label or '')]
check('每条错题都有「提交这次的答案」按钮', len(_rd_btns) >= len(_mist), len(_rd_btns))

# 找一道题, 填正确答案 -> 应归到已掌握
_m0 = _mist[0]
if _m0.get('qtype') in ('fill', 'choice', 'judge', 'multi'):
    _key = f'rd_{_m0["id"]}'
    _w = [x for x in at.text_input if x.key == _key] + \
         [x for x in at.radio if x.key == _key] + \
         [x for x in at.text_area if x.key == _key]
    if _w:
        _ans = (_m0.get('correct_answer') or '').strip()
        try:
            if hasattr(_w[0], 'options'):
                _hit = [o for o in _w[0].options if str(o).startswith(_ans)]
                _w[0].set_value(_hit[0] if _hit else _w[0].options[1])
            else:
                _w[0].set_value(_ans)
            _btn = [b for b in at.button if b.key == f'rd_sub_{_m0["id"]}']
            if _btn:
                _btn[0].click().run()
            check('重做答对后归到已掌握', db.get_mistake(_m0['id'])['mastered'] == 1,
                  db.get_mistake(_m0['id'])['mastered'])
            check('留下重做记录', len(db.list_redos(_m0['id'])) == 1)
        except Exception as e:
            check('重做交互', False, f'{type(e).__name__}: {e}')
else:
    print('   (第一条是简答题, 跳过自动填答)')

print()
if fails:
    print('失败:', fails)
    sys.exit(1)
print('全部通过')
