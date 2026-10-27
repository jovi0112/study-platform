# -*- coding: utf-8 -*-
"""判卷端到端: 造一张含 选择/判断/多选/填空 的卷子, 真的走 save_answer -> grade_paper。
这是「新题型能不能判对」的硬验证, 不是只看函数返回值。"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))

tmpdir = tempfile.mkdtemp(prefix='gradev2_')
import db
db.DB_PATH = os.path.join(tmpdir, 'study.db')
import quiz

quiz.init_quiz_db(); db.init_db()

def mk(uid, qtype, answer, options=None, stem=None):
    return {'uid': uid, 'subject': '数学', 'chapter': '有理数', 'topic': '混合测试',
            'qtype': qtype, 'difficulty': 3, 'stem': stem or f'{qtype} 测试题',
            'options': options, 'answer': answer, 'alt': [], 'explain': '解析占位',
            'source': 'seed', 'verified': True}

QS = [
    mk('t-choice', 'choice', 'B', {'A': '1', 'B': '3', 'C': '5', 'D': '7'}),
    mk('t-judge-yes', 'judge', '对'),
    mk('t-judge-no', 'judge', '错'),
    mk('t-multi', 'multi', 'ABC', {'A': 'a', 'B': 'b', 'C': 'c', 'D': 'd'}),
    mk('t-fill', 'fill', '-4'),
]

fail = []
def check(name, cond, extra=''):
    print(('  [OK] ' if cond else '  [FAIL] ') + name + (('  -> ' + str(extra)) if extra else ''))
    if not cond:
        fail.append(name)

print('== 情况 A: 全部答对(含判断/多选) ==')
pid = quiz.create_paper('数学', QS, '判卷测试')
items = quiz.get_paper(pid)['items']
answers = {'t-choice': 'B', 't-judge-yes': '对', 't-judge-no': '错',
           't-multi': 'ACB', 't-fill': '－４'}   # 多选故意乱序, 填空故意全角
for it in items:
    quiz.save_answer(it['id'], answers[it['uid']])
rep = quiz.grade_paper(pid, '数学')
print(f"  得分 {rep['score']} 答对 {rep['right']}/{rep['total']}")
check('5 题全判对', rep['right'] == 5, f"{rep['right']}/{rep['total']}")
for it in quiz.get_paper(pid)['items']:
    check(f"{it['qtype']} 判为对", it['correct'] == 1, it['user_answer'])

print('== 情况 B: 判断答反 / 多选少选 / 填空错 ==')
QS2 = [dict(q, uid=q['uid'] + '-b') for q in QS]
pid2 = quiz.create_paper('数学', QS2, '判卷测试2')
items2 = quiz.get_paper(pid2)['items']
wrong = {'t-choice-b': 'C', 't-judge-yes-b': '错', 't-judge-no-b': '对',
         't-multi-b': 'AB', 't-fill-b': '4'}
for it in items2:
    quiz.save_answer(it['id'], wrong[it['uid']])
rep2 = quiz.grade_paper(pid2, '数学')
print(f"  得分 {rep2['score']} 答对 {rep2['right']}/{rep2['total']}")
check('5 题全判错(不冤枉也不放过)', rep2['right'] == 0, f"{rep2['right']}/{rep2['total']}")

print('== 情况 C: 判断题留空 ==')
QS3 = [dict(q, uid=q['uid'] + '-c') for q in QS if q['qtype'] == 'judge']
pid3 = quiz.create_paper('数学', QS3, '判卷测试3')
for it in quiz.get_paper(pid3)['items']:
    quiz.save_answer(it['id'], '')
rep3 = quiz.grade_paper(pid3, '数学')
check('留空判错', rep3['right'] == 0, f"{rep3['right']}/{rep3['total']}")

print('== 情况 D: 错题是否流进错题本 ==')
import sqlite3
conn = sqlite3.connect(db.DB_PATH); conn.row_factory = sqlite3.Row
n = conn.execute("SELECT COUNT(*) c FROM mistakes").fetchone()['c']
print(f'  错题本里现在有 {n} 条')
check('错题已回流错题本', n >= 3, n)
rows = conn.execute("SELECT question, subject, correct_answer FROM mistakes ORDER BY id DESC LIMIT 5").fetchall()
for r in rows:
    print('   -', r['subject'], str(r['question'])[:40], '| 正确答案:', r['correct_answer'])

print()
print('FAILS:', fail if fail else '无, 全过')
sys.exit(1 if fail else 0)
