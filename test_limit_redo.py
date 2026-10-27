# -*- coding: utf-8 -*-
"""
验证两块新功能, 全用临时库:
  A 限时收卷: 按题量定时长 / 到点自动交卷 / 没做完的标清原因并进错题本
  B 错题重做: 答对归已掌握, 答错留在错题本并按新答案重画图
  C 阅读题: 带原文能进当前进度段, 要点按覆盖率判
"""
import os
import sys
import json
import tempfile
import datetime

_tmp = os.path.join(tempfile.gettempdir(), 'study_limit_redo.db')
if os.path.exists(_tmp):
    os.remove(_tmp)

import db           # noqa: E402
db.DB_PATH = _tmp
db.init_db()

import llm          # noqa: E402
import diagram      # noqa: E402
import quiz         # noqa: E402
quiz.init_quiz_db()

PASS, FAIL = [], []


def ck(cond, name, extra=''):
    (PASS if cond else FAIL).append(name + ((' | ' + str(extra)) if extra else ''))
    print(('  OK  ' if cond else ' FAIL ') + name + ((' | ' + str(extra)) if extra else ''))


print('=== A1. 按题量定时长 ===')
ck(quiz.limit_seconds(10) == 900, '10 题 = 15 分钟', quiz.limit_seconds(10))
ck(quiz.limit_seconds(15) == 1200, '15 题 = 20 分钟', quiz.limit_seconds(15))
ck(quiz.limit_seconds(20) == 1500, '20 题 = 25 分钟', quiz.limit_seconds(20))
ck(quiz.limit_seconds(30) == 2100, '30 题 = 35 分钟', quiz.limit_seconds(30))
ck(quiz.limit_seconds(15, '宽松') == 1800, '宽松 = 30 分钟', quiz.limit_seconds(15, '宽松'))
ck(quiz.limit_seconds(15, '不限时') == 0, '不限时 = 0', quiz.limit_seconds(15, '不限时'))
ck(quiz.limit_seconds(12) == 1200, '12 题按公式兜底', quiz.limit_seconds(12))
ck(quiz.limit_text(1200) == '20 分钟', '限时文案', quiz.limit_text(1200))
ck(quiz.limit_text(0) == '不限时', '不限时文案')

print('=== A2. 出卷落 deadline ===')
QS = [
    dict(uid='t1', source='seed', chapter='测试', topic='整式', qtype='fill', difficulty=2,
         stem='计算 2+3=', answer='5', alt=[], explain='直接相加', verified=True),
    dict(uid='t2', source='seed', chapter='测试', topic='整式', qtype='choice', difficulty=2,
         stem='下列正确的是', options={'A': '1', 'B': '2'}, answer='A', alt=[], explain='选 A', verified=True),
    dict(uid='t3', source='seed', chapter='测试', topic='整式', qtype='answer', difficulty=3,
         stem='说说你的理解', answer='要点: ①甲; ②乙', explain='先甲后乙', verified=True,
         points=[{'no': '①', 'text': '甲的说法', 'w': 1}, {'no': '②', 'text': '乙的说法', 'w': 1}]),
]
pid = quiz.create_paper('数学', QS, '测试段', limit_sec=quiz.limit_seconds(10))
p = quiz.get_paper(pid)['paper']
ck(p['limit_sec'] == 900 and bool(p['deadline']), 'paper 存了限时与到点时刻', p['deadline'])
_left = quiz.paper_time_left(pid)
ck(_left['limited'] and 890 <= _left['left'] <= 900, '剩余时间在 900 秒内', _left['left'])
ck(_left['total'] == 900, '返回总时长, 页面要显示"限时 15 分钟"')

_pid_free = quiz.create_paper('数学', QS[:1], '测试段', limit_sec=0)
ck(not quiz.paper_time_left(_pid_free)['limited'], '不限时的卷子不触发收卷')

print('=== A3. 到点自动收卷: 没做完的标原因, 进错题本 ===')
items = quiz.get_paper(pid)['items']
quiz.save_answer(items[0]['id'], '6')        # 做错了
quiz.save_answer(items[1]['id'], '')         # 空着
quiz.save_answer(items[2]['id'], '甲')        # 答了一半(主观)
# 把到点时刻挪到过去, 模拟时间用完
with quiz._conn() as c:
    c.execute("UPDATE quiz_papers SET deadline=? WHERE id=?",
              ((datetime.datetime.now() - datetime.timedelta(seconds=5)).isoformat(timespec='seconds'), pid))
ck(quiz.paper_time_left(pid)['over'], '时间判断为已到点')

llm.is_llm_enabled = lambda: False           # 这一步不依赖联网
rep = quiz.grade_paper(pid, '数学', close_reason='timeout')
ck(rep['ok'] and rep['close_reason'] == 'timeout', '判卷记下是超时收卷')
ck(rep['timeout_cnt'] == 1, '统计出 1 题空着没写', rep['timeout_cnt'])
ck(rep['right'] == 0, '这一卷 0 题答对(填空答错/选择空着/简答只答一半)', rep['right'])
items2 = quiz.get_paper(pid)['items']
ck(items2[0].get('correct') == 0, '答错的判错')
ck(items2[1].get('unanswered') == 1 and items2[1].get('miss_reason') == 'timeout',
   '空题标 unanswered + timeout', items2[1].get('miss_reason'))
ck(items2[2].get('unanswered') == 0, '写了答案(哪怕不全)不算未答', items2[2].get('unanswered'))

mist = db.list_mistakes(subject='数学', limit=50)
reasons = {m['question'][:12]: m for m in mist}
ck(len(mist) == rep['pushed'] == 3, '3 道错题都进了错题本', len(mist))
blank_m = [m for m in mist if m.get('miss_reason') == 'timeout']
ck(len(blank_m) == 1, '只有空着的那道标 timeout', len(blank_m))
ck(all('时间到未作答' in (m.get('wrong_reason') or '') for m in blank_m), '错因写明「时间到未作答」')
ck(all('⏰' in (m.get('explanation') or '') for m in blank_m), '讲解开头就说明是时间到没做完')
ck(all(m.get('qtype') for m in mist), '题型带进错题本, 才能重做', [m.get('qtype') for m in mist])
_m_answer = [m for m in mist if m.get('qtype') == 'answer']
ck(bool(_m_answer) and bool(_m_answer[0].get('points_json')), '主观题的评分点也带进来了')

print('=== A4. 自己交卷时空着 -> 标 blank 而不是 timeout ===')
pid2 = quiz.create_paper('数学', QS[:2], '测试段', limit_sec=600)
i2 = quiz.get_paper(pid2)['items']
quiz.save_answer(i2[0]['id'], '5')
quiz.save_answer(i2[1]['id'], '')
rep2 = quiz.grade_paper(pid2, '数学', close_reason='manual')
it2 = quiz.get_paper(pid2)['items']
ck(it2[1].get('miss_reason') == 'blank', '自己交卷空着 -> blank', it2[1].get('miss_reason'))
_b = [m for m in db.list_mistakes(subject='数学', limit=50) if m.get('miss_reason') == 'blank']
ck(len(_b) >= 1 and '未作答' in (_b[0].get('wrong_reason') or ''), '错因写「未作答」')

print('=== B. 错题本里重做 ===')
mid = [m['id'] for m in mist if m.get('qtype') == 'fill'][0]
_r1 = quiz.retry_mistake(mid, '5')            # 客观题答对
ck(_r1['ok'] and _r1['correct'], '客观题答对', _r1['msg'])
_m1 = db.get_mistake(mid)
ck(_m1['mastered'] == 1, '答对 -> 归到已掌握')
ck(_m1['review_count'] == 1, '复习次数 +1', _m1['review_count'])
ck(len(db.list_redos(mid)) == 1, '留下 1 条重做记录')

mid2 = [m['id'] for m in mist if m.get('qtype') == 'choice'][0]
_r2 = quiz.retry_mistake(mid2, 'B')           # 客观题答错
ck(_r2['ok'] and not _r2['correct'], '客观题答错')
_m2 = db.get_mistake(mid2)
ck(_m2['mastered'] == 0, '答错 -> 仍留在错题本')
ck(_m2.get('answer') == 'B', '把这次答案写回错题', _m2.get('answer'))
ck(_m2.get('diagram_json') is None, '讲解图作废, 下次打开按新答案重画')
ck('重做' in (_m2.get('wrong_reason') or ''), '错因标成重做仍错', _m2.get('wrong_reason'))
ck(len(db.list_redos(mid2)) == 1, '留下重做记录')

print('=== B2. 主观题按要点覆盖率判 ===')
mid3 = [m['id'] for m in mist if m.get('qtype') == 'answer'][0]
pts = json.loads(db.get_mistake(mid3)['points_json'])
ck(len(pts) == 2, '错题带 2 个评分点')
_r3 = quiz.retry_mistake(mid3, '甲的说法')     # 只答一条
ck(_r3['ok'], '主观题重做有结果')
ck(_r3.get('coverage') is not None and abs(_r3['coverage'] - 0.5) < 0.01,
   '覆盖 50% (AI 不在时走本地粗判)', _r3.get('coverage'))
ck(not _r3['correct'], '50% < 60% -> 判错')
_r4 = quiz.retry_mistake(mid3, '甲的说法和乙的说法')
ck(_r4['correct'] and _r4['coverage'] == 1.0, '两条都答到 -> 100% 判对', _r4.get('coverage'))
ck(db.get_mistake(mid3)['mastered'] == 1, '答对后归到已掌握')

print('=== C. 阅读题: 原文 + lesson 能进当前进度 ===')
bank = quiz.all_bank('语文')
_s, _ = None, None
_rd = [q for q in bank if q.get('qtype') == 'answer' and q.get('passage') and q.get('points')]
ck(len(_rd) >= 12, '语文带原文和评分点的阅读题 >= 12 道', len(_rd))
ck(all(len(q['points']) >= 3 for q in _rd), '每道至少 3 个评分点')
_q = [q for q in _rd if q['uid'] == 'c035'][0]
ck(_q.get('lesson') == '从百草园到三味书屋', '阅读题带课文名 lesson')
from datetime import date
_ph = quiz.current_phase('语文', date(2026, 10, 27))
_topics = _ph.get('topics') or []
ck(quiz.in_phase(_q, _topics), '阅读题能落进当前进度段(以前落不进来)', _topics[:3])
_st = quiz.scope_stats('语文', date(2026, 10, 27))
ck(_st['types'].get('answer', 0) >= 3, '当前进度段有 3 道以上简答/阅读', _st['types'])

print('=== C2. 要点覆盖率算分 ===')
_hits = [True, False, True]
_cov = quiz.coverage_of(quiz._norm_points(_q['points'])[:3], _hits)
ck(abs(_cov - 2 / 3) < 0.01, '3 条命中 2 条 = 66.7%', _cov)
ck(quiz.coverage_of([], []) is None, '没有评分点时不算覆盖率')
_local = quiz.local_point_hits(quiz._norm_points(_q['points']),
                               '写出百草园景物的丰富有趣，表现它是我的乐园，与三味书屋的枯燥形成对比')
ck(sum(_local) >= 1, '本地粗判至少命中一条', _local)

print('=== D. 没交的卷能接着做 ===')
_fu = quiz.find_unfinished('数学')
ck(bool(_fu) and _fu.get('status') == 'doing', '找到最近一张没交的卷', _fu.get('id'))
quiz.abandon_paper(_fu['id'])
ck(quiz.get_paper(_fu['id'])['paper']['status'] == 'abandoned', '放弃后状态是 abandoned')
ck(not quiz.find_unfinished('数学') or quiz.find_unfinished('数学')['id'] != _fu['id'],
   '放弃的卷不再被找回')

print()
print(f'通过 {len(PASS)} / 共 {len(PASS) + len(FAIL)}')
if FAIL:
    print('失败项:')
    for f in FAIL:
        print('  -', f)
    sys.exit(1)
