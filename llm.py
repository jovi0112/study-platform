"""
LLM 抽象层: 现在用离线 fallback, 留好接口
- solve(question, subject) -> {answer, explanation, steps, sources}
- 后续接 DeepSeek/通义/智谱时, 只需改 _call_real_llm()
- 云端配置: 在 Streamlit Cloud 的 Secrets 页面添加 STUDY_LLM_* 键值
"""
import os
import json
from typing import Optional


def _read_secret(key: str, default: str = '') -> str:
    """
    同时支持 Streamlit Cloud 的 st.secrets 和本地环境变量
    云端部署时无需改代码, 直接在 Secrets 页面配即可
    """
    val = os.environ.get(key, '')
    if val:
        return val
    try:
        import streamlit as st
        if hasattr(st, 'secrets') and key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return default


# ============== 配置区 ==============
LLM_PROVIDER = _read_secret('STUDY_LLM_PROVIDER', 'offline')  # offline / openai / deepseek / qwen
LLM_API_KEY = _read_secret('STUDY_LLM_API_KEY', '')
LLM_BASE_URL = _read_secret('STUDY_LLM_BASE_URL', '')
LLM_MODEL = _read_secret('STUDY_LLM_MODEL', '')


def is_llm_enabled() -> bool:
    return LLM_PROVIDER != 'offline' and bool(LLM_API_KEY)


def _call_real_llm(question: str, subject: str) -> Optional[dict]:
    """
    真实 LLM 调用. 当前未配置(离线模式), 返回 None 走 fallback.
    后续接入时: 选 openai SDK 风格, 支持所有兼容服务.
    """
    if not is_llm_enabled():
        return None
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL or None,
        )
        model = LLM_MODEL or {
            'openai': 'gpt-4o-mini',
            'deepseek': 'deepseek-chat',
            'qwen': 'qwen-turbo',
        }.get(LLM_PROVIDER, 'gpt-4o-mini')

        prompt = f"""你是一个 13 岁孩子的学习助手. 请解答下面这道{subject}题.
要求: 1) 先给出准确答案 2) 解释解题思路, 分步骤 3) 用孩子能懂的语言.
用 JSON 输出: {{"answer": "...", "explanation": "..."}}"""

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": question},
            ],
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content
        return json.loads(content)
    except Exception as e:
        return {"answer": None, "explanation": None, "error": f"LLM 调用失败: {e}"}


def _offline_solve(question: str, subject: str) -> dict:
    """
    离线模式: 返回引导性问题, 提示孩子自己思考/拍照留存
    """
    return {
        'answer': None,
        'explanation': (
            "【离线模式】当前未配置 LLM API, AI 老师暂未启用.\n\n"
            "建议: 1) 拍照留存题目, 等家长接入 API 后再点 'AI 讲题' 按钮;\n"
            "      2) 先看'联网搜题'标签, 搜集参考资料;\n"
            "      3) 自己先做一遍, 把思路记到'错因'里, 强化记忆.\n\n"
            f"题目已识别(预览):\n{question[:300]}"
        ),
        'source': 'offline-fallback',
    }


def solve(question: str, subject: str = '通用') -> dict:
    """
    主入口: 解答题目. 优先用 LLM, 失败时离线 fallback.
    """
    if not question or not question.strip():
        return {
            'answer': None,
            'explanation': '题目为空, 请先拍照或手动输入.',
            'source': 'empty',
        }
    real = _call_real_llm(question, subject)
    if real and real.get('answer'):
        real['source'] = f"llm:{LLM_PROVIDER}"
        return real
    return _offline_solve(question, subject)


def config_info() -> dict:
    """返回当前 LLM 配置状态(供家长端显示)"""
    return {
        'provider': LLM_PROVIDER,
        'enabled': is_llm_enabled(),
        'model': LLM_MODEL or '(默认)',
        'base_url': LLM_BASE_URL or '(默认)',
        'api_key_set': bool(LLM_API_KEY),
    }
