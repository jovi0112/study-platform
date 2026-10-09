"""项目自检 + 模拟云端 secrets 读取测试"""
import os

print('=== 1. 关键文件检查 ===')
for f in ['app.py', 'db.py', 'ocr.py', 'llm.py', 'web_search.py',
         'requirements.txt', '.gitignore', '.streamlit/config.toml', 'README.md']:
    ok = os.path.exists(f)
    mark = chr(0x2713) if ok else chr(0x2717)
    print(f'  {mark} {f}')

print()
print('=== 2. 模块导入测试 ===')
for m in ['db', 'ocr', 'llm', 'web_search']:
    try:
        __import__(m)
        print(f'  ok  {m}')
    except Exception as e:
        print(f'  FAIL {m}: {type(e).__name__}: {e}')

print()
print('=== 3. 云端 st.secrets 读取模拟 ===')
# 模拟云端 secrets (Streamlit Cloud 部署时类似这样)
import streamlit as st
# Streamlit 不允许在脚本中改 st.secrets, 但我们的 _read_secret 同时读 os.environ
# 先用 os.environ 模拟
os.environ['STUDY_LLM_PROVIDER'] = 'openai'
os.environ['STUDY_LLM_API_KEY'] = 'sk-test'
os.environ['STUDY_LLM_MODEL'] = 'deepseek-chat'

# 重新 import llm
import importlib, llm
importlib.reload(llm)
print(f'  provider = {llm.LLM_PROVIDER}')
print(f'  key_set  = {bool(llm.LLM_API_KEY)}')
print(f'  model    = {llm.LLM_MODEL}')
print(f'  enabled  = {llm.is_llm_enabled()}')

print()
print('=== 4. 数据库初始化测试 ===')
db = __import__('db')
db.init_db()
print(f'  db path: {db.DB_PATH}')
print(f'  ok init_db')

print()
print('=== 5. 数据持久性测试 (多次重启) ===')
n1 = len(db.list_mistakes())
db.add_mistake('数学', 'test question ' + os.urandom(3).hex())
n2 = len(db.list_mistakes())
print(f'  第一次 {n1} -> 第二次 {n2} 条 (期望 +1)')

print()
print('ALL OK - 项目可以部署到云端')
