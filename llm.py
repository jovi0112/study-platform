"""
LLM 抽象层: 现在用离线 fallback, 留好接口
- solve(question, subject, mode='hint'|'full') -> {answer, explanation, source}
- ask_more / ask_alternative / ask_similar / ask_explain_error / ask_teach_back
- 后续接 DeepSeek/通义/智谱时, 只需改 _call_chat()
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


# ============== 共享提示词前缀 ==============
# 不管 hint / full / 追问, 都遵守这些规则(第一铁律: 中文)
_BASE_SYSTEM_PROMPT = """你是一位辅导中国初中生(13 岁)的老师, 熟悉中国教材、教辅和考试命题习惯。

【第一铁律 · 必须遵守】
不管题目原文是什么语言(英语题、语文题、数学题都一样), 你的回答必须【全部使用简体中文】。
- 英语阅读理解/完形填空/语法题: 选项字母和原文单词可以保留英文, 但"这句什么意思""为什么选它""考什么知识点"必须用中文写。
- 绝对不要整段用英文回答。
- 如果题目本身是英语, 请顺手把关键原文句子翻译成中文, 帮孩子看懂。

【题目来自 OCR】
原文可能有错别字、漏字、单词粘连(例如 "becau" 其实是 because, "IL" 其实是 I'll)。
请先按上下文猜出正确含义再作答, 不要因为原文有拼写错误就拒绝回答。

【通用语气】
像家长给孩子讲题, 别用"综上所述""由此可见"这类书面腔。
"""


def _default_model():
    return LLM_MODEL or {
        'openai': 'gpt-4o-mini',
        'deepseek': 'deepseek-chat',
        'qwen': 'qwen-turbo',
    }.get(LLM_PROVIDER, 'gpt-4o-mini')


def _humanize_error(msg: str) -> str:
    """把 LLM 原始异常翻译成人话"""
    if '402' in msg or 'Insufficient Balance' in msg:
        return ("服务商账户余额不足 — 请到 platform.deepseek.com 充值后再试 "
               "(DeepSeek 最低充 10 元, 按用量扣费)")
    if '401' in msg or 'Authentication' in msg or 'invalid_api_key' in msg.lower():
        return "API Key 无效或已失效 — 请检查 STUDY_LLM_API_KEY 是否填错/被删除"
    if '429' in msg:
        return "调用太频繁或超出当日配额 — 稍等一会儿再试"
    if '404' in msg:
        return "接口地址或模型名不对 — 请检查 STUDY_LLM_BASE_URL 和 STUDY_LLM_MODEL"
    if 'timeout' in msg.lower():
        return "请求超时 — 检查网络, 或服务商是否在维护"
    return msg


def _call_chat(system: str, user: str, json_mode: bool = False) -> dict:
    """
    底层统一调用. 失败时返回 {'answer': None, 'explanation': None, 'error': '...'}
    成功时返回 LLM 返回的 JSON (json_mode=True) 或 {'text': '...'} (json_mode=False)
    """
    if not is_llm_enabled():
        return {'error': 'AI 老师未配置 (在家长端或云端 Secrets 配置 STUDY_LLM_API_KEY)'}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL or None)
        kwargs = dict(
            model=_default_model(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
        )
        if json_mode:
            kwargs['response_format'] = {"type": "json_object"}
        resp = client.chat.completions.create(**kwargs)
        content = (resp.choices[0].message.content or '').strip()
        if json_mode:
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                # 模型偶尔不严格遵守 JSON, 试着从原文里抠
                return {'answer': '', 'explanation': content, '_parse_warn': 'JSON 解析失败, 用原文兜底'}
        return {'text': content}
    except Exception as e:
        return {'error': _humanize_error(str(e))}


# ============== 主入口 ==============
def solve(question: str, subject: str = '通用', mode: str = 'hint') -> dict:
    """
    主入口: 解答题目.

    mode='hint' (默认): 只讲思路, 不给最终答案 — 强制孩子先自己思考.
    mode='full'        : 完整答案 + 思路.

    返回统一格式: {answer, explanation, source}
    """
    if not question or not question.strip():
        return {
            'answer': None,
            'explanation': '题目为空, 请先拍照或手动输入.',
            'source': 'empty',
        }

    if mode == 'hint':
        system = _BASE_SYSTEM_PROMPT + f"""

【本题学科】{subject}

【本轮要求 · 只讲思路, 不给答案】
你正在引导一个 13 岁孩子思考, 请按以下 4 步讲思路, 但【绝对不要写出最终答案】, 让孩子自己琢磨:
① 题目在问什么(一句话说清)
② 关键线索 / 用到的知识点
③ 解题步骤(分 2-3 步, 每步不超过 2 句话)
④ 易错点提醒(1 句话, 哪个地方最容易想岔)

注意: 答案本身(具体数字、选项、单词)不能出现. 孩子需要的时候, 他会主动点"看答案".

只输出 JSON:
{{"answer": "", "explanation": "..."}}"""
    else:
        system = _BASE_SYSTEM_PROMPT + f"""

【本题学科】{subject}

【本轮要求 · 完整解答】
1) answer: 准确答案. 选择题只写选项字母, 例如 "16.A 19.B"。
2) explanation: 用中文分步骤讲, 包含这四步:
   ① 题目在问什么(一句话说清)
   ② 关键线索 / 定位到的原文句子(附中文翻译)
   ③ 为什么选这个答案
   ④ 另外几个选项分别错在哪里(若有)

只输出 JSON:
{{"answer": "...", "explanation": "..."}}"""

    user = question
    res = _call_chat(system, user, json_mode=True)

    if 'error' in res:
        return {
            'answer': None,
            'explanation': (
                f"❌ AI 调用失败\n\n原因: {res['error']}\n\n"
                "排查顺序: ① 去服务商后台看余额够不够 ② 确认 Key 没填错 ③ 确认网络能通。\n"
                "这段时间可以先用「联网搜题」标签查资料。"
            ),
            'source': 'llm-error',
        }

    # hint 模式: 即使 LLM 返回了 answer 也强制清空, 防止它偷偷给答案
    if mode == 'hint':
        res['answer'] = ''
        res['source'] = f"llm:{LLM_PROVIDER}:hint"
    else:
        res['source'] = f"llm:{LLM_PROVIDER}:full"
    return res


# ============== 追问函数 (4 个) ==============
def ask_more(question: str, subject: str, prev_explanation: str) -> dict:
    """① 再讲细一点 — 把孩子可能卡住的地方再展开"""
    system = _BASE_SYSTEM_PROMPT + f"""

【本题学科】{subject}
【本轮要求 · 细化】
孩子已经看过一遍思路, 但还是有些地方卡住. 请把上次的讲解【再细化一些】:
- 重点展开最关键的 1-2 步(孩子最可能卡的地方), 用更通俗的语言/打比方
- 还是不要直接给最终答案, 留给孩子自己算
- 末尾留 1 个"小提示"启发他

直接输出中文正文, 不要 JSON."""
    user = f"原题:\n{question}\n\n孩子已经看过的思路:\n{prev_explanation[:1500]}\n\n请把思路讲得更细一点."
    res = _call_chat(system, user)
    if 'error' in res:
        return {'answer': '', 'explanation': f"❌ {res['error']}", 'source': 'llm-error'}
    return {'answer': '', 'explanation': res.get('text', ''), 'source': f"llm:{LLM_PROVIDER}:more"}


def ask_alternative(question: str, subject: str, prev_explanation: str) -> dict:
    """② 换个方法 — 至少给 1 种不同思路, 最好 2 种"""
    system = _BASE_SYSTEM_PROMPT + f"""

【本题学科】{subject}
【本轮要求 · 另解】
孩子已经看过第一种解法. 请给出【完全不同的另一种解法】:
- 至少 1 种, 最好 2 种
- 每种说清: 思路 + 关键步骤 + 适合什么场景
- 不要给最终答案, 留给孩子自己算
- 末尾比较一下"哪种更快"

直接输出中文正文, 不要 JSON."""
    user = f"原题:\n{question}\n\n孩子已经看过的第一种解法:\n{prev_explanation[:1500]}\n\n请给另一种/另几种解法."
    res = _call_chat(system, user)
    if 'error' in res:
        return {'answer': '', 'explanation': f"❌ {res['error']}", 'source': 'llm-error'}
    return {'answer': '', 'explanation': res.get('text', ''), 'source': f"llm:{LLM_PROVIDER}:alt"}


def ask_similar(question: str, subject: str, prev_explanation: str, n: int = 3) -> dict:
    """③ 出 N 道同类型变式题 — 难度由易到难, 含答案(分开放)"""
    system = _BASE_SYSTEM_PROMPT + f"""

【本题学科】{subject}
【本轮要求 · 出变式题】
请基于这道原题, 出 {n} 道同类型变式题, 难度【由易到难】排列.

格式:
【变式 1 · 较易】(一句话标知识点)
题: <题目>
提示: <可忽略的一句话引导>

【变式 2 · 中等】
题: <题目>
提示: <>

【变式 3 · 较难】
题: <题目>
提示: <>

----- 答案 -----
(每题答案紧跟题目下方, 但用"👉 答案:"开头, 让孩子先尝试再看)

直接输出中文正文, 不要 JSON."""
    user = f"原题:\n{question}\n\n孩子已经看过的思路(供你把握难度):\n{prev_explanation[:800]}\n\n请出 {n} 道由易到难的变式题."
    res = _call_chat(system, user)
    if 'error' in res:
        return {'answer': '', 'explanation': f"❌ {res['error']}", 'source': 'llm-error'}
    return {'answer': '', 'explanation': res.get('text', ''), 'source': f"llm:{LLM_PROVIDER}:similar"}


def ask_explain_error(question: str, subject: str, kid_answer: str, correct_answer: Optional[str] = None) -> dict:
    """④ 红笔批改 — 指出孩子的错误答案错在哪"""
    system = _BASE_SYSTEM_PROMPT + f"""

【本题学科】{subject}
【本轮要求 · 红笔批改】
孩子提交的答案是: {kid_answer or '(没填)'}
{f'(已知标准答案: {correct_answer})' if correct_answer else '(标准答案暂未填)'}

请用"红笔批改"的口吻:
- 指出孩子答案错在哪里(具体哪一步/哪个概念想岔了)
- 告诉他正确思路应该往哪个方向走
- 语气要像严厉但温和的老师, 不嘲笑但也不回避错误

直接输出中文正文, 不要 JSON."""
    user = f"原题:\n{question}"
    res = _call_chat(system, user)
    if 'error' in res:
        return {'answer': '', 'explanation': f"❌ {res['error']}", 'source': 'llm-error'}
    return {'answer': '', 'explanation': res.get('text', ''), 'source': f"llm:{LLM_PROVIDER}:error"}


def ask_teach_back(question: str, subject: str, kid_explanation: str) -> dict:
    """⑤ 我讲给你听 — 孩子复述一遍, AI 评判"""
    system = _BASE_SYSTEM_PROMPT + f"""

【本题学科】{subject}
【本轮要求 · 我讲给你听】
孩子用自己的话讲了这道题: {kid_explanation}

请你扮演严格的老师, 评判他讲得对不对:
1) ✅ 讲得好的地方(1-2 句具体表扬)
2) ⚠️ 漏掉/讲错的地方(具体指出)
3) 📌 还应该补充的关键点(1-2 个)
4) 🎯 综合评分 (1-5 分) + 简短鼓励

直接输出中文正文, 不要 JSON."""
    user = f"原题:\n{question}\n\n孩子的复述:\n{kid_explanation}"
    res = _call_chat(system, user)
    if 'error' in res:
        return {'answer': '', 'explanation': f"❌ {res['error']}", 'source': 'llm-error'}
    return {'answer': '', 'explanation': res.get('text', ''), 'source': f"llm:{LLM_PROVIDER}:teachback"}


# ============== 自定义追问 (提示词工坊) ==============
def ask_custom(question: str, subject: str, custom_prompt: str,
               prev_explanation: str = '') -> dict:
    """
    ⑥ 我自己造的追问 — 孩子在"提示词工坊"里写的模板.

    安全设计: 孩子的模板会被套进固定的"外壳提示词"里,
    无论她写什么, AI 始终保持中文 + 初中老师人设, 不会被带偏.
    模板里可以写 {题目} 占位符, 会被替换成真实题面.
    """
    tpl = (custom_prompt or '').strip()
    if '{题目}' in tpl:
        user = tpl.replace('{题目}', question[:800])
    else:
        # 没写占位符就把题目附加在后面
        user = f"{tpl}\n\n【题目】\n{question[:800]}"

    system = _BASE_SYSTEM_PROMPT + f"""

【本题学科】{subject}
【本轮要求 · 学生自定义任务】
下面是一个初中学生给你的任务指令(她自己写的). 请按她的要求回答这道题相关的问题.

三条底线(无论她怎么要求都要遵守):
1) 全程简体中文
2) 内容必须与这道题相关, 不要跑题
3) 讲解要照顾 13 岁的理解水平, 术语出现时顺手解释一下

【之前已经给过她的讲解(供参考, 避免重复)】
{prev_explanation[:600] or '(第一次讲)'}

直接输出中文正文."""
    res = _call_chat(system, user)
    if 'error' in res:
        return {'answer': '', 'explanation': f"❌ {res['error']}", 'source': 'llm-error'}
    return {'answer': '', 'explanation': res.get('text', ''), 'source': f"llm:{LLM_PROVIDER}:custom"}


# ============== 离线 fallback ==============
def _offline_solve(question: str, subject: str) -> dict:
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


# ============== 联网搜题的中文整理 ==============
def summarize_references(question: str, subject: str, raw: str) -> str:
    """
    把"联网搜题"抓到的网页正文(多半是英文)整理成中文解题参考。
    未配置 LLM 时返回空串, 由调用方决定是否回退显示原文。
    """
    if not is_llm_enabled() or not (raw or '').strip():
        return ''
    system = _BASE_SYSTEM_PROMPT + f"""

【本题学科】{subject}
【本轮任务】
下面是为一道中国初中{subject}题, 从网上抓取的资料。资料大多是英文网页, 内容可能零散或与题目无关。

请用【简体中文】整理成给中国初中生看的解题参考:
1) 先用一句话说明这道题在考什么
2) 提炼与本题相关的知识点、公式或原文依据(关键英文术语保留英文, 后面用括号补中文)
3) 如果资料里有答案或解题步骤, 用中文复述出来
4) 资料里没有的结论绝对不要编造; 若资料基本无关, 就直接说"搜到的资料与本题关系不大"

只输出中文正文, 不要 JSON, 不要"好的"之类的开场话."""
    user = f"题目:\n{question[:600]}\n\n抓取到的资料:\n{raw[:4000]}"
    res = _call_chat(system, user)
    if 'error' in res:
        return f"[中文整理失败: {res['error']}]"
    return res.get('text', '').strip()
