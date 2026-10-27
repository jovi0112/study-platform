# -*- coding: utf-8 -*-
"""实测: 每日测试的错题是否真的进错题本, 讲解是否完整。全在临时库跑。"""
import os, sys, tempfile, importlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
tmp = tempfile.mkdtemp(prefix='mflow_')
import db
db.DB_PATH = os.path.join(tmp, 'study.db')
import quiz
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

SUBJ = '数学'
before = db.list_mistakes(limit=500)
print(f'== 1. 出卷 (原有错题 {len(before)} 条) ==')
r = quiz.pick_questions(SUBJ, n=10, use_ai=False)
check('出卷成功', r['ok'], r.get('msg'))
qs = r['questions']
paper = quiz.create_paper(SUBJ, qs)
pid = paper['id'] if isinstance(paper, dict) else paper
print(f'   卷子 id={pid}, {len(qs)} 题')

print('== 2. 全部故意答错后判卷 ==')
items = quiz.get_paper(pid)['items']
for it in items:
    quiz.save_answer(it['id'], '这是我的错误答案xyz')
rep = quiz.grade_paper(pid, SUBJ)
print(f"   判卷: 对 {rep.get('right')} 错 {rep.get('wrong')} 共 {rep.get('total')}")
check('全部判错', rep.get('right') == 0 and rep.get('wrong') == len(items), rep)

print('== 3. 错题是否进错题本 ==')
after = db.list_mistakes(limit=500)
new = [m for m in after if m.get('source') == 'quiz']
print(f'   错题本 {len(before)} -> {len(after)} 条, source=quiz 共 {len(new)} 条')
check('错题条数与判错数一致', len(new) == len(items), f'{len(new)} vs {len(items)}')

print('== 4. 每条错题的讲解内容核查 ==')
no_explain = [m for m in new if not (m.get('explanation') or '').strip()]
check('每条都有讲解', not no_explain, f'{len(no_explain)} 条缺讲解')
# 门槛 20 字而不是 40: 题库自带的精炼讲解可能只有 30 多字(如"同类项要求指数相同, 所以
# n=2, m=3, m+n=5"), 内容其实讲清楚了。这里只拦真正的空话/占位符。
short = [m for m in new if len((m.get('explanation') or '')) < 20]
check('讲解不是敷衍(>=20字)', not short,
      f'{len(short)} 条过短 -> ' + str([m.get("explanation") for m in short[:1]]))
no_corr = [m for m in new if not (m.get('correct_answer') or '').strip()]
check('带正确答案', not no_corr, f'{len(no_corr)} 条缺')

print('== 5. 重复判卷/重复推送不能让错题本变臃肿 ==')
_n1 = len([m for m in db.list_mistakes(limit=500) if m.get('source') == 'quiz'])
quiz.push_wrong_to_mistakes(pid, SUBJ)      # 判卷内部已经推过, 这里再推
quiz.push_wrong_to_mistakes(pid, SUBJ)
_n2 = len([m for m in db.list_mistakes(limit=500) if m.get('source') == 'quiz'])
print(f'   推之前 {_n1} 条 -> 又推两次 {_n2} 条')
check('重复推送不产生重复条目', _n1 == _n2, f'{_n1} vs {_n2}')
check('去重靠 uid', all(m.get('uid') for m in
                        [x for x in db.list_mistakes(limit=500) if x.get('source') == 'quiz'][:5]))

if new:
    m = new[0]
    print('\n--- 错题本里的实际一条 ---')
    for k in ['subject', 'title', 'question', 'answer', 'correct_answer', 'wrong_reason', 'source']:
        v = str(m.get(k) or '')[:70].replace('\n', ' ')
        print(f'   {k:15s}= {v}')
    print(f'   explanation    = {str(m.get("explanation"))[:120]}...')

print(f'\n汇总: {ok} 通过 / {fail} 失败')
