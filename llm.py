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

        prompt = f"""你是一位辅导中国初中生(13 岁)的老师, 熟悉中国教材、教辅和考试命题习惯。

【第一铁律 · 必须遵守】
不管题目原文是什么语言(英语题、语文题、数学题都一样), 你的回答必须【全部使用简体中文】。
- 英语阅读理解/完形填空/语法题: 选项字母和原文单词可以保留英文, 但"这句什么意思""为什么选它""考什么知识点"必须用中文写。
- 绝对不要整段用英文回答。
- 如果题目本身是英语, 请顺手把关键原文句子翻译成中文, 帮孩子看懂。

【本题学科】{subject}

【注意: 题目文字来自拍照 OCR】
原文可能有错别字、漏字、单词粘连(例如 "becau" 其实是 because, "IL" 其实是 I'll)。
请先按上下文猜出正确含义再作答, 不要因为原文有拼写错误就拒绝回答。

【输出要求】
1) answer: 准确答案。选择题只写选项字母, 例如 "16.A 19.B"。
2) explanation: 用中文分步骤讲, 包含这四步:
   ① 题目在问什么(一句话说清)
   ② 关键线索 / 定位到的原文句子(附中文翻译)
   ③ 为什么选这个答案
   ④ 另外几个选项分别错在哪里
   语气像家长给孩子讲题, 别用"综上所述""由此可见"这类书面腔。

只输出 JSON, 不要任何额外文字:
{{"answer": "...", "explanation": "..."}}"""

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
        msg = str(e)
        if '402' in msg or 'Insufficient Balance' in msg:
            msg = ("服务商账户余额不足 — 请到 platform.deepseek.com 充值后再试 "
                   "(DeepSeek 最低充 10 元, 按用量扣费)")
        elif '401' in msg or 'Authentication' in msg or 'invalid_api_key' in msg.lower():
            msg = "API Key 无效或已失效 — 请检查 STUDY_LLM_API_KEY 是否填错/被删除"
        elif '429' in msg:
            msg = "调用太频繁或超出当日配额 — 稍等一会儿再试"
        elif '404' in msg:
            msg = "接口地址或模型名不对 — 请检查 STUDY_LLM_BASE_URL 和 STUDY_LLM_MODEL"
        return {"answer": None, "explanation": None, "error": msg}


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
    if real and real.get('error'):
        # 真实调用失败: 明确把原因告诉用户, 不要伪装成"未配置"
        return {
            'answer': None,
            'explanation': (
                f"❌ AI 调用失败\n\n原因: {real['error']}\n\n"
                "排查顺序: ① 去服务商后台看余额够不够 ② 确认 Key 没填错 ③ 确认网络能通。\n"
                "这段时间可以先用「联网搜题」标签查资料。"
            ),
            'source': 'llm-error',
        }
    return _offline_solve(question, subject)


def provider_display_name() -> str:
    """
    把"接口风格"(openai)翻译成家长看得懂的服务商名字。
    注意: STUDY_LLM_PROVIDER=openai 只是说"用 OpenAI 兼容格式调用",
    真正接的是哪家, 由 STUDY_LLM_BASE_URL 决定。
    """
    hay = f"{LLM_BASE_URL} {LLM_MODEL}".lower()
    for key, label in (
        ('deepseek', 'DeepSeek 深度求索'),
        ('dashscope', '通义千问'),
        ('aliyuncs', '通义千问'),
        ('bigmodel', '智谱 GLM'),
        ('zhipu', '智谱 GLM'),
        ('moonshot', 'Kimi'),
        ('siliconflow', 'SiliconFlow'),
        ('volces', '火山方舟'),
        ('openai.com', 'OpenAI'),
    ):
        if key in hay:
            return label
    return 'OpenAI 兼容接口' if LLM_PROVIDER == 'openai' else LLM_PROVIDER


def config_info() -> dict:
    """返回当前 LLM 配置状态(供家长端显示)"""
    return {
        'provider': LLM_PROVIDER,
        'provider_display': provider_display_name(),
        'enabled': is_llm_enabled(),
        'model': LLM_MODEL or '(默认)',
        'base_url': LLM_BASE_URL or '(默认)',
        'api_key_set': bool(LLM_API_KEY),
    }


def summarize_references(question: str, subject: str, raw: str) -> str:
    """
    把"联网搜题"抓到的网页正文(多半是英文)整理成中文解题参考。
    未配置 LLM 时返回空串, 由调用方决定是否回退显示原文。
    """
    if not is_llm_enabled() or not (raw or '').strip():
        return ''
    try:
        from openai import OpenAI
        client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL or None)
        model = LLM_MODEL or 'deepseek-chat'
        prompt = f"""下面是为一道中国初中{subject}题, 从网上抓取的资料。资料大多是英文网页, 内容可能零散或与题目无关。

请用【简体中文】整理成给中国初中生看的解题参考:
1) 先用一句话说明这道题在考什么
2) 提炼与本题相关的知识点、公式或原文依据(关键英文术语保留英文, 后面用括号补中文)
3) 如果资料里有答案或解题步骤, 用中文复述出来
4) 资料里没有的结论绝对不要编造; 若资料基本无关, 就直接说"搜到的资料与本题关系不大"

只输出中文正文, 不要 JSON, 不要"好的"之类的开场话。"""
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"题目:\n{question[:600]}\n\n抓取到的资料:\n{raw[:4000]}"},
            ],
            temperature=0.3,
        )
        return (resp.choices[0].message.content or '').strip()
    except Exception as e:
        return f"[中文整理失败: {type(e).__name__}: {e}]"
