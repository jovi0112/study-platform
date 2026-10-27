# -*- coding: utf-8 -*-
"""quiz.py 本地功能冒烟. 用临时库, 不碰孩子的真实数据, 不联网."""
import os, sys, tempfile, datetime
sys.path.insert(0, os.path.dirname(__file__))

tmpdir = tempfile.mkdtemp(prefix='quiztest_')
import db
db.DB_PATH = os.path.join(tmpdir, 'study.db')   # 必须在 import quiz 之前

import quiz

fails = []
def check(name, cond, extra=''):
    print(('  [OK] ' if cond else '  [FAIL] ') + name + (('  -> ' + str(extra)) if extra else ''))
    if not cond:
        fails.append(name)

quiz.init_quiz_db()
db.init_db()

print('== 1. 教学日历按日期定位 ==')
today = datetime.date(2026, 10, 27)
for subj, expect in [('数学', '第二章 整式的加减'), ('语文', '第三单元 学习生活'), ('英语', 'Unit 2 School Life')]:
    ph = quiz.current_phase(subj, today)
    got = (ph or {}).get('label', '')
    check(f'{subj} 今日进度 = {got}', expect.split()[0] in got, f'期望包含 {expect}')
check('进度说明非空', bool(quiz.progress_hint('数学')), quiz.progress_hint('数学'))
ph = quiz.current_phase('数学', datetime.date(2027, 7, 1))
check('超出日历范围有标记', ph.get('out_of_range') == 'after')
quiz.set_offset('数学', -14)   # 10-27 提前14天 = 10-13, 应落回有理数段
check('偏移生效(提前14天回到有理数)', '有理数' in (quiz.current_phase('数学', today) or {}).get('label', ''),
      quiz.current_phase('数学', today).get('label'))
quiz.set_offset('数学', 0)

print('== 2. 组卷(不联网, 只用题库) ==')
papers = {}
for subj in quiz.SUBJECTS:
    r = quiz.pick_questions(subj, n=8, on=today, use_ai=False)
    qs = r['questions']
    check(f'{subj} 选出 {len(qs)} 题', len(qs) >= 5, r['msg'])
    scope_topics = r['phase']['topics'] if r['phase'] else []
    in_scope = sum(1 for q in qs if q['topic'] in scope_topics or q['chapter'] in scope_topics)
    check(f'{subj} 多数题在当前进度内 ({in_scope}/{len(qs)})', in_scope >= len(qs) * 0.6)
    papers[subj] = qs

print('== 2b. 题库不足时 AI 补充(真实调用) ==')
import llm as _llm
if _llm.is_llm_enabled():
    for subj in ['英语', '语文']:
        r = quiz.pick_questions(subj, n=8, on=today, use_ai=True)
        qs = r['questions']
        check(f'{subj} 用 AI 补齐到 {len(qs)} 题', len(qs) >= 8, r['msg'])
        print(f'   {subj}: {r["msg"]} | 来源 {[q["source"] for q in qs]}')
        papers[subj] = qs
else:
    print('   [跳过] AI 未配置')

print('== 3. 判卷(客观题本地硬判) ==')
for subj in quiz.SUBJECTS:
    qs = [q for q in papers[subj] if q['qtype'] != 'answer']
    if not qs:
        continue
    pid = quiz.create_paper(subj, qs, '测试进度段')
    items = quiz.get_paper(pid)['items']
    expect_right = 0
    for i, it in enumerate(items):
        if i % 2 == 0:
            quiz.save_answer(it['id'], it['answer'])   # 答对
            expect_right += 1
        else:
            quiz.save_answer(it['id'], '这是个错误答案')
    rep = quiz.grade_paper(pid, subj)
    check(f'{subj} 判对 {rep.get("right")}/{rep.get("total")} (期望 {expect_right})',
          rep.get('right') == expect_right, rep)
    check(f'{subj} 分数 = {rep.get("score")}', abs(rep.get('score', 0) - expect_right / len(items) * 100) < 0.6)
    d = quiz.get_paper(pid)
    check(f'{subj} 试卷状态已归档', d['paper']['status'] == 'done')
    # push_wrong_to_mistakes 返回 dict(带 count/pairs), 不是条数本身
    _pw = quiz.push_wrong_to_mistakes(pid, subj)
    check(f'{subj} 错题已流入错题本',
          (_pw.get('count') if isinstance(_pw, dict) else _pw) == len(items) - expect_right)

print('== 4. 判卷容错(最容易冤枉孩子的几种写法) ==')
cases = [
    ({'answer': '-4', 'alt': [], 'options': None}, '－４', True),      # 全角数字
    ({'answer': '-4', 'alt': [], 'options': None}, ' -4 ', True),      # 前后空格
    ({'answer': '±3', 'alt': ['3或-3', '3,-3'], 'options': None}, '3或-3', True),
    ({'answer': '0.5', 'alt': [], 'options': None}, '1/2', True),      # 数值等价
    ({'answer': 'library', 'alt': [], 'options': None}, 'Library', True),  # 英文大小写
    ({'answer': 'B', 'alt': [], 'options': {'A': '1', 'B': '2'}}, 'b', True),   # 选项小写
    ({'answer': 'B', 'alt': [], 'options': {'A': '1', 'B': '2'}}, '2', True),   # 答选项内容
    ({'answer': '5', 'alt': [], 'options': None}, '6', False),         # 真错
    ({'answer': '5', 'alt': [], 'options': None}, '', False),          # 未作答不算对
    ({'answer': 'x²+5x-1', 'alt': ['x^2+5x-1'], 'options': None}, 'x^2+5x-1', True),
]
for q, ua, expect in cases:
    got = quiz.is_correct(q, ua)
    check(f'答「{ua}」对答案「{q["answer"]}」-> {got}', got == expect, f'期望 {expect}')

print('== 5. 掌握度随结果变化 ==')
m0 = quiz.topic_level('数学', '合并同类项')
quiz.update_mastery('数学', '合并同类项', True)
m1 = quiz.topic_level('数学', '合并同类项')
quiz.update_mastery('数学', '合并同类项', False)
quiz.update_mastery('数学', '合并同类项', False)
m2 = quiz.topic_level('数学', '合并同类项')
check(f'答对后难度升 {m0}->{m1}', m1 == min(5, m0 + 1))
check(f'连错后难度降 {m1}->{m2}', m2 == m1 - 2)
for _ in range(8):
    quiz.update_mastery('数学', '合并同类项', False)
check('难度不低于1', quiz.topic_level('数学', '合并同类项') == 1)
for _ in range(10):
    quiz.update_mastery('数学', '合并同类项', True)
check('难度不超过5', quiz.topic_level('数学', '合并同类项') == 5)

print('== 6. AI 出题 + 自动验算(真实调用 DeepSeek) ==')
import llm
print('  AI 是否启用:', llm.is_llm_enabled(), '| provider:', llm.config_info().get('provider_display'))
if not llm.is_llm_enabled():
    print('  [跳过] 未配置 AI, 改为检查降级文案')
    r = quiz.ai_generate_questions('数学', '有理数乘法', '第一章', n=2)
    check('出题失败时给出原因', (not r['ok']) and bool(r['msg']), r.get('msg'))
else:
    r = quiz.ai_generate_questions('数学', '有理数乘法', '第一章 有理数', n=3, difficulty=3)
    check(f'出题成功: {r.get("msg")}', r['ok'], r.get('msg'))
    for q in r.get('questions', [])[:3]:
        print(f'    - [{q["qtype"]}] {q["stem"][:40]}... 答案={q["answer"]} 验算通过={q["verified"]}')
        check(f'  题「{q["stem"][:16]}」答案非空', bool(q['answer']))
        if q['qtype'] == 'choice':
            check(f'  选项自洽', str(q['answer']).strip()[:1].upper() in (q['options'] or {}).keys(),
                  f'答案 {q["answer"]} / 选项 {list((q["options"] or {}).keys())}')
    # 主观题 AI 评分
    g = quiz.ai_grade('语文', '《秋天的怀念》结尾反复出现"好好儿活", 结合全文说说它有什么含义。',
                      '要点: 母亲希望儿子坚强活下去; 承载母亲的爱与期望; 结尾表明"我"走出绝望',
                      '母亲希望我坚强地活下去, 不要放弃。')
    check(f'主观题评分 = {g.get("score")} 分', g.get('ok') and g.get('score') is not None)
    print('    评语:', str(g.get('comment'))[:120])
    g2 = quiz.ai_grade('语文', '同上题目', '要点同上', '')
    check('未作答给 0 分', g2.get('ok') and g2.get('score') == 0)

print('== 7. 验算器(只放行数字算式, 挡住危险输入) ==')
check('算式 (-7)+3 -> -4', quiz._eval_expr('(-7)+3') == -4)
check('分数 Fraction(1,2) 可用', abs(float(quiz._eval_expr('Fraction(1,2)')) - 0.5) < 1e-9)
check('挡住 __import__ 等危险输入', quiz._eval_expr('__import__("os").system("echo hi")') is None)

print('== 8. 错题归纳 ==')
w = quiz.wrong_summary('数学', days=30)
check('按知识点聚合错题', isinstance(w, list), w[:3])

print()
print('失败项:', len(fails))
for f in fails:
    print('  -', f)
print('临时库:', tmpdir)
