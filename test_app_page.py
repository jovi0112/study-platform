# -*- coding: utf-8 -*-
"""整页冒烟: Streamlit 真跑一遍 app.py, 重点验证本轮改动
(题量档位 / 新题型控件 / 侧边栏真实状态文案) 不炸。"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from streamlit.testing.v1 import AppTest

at = AppTest.from_file('app.py', default_timeout=240)
at.run()

print('运行期异常:', [e.value for e in at.exception] or '无')
print('标题:', [t.value for t in at.title])
print('小标题:', [s.value for s in at.subheader][:8])
print('错误框:', [e.value for e in at.error][:3] or '无')
print('--- 侧边栏状态文案 ---')
for c in at.caption:
    v = str(c.value)
    if '本地识图' in v or '换电脑' in v or '视觉模型' in v or 'GitHub' in v:
        print('  ', v[:110])
print('--- 卷子大小选项 ---')
for s in at.selectbox:
    print('  ', s.label, '->', s.options)
print('--- 当前进度段可出题 ---')
for m in at.metric:
    print('  ', m.label, '=', m.value if hasattr(m, 'value') else '')

print('--- 点击出卷(纯题库, 关掉 AI 以免等太久) ---')
try:
    at.checkbox(key='qz_use_ai').uncheck().run()
    at.selectbox(key='qz_size').select('标准 · 15 题').run()
    b = [x for x in at.button if '刷新' in x.label]
    if b:
        b[0].click().run()
        print('异常:', [e.value for e in at.exception] or '无')
        print('成功提示:', [s.value for s in at.success][:2])
        print('小标题:', [s.value for s in at.subheader][:4])
        print('单选框数(选择+判断):', len(at.radio), ' 多选数:', len(at.multiselect), ' 输入框:', len(at.text_input))
        print('题干前三:', [m.value[:40].replace('\n', ' ') for m in at.markdown if '第 1 题' in str(m.value)][:1])
except Exception as e:
    print('点击流程异常:', type(e).__name__, e)
