# -*- coding: utf-8 -*-
"""手动指定章节 功能验证. 用临时库, 不碰孩子真实数据."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))

tmpdir = tempfile.mkdtemp(prefix='phasetest_')
import db
db.DB_PATH = os.path.join(tmpdir, 'study.db')
import quiz

fails = []
def check(name, cond, extra=''):
    print(('  [OK]   ' if cond else '  [FAIL] ') + name + (('  -> ' + str(extra)) if extra else ''))
    if not cond:
        fails.append(name)

quiz.init_quiz_db()
db.init_db()

print('== 1. 未指定时走日历自动 ==')
ph = quiz.current_phase('数学')
auto_label = ph['label']
check('默认按日历自动', not ph.get('manual'), auto_label)
check('manual_summary 为空', quiz.manual_summary('数学') == '')

print('== 2. 指定单段(第0段 有理数上) ==')
opts = quiz.phase_options('数学')
print('  可选项:', len(opts))
r = quiz.set_manual('数学', [0], note='学校回头补第一章')
check('保存成功', r['ok'], r['msg'])
ph = quiz.current_phase('数学')
check('生效的是手动指定', ph.get('manual') is True)
check('章节对了', ph['label'] == opts[0]['label'], ph['label'])
check('topics 来自该段', set(ph['topics']) == set(opts[0]['topics']), f"{len(ph['topics'])} 个知识点")
check('带上了指定时间', bool(ph.get('manual_set_at')), ph.get('manual_set_at'))
check('带上了备注', ph.get('manual_note') == '学校回头补第一章', ph.get('manual_note'))

print('== 3. 持久化(重新读库, 模拟下次打开页面) ==')
m = quiz.get_manual('数学')
check('库里读得到', m.get('indices') == [0], m)
check('manual_summary 有人话', '第' in quiz.manual_summary('数学') or opts[0]['label'] in quiz.manual_summary('数学'),
      quiz.manual_summary('数学'))

print('== 4. 段内只练部分知识点 ==')
pool = opts[1]['topics']
pick = pool[:2]
r = quiz.set_manual('数学', [1], topics=pick, note='1.4 还没讲')
ph = quiz.current_phase('数学')
check('只保留勾选的知识点', set(ph['topics']) == set(pick), ph['topics'])
check('label 用第1段', ph['label'] == opts[1]['label'], ph['label'])

print('== 5. 多段同时指定(复习 + 新课并行) ==')
r = quiz.set_manual('数学', [1, 2], note='期中复习')
ph = quiz.current_phase('数学')
check('两段合并', ' + ' in ph['label'], ph['label'])
check('topics 是两段并集',
      set(ph['topics']) == set(opts[1]['topics']) | set(opts[2]['topics']),
      f"{len(ph['topics'])} 个")

print('== 6. 上限保护(最多3段) ==')
r = quiz.set_manual('数学', [0, 1, 2, 3])
check('4段被拒', (not r['ok']) and '最多' in r['msg'], r['msg'])
check('被拒后原配置没被破坏', quiz.get_manual('数学').get('indices') == [1, 2],
      quiz.get_manual('数学').get('indices'))

print('== 7. 非法/越界输入 ==')
r = quiz.set_manual('数学', [99])
check('越界序号被拒', not r['ok'], r['msg'])
r = quiz.set_manual('数学', [])
check('空选择被拒', not r['ok'], r['msg'])
r = quiz.set_manual('不存在的科目', [0])
check('未知科目被拒', not r['ok'], r['msg'])

print('== 8. 改动记录(记录在案) ==')
logs = quiz.phase_log('数学')
check('有日志', len(logs) >= 3, f'{len(logs)} 条')
check('最新一条在最前', logs[0]['id'] > logs[-1]['id'])
check('日志含 before/after', bool(logs[0]['before_text']) and bool(logs[0]['after_text']),
      f"{logs[0]['before_text']} -> {logs[0]['after_text']}")
sets = [l for l in logs if l['action'] == 'set_manual']
check('记录了 set_manual', len(sets) >= 3, len(sets))

print('== 9. 出题范围跟着变(手动优先于天数偏移) ==')
quiz.set_offset('数学', 30)     # 故意把偏移设成很大的值
ph = quiz.current_phase('数学')
check('偏移不影响手动指定', ph.get('manual') is True and ' + ' in ph['label'], ph['label'])
sc = quiz.scope_stats('数学')
check('可出题数 > 0', sc['in_scope'] > 0, sc)
r = quiz.pick_questions('数学', n=8, use_ai=False)
check('能出卷', r['ok'], r.get('msg'))
qs = r['questions']
check('出的题都在指定范围内',
      all(q['topic'] in ph['topics'] or q.get('review') for q in qs),
      [q['topic'] for q in qs])
quiz.set_offset('数学', 0)

print('== 10. 改回自动 ==')
r = quiz.clear_manual('数学', note='学校按顺序讲了')
check('恢复成功', r['ok'], r['msg'])
ph = quiz.current_phase('数学')
check('回到日历自动', not ph.get('manual'), ph['label'])
check('配置已清空', quiz.get_manual('数学') == {})
logs = quiz.phase_log('数学')
check('恢复动作也留痕', logs[0]['action'] == 'clear_manual', logs[0]['action'])
r2 = quiz.clear_manual('数学')
check('重复恢复不报错', r2['ok'], r2['msg'])

print('== 11. 坏数据不炸页面 ==')
db.set_setting('quiz_manual_phase_语文', '{这不是json')
check('坏配置被当成"自动"', quiz.get_manual('语文') == {})
ph = quiz.current_phase('语文')
check('当前进度仍算得出来', ph and bool(ph.get('label')), ph and ph.get('label'))
db.set_setting('quiz_manual_phase_语文', '{"indices": []}')
check('空 indices 也当自动', quiz.get_manual('语文') == {})

print()
print('FAILS:', fails if fails else '无')
sys.exit(1 if fails else 0)
