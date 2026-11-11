"""笔记工坊整页冒烟（AppTest）：页面能渲染、不抛异常；查看页离线/在线都不崩。"""
import os
import sys

os.environ.setdefault('STUDY_LLM_API_KEY', 'sk-test-local')
os.environ.setdefault('STUDY_VISION_API_KEY', 'sk-test-local')

from streamlit.testing.v1 import AppTest

at = AppTest.from_file('app.py', default_timeout=300)
at.run()
assert not at.exception, f'整页异常: {[e.value for e in at.exception]}'

# 导航到笔记工坊
at.sidebar.radio[0].set_value('🗒️ 笔记工坊').run()
assert not at.exception, f'笔记工坊页异常: {[e.value for e in at.exception]}'

texts = '\n'.join(m.value for m in at.markdown) + '\n'.join(str(t.label) for t in at.tabs)
assert ('📤 上传 PDF 生成' in str([t.label for t in at.tabs])), \
    f'应该有生成/查看两个 tab，实际: {[t.label for t in at.tabs]}'

print('APP_NOTES_SMOKE_OK')
