# -*- coding: utf-8 -*-
"""验证「题型单一 / 题量太少」这两个问题的修复。用临时库, 不碰真实数据。"""
import os, sys, tempfile, collections
sys.path.insert(0, os.path.dirname(__file__))

tmpdir = tempfile.mkdtemp(prefix='quizv2_')
import db
db.DB_PATH = os.path.join(tmpdir, 'study.db')
import quiz

fails = []
def check(name, cond, extra=''):
    print(('  [OK] ' if cond else '  [FAIL] ') + name + (('  -> ' + str(extra)) if extra else ''))
    if not cond:
        fails.append(name)

quiz.init_quiz_db(); db.init_db()

print('== 1. 题库规模 ==')
for s in quiz.SUBJECTS:
    st = quiz.bank_stats(s)
    print(f'  {s}: 共 {st["total"]} 道, {len(st["topics"])} 个知识点')
check('数学题库 >= 90 道', quiz.bank_stats('数学')['total'] >= 90, quiz.bank_stats('数学')['total'])

print('== 2. 一张卷子的题型多样性(纯题库, 不调 AI) ==')
for s in quiz.SUBJECTS:
    r = quiz.pick_questions(s, n=15, use_ai=False)
    qs = r['questions']
    types = collections.Counter(q['qtype'] for q in qs)
    print(f'  {s}: {len(qs)} 题 · {dict(types)}')
    print(f'     msg: {r["msg"]}')
    check(f'{s} 出满或说明缺口', len(qs) > 0, len(qs))
    if s == '数学':
        check('数学单张卷子题型 >= 3 种', len(types) >= 3, dict(types))

print('== 3. 连续两套的重复率(模拟真实流程: 出卷会写入 quiz_items) ==')
def make_paper(subject, n=15):
    r = quiz.pick_questions(subject, n=n, use_ai=False)
    quiz.create_paper(subject, r['questions'], '测试')
    return r

a = {q['uid'] for q in make_paper('数学')['questions']}
b = {q['uid'] for q in make_paper('数学')['questions']}
inter = a & b
print(f'  第一套 {len(a)} 题, 第二套 {len(b)} 题, 重复 {len(inter)} 题 -> {sorted(inter)[:6]}')
check('两套重复不超过 60%', len(inter) <= max(len(a), 1) * 0.6, f'{len(inter)}/{len(a)}')
r3 = make_paper('数学')['questions']
c = {q['uid'] for q in r3}
print(f'  第三套 {len(c)} 题, 与第一套重复 {len(a & c)} 题, 与前两套累计重复 {len(c & (a | b))} 题')
check('第三套仍有新题进来', len(c - (a | b)) >= 3, len(c - (a | b)))

print('== 4. 难度阶梯(同一张卷子里应有不同难度) ==')
r = quiz.pick_questions('数学', n=15, use_ai=False)
diffs = sorted(collections.Counter(q['difficulty'] for q in r['questions']).items())
print(f'  难度分布: {dict(diffs)}')
check('难度档位 >= 3 档', len(diffs) >= 3, dict(diffs))

print('== 5. 判断题判卷容错 ==')
jq = {'qtype': 'judge', 'answer': '对'}
for ua, want in [('对', True), ('√', True), ('T', True), ('正确', True), ('true', True),
                 ('错', False), ('×', False), ('F', False), ('错误', False), ('', False)]:
    got = quiz.is_correct(jq, ua)
    check(f'判断 answer=对 user={ua!r} -> {got}', got == want, f'期望 {want}')
jq2 = {'qtype': 'judge', 'answer': '错'}
check('判断 answer=错 user=错 -> True', quiz.is_correct(jq2, '错') is True)
check('判断 answer=错 user=对 -> False', quiz.is_correct(jq2, '对') is False)

print('== 6. 多选题判卷(必须完全一致) ==')
mq = {'qtype': 'multi', 'answer': 'ABC', 'options': {'A': 'x', 'B': 'y', 'C': 'z', 'D': 'w'}}
check('multi 全对 ABC', quiz.is_correct(mq, 'ABC') is True)
check('multi 全对 CBA(顺序无关)', quiz.is_correct(mq, 'CBA') is True)
check('multi 列表形式 ["A","C","B"]', quiz.is_correct(mq, ['A', 'C', 'B']) is True)
check('multi 少选 AB -> False', quiz.is_correct(mq, 'AB') is False)
check('multi 多选 ABCD -> False', quiz.is_correct(mq, 'ABCD') is False)
check('multi 空 -> False', quiz.is_correct(mq, '') is False)

print('== 7. 原有题型没被改坏 ==')
check('填空 -4 vs －４(全角)', quiz.is_correct({'answer': '-4'}, '－４') is True)
check('分数 0.5 = 1/2', quiz.is_correct({'answer': '1/2'}, '0.5') is True)
check('选择题答内容', quiz.is_correct({'answer': 'B', 'options': {'A': '1', 'B': '3'}}, '3') is True)
check('多解 7 或 3 / 3 或 7', quiz.is_correct({'answer': '7 或 3', 'alt': ['3 或 7']}, '3 或 7') is True)
check('判断不误伤普通填空', quiz.is_correct({'answer': '5'}, '5') is True)

print('== 8. 30 题大卷能否组出来(纯题库) ==')
r = quiz.pick_questions('数学', n=30, use_ai=False)
print(f"  出 {len(r['questions'])} 题: {r['msg']}")
check('30 题能凑够', len(r['questions']) == 30, len(r['questions']))
check('30 题里题型 >= 4 种', len({q['qtype'] for q in r['questions']}) >= 4,
      {q['qtype'] for q in r['questions']})

print()
print('FAILS:', fails if fails else '无, 全过')
sys.exit(1 if fails else 0)
