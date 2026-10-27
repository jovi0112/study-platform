# -*- coding: utf-8 -*-
"""界面上手动指定章节的端到端验证. 用临时库, 不碰真实数据."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
tmpdir = tempfile.mkdtemp(prefix='appman_')

import db
db.DB_PATH = os.path.join(tmpdir, 'study.db')   # 必须在 AppTest.run 之前

from streamlit.testing.v1 import AppTest

fails = []
def check(name, cond, extra=''):
    print(('  [OK]   ' if cond else '  [FAIL] ') + name + (('  -> ' + str(extra)) if extra else ''))
    if not cond:
        fails.append(name)

at = AppTest.from_file('app.py', default_timeout=120)
at.run()
check('首屏无异常', not at.exception, [e.value for e in at.exception])

import quiz
check('初始为自动', not quiz.get_manual('数学'), quiz.manual_summary('数学'))

print('== 找到章节选择控件 ==')
ms = [m for m in at.multiselect if m.key and m.key.startswith('qz_man_phases_')]
check('有章节多选框', len(ms) == 1, [m.key for m in at.multiselect])
if not ms:
    print('FAILS:', fails); sys.exit(1)
ms = ms[0]
print('  选项:', ms.options)

print('== 模拟选中第0段并保存 ==')
target = ms.options[0]
ms.select(target).run()
check('选后无异常', not at.exception, [e.value for e in at.exception])

save = [b for b in at.button if b.key and b.key.startswith('qz_man_save_')]
check('有保存按钮', len(save) == 1, [b.key for b in at.button if b.key and 'man' in b.key])
check('未选之前按钮是禁用的(选了才启用)', save and not save[0].disabled)
save[0].click().run()
check('保存后无异常', not at.exception, [e.value for e in at.exception])

m = quiz.get_manual('数学')
check('真的写进库了', m.get('indices') == [0], m)

print('== 保存后卡片应显示手动标记 ==')
mds = [x.value for x in at.markdown]
hits = [v for v in mds if '手动指定' in v and '数学' in v]
check('卡片标了手动指定', len(hits) >= 1, hits[:1])
caps = [c.value for c in at.caption]
check('卡片说明里带指定时间', any('指定于' in c for c in caps),
      [c for c in caps if '指定于' in c][:1])
check('时间格式不带 T', any('指定于' in c and 'T' not in c.split('指定于')[1][:18] for c in caps),
      [c for c in caps if '指定于' in c][:1])

print('== 改动记录区块 ==')
caps = [c.value for c in at.caption]
check('界面上能看到改动记录', any('手动指定：' in c for c in caps),
      [c for c in caps if '手动指定：' in c][:1])

print('== 模拟改回自动 ==')
clr = [b for b in at.button if b.key and b.key.startswith('qz_man_clear_')]
check('有恢复按钮', len(clr) == 1)
clr[0].click().run()
check('恢复后无异常', not at.exception, [e.value for e in at.exception])
check('库里已清空', quiz.get_manual('数学') == {}, quiz.get_manual('数学'))
check('恢复动作留痕', quiz.phase_log('数学')[0]['action'] == 'clear_manual')

print('== 控件不再显示旧选择(状态一致性) ==')
at.run()
ms2 = [m for m in at.multiselect if m.key and m.key.startswith('qz_man_phases_')][0]
check('多选框已清空', list(ms2.value) == [], ms2.value)

print()
print('FAILS:', fails if fails else '无')
sys.exit(1 if fails else 0)
