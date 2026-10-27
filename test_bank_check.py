# -*- coding: utf-8 -*-
"""种子题库自检: JSON 合法性 / 字段完整 / 选择题答案在选项内 / 数学答案复算"""
import json, os, re
from fractions import Fraction

BASE = os.path.join(os.path.dirname(__file__), 'data')
FILES = {
    '数学': 'question_bank_math.json',
    '英语': 'question_bank_english.json',
    '语文': 'question_bank_chinese.json',
}

# 人工复算的数学题答案(独立于题库文件, 用于交叉核对)
MATH_TRUTH = {
    'm004': '-4', 'm005': '13', 'm006': '-6', 'm007': '-4', 'm009': '-8',
    'm010': '-16', 'm011': '-2', 'm012': '17', 'm019': '1/25', 'm045': '-8',
    'm002': '5', 'm003': '±3', 'm008': '3', 'm014': '9', 'm017': '2',
    'm018': '-1/6', 'm021': '-3', 'm022': '3', 'm024': 'x²+5x-1', 'm025': 'a+2b',
    'm026': '5', 'm027': '-x+2', 'm029': '4x²-x+1', 'm031': '2', 'm032': '7',
    'm033': '5', 'm034': '12', 'm036': '-2', 'm038': '4', 'm039': '60',
    'm041': '32°30′', 'm042': '5.5', 'm043': '60',
}
# 用 sympy 风格的可执行验算(表达式 -> 期望值), 独立于题库
MATH_EVAL = {
    'm004': ('(-7)+(+3)', -4), 'm005': ('5-(-8)', 13), 'm006': ('(-2)*(-3)*(-1)', -6),
    'm007': ('(-24)/6', -4), 'm009': ('(-2)**3', -8), 'm010': ('-(2**4)', -16),
    'm011': ('-(1**2026)+((-1)**2027)', -2), 'm012': ('(-3)**2-2*(-4)', 17),
    'm018': ('Fraction(-1,2)+Fraction(1,3)', Fraction(-1, 6)),
    'm019': ('Fraction(-1,-5)*Fraction(1,5)', Fraction(1, 25)),
    'm045': ('-(3**2)+((-2)**2)*Fraction(-1,4)-(6/(-3))', -8.0),
    'm042': ('5/2+3', 5.5),
}

errors, warns = [], []
total = 0
for subj, fn in FILES.items():
    p = os.path.join(BASE, fn)
    try:
        data = json.load(open(p, encoding='utf-8'))
    except Exception as e:
        errors.append(f'{subj}: JSON 解析失败 -> {e}')
        continue
    qs = data.get('questions', [])
    total += len(qs)
    ids = set()
    for q in qs:
        qid = q.get('id', '?')
        if qid in ids:
            errors.append(f'{subj}/{qid}: id 重复')
        ids.add(qid)
        for k in ('id', 'chapter', 'topic', 'type', 'difficulty', 'stem', 'answer', 'explain'):
            if k not in q:
                errors.append(f'{subj}/{qid}: 缺字段 {k}')
        if q.get('type') == 'choice':
            opts = q.get('options') or {}
            if len(opts) < 2:
                errors.append(f'{subj}/{qid}: 选择题选项不足')
            if q.get('answer') not in opts:
                errors.append(f'{subj}/{qid}: 答案 {q.get("answer")} 不在选项 {list(opts)} 中')
        if not (1 <= int(q.get('difficulty', 0)) <= 5):
            errors.append(f'{subj}/{qid}: difficulty 越界')
        if subj == '数学' and qid in MATH_TRUTH:
            if str(q.get('answer')).strip() != MATH_TRUTH[qid]:
                errors.append(f'{subj}/{qid}: 答案 {q.get("answer")} 与人工核对值 {MATH_TRUTH[qid]} 不符')

# 独立复算
for qid, (expr, expect) in MATH_EVAL.items():
    try:
        got = eval(expr, {'Fraction': Fraction})
    except Exception as e:
        errors.append(f'{qid}: 验算表达式执行失败 {e}')
        continue
    if abs(float(got) - float(expect)) > 1e-9:
        errors.append(f'{qid}: 复算 {got} != 期望 {expect}')

# 日历自检
try:
    cal = json.load(open(os.path.join(BASE, 'quiz_calendar.json'), encoding='utf-8'))
    for s, cfg in cal['subjects'].items():
        prev_to = None
        for ph in cfg['phases']:
            if prev_to and ph['from'] <= prev_to:
                errors.append(f'日历/{s}: 区间重叠 {ph["from"]}')
            if ph['from'] > ph['to']:
                errors.append(f'日历/{s}: {ph["label"]} 起止日期反了')
            if not ph.get('topics'):
                errors.append(f'日历/{s}: {ph["label"]} 没有知识点')
            prev_to = ph['to']
    print('日历科目:', list(cal['subjects']))
except Exception as e:
    errors.append(f'日历解析失败: {e}')

print(f'题库总题数: {total}')
print('错误:', len(errors))
for e in errors:
    print('  [ERR]', e)
if not errors:
    print('  OK: JSON 合法, 字段完整, 选择题答案自洽, 数学答案复算一致')
