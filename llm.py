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

# 界面(app.py)会拿这个值判断进程里是不是旧模块 —— 改了对外接口就 bump, 并在 app.py 的
# _LLM_WANT 里同步. 背景见 scan.module_ver_issue 的注释(线上"新界面配旧模块"那次事故).
MODULE_VER = 3      # v3: 看图模型可在界面换+可测 / 秒拒快收敛 / 校对不再丢孩子的作答 2026-10-28
#                      v2: 加 error_code / auto_verify / _vision_call(on_wait) 2026-10-28


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
def _clean_secret(key: str, default: str = '') -> str:
    """
    读配置 + 自动清洗: 去首尾空白、剥掉误粘贴的引号(中英文)、去零宽字符/全角空格.
    真实案例: 在云端 Secrets 复制 Key 时带上了中文引号, 请求头编码直接炸出
    UnicodeEncodeError('ascii' codec ...), 而且报错原文完全看不出是 Key 的问题.
    """
    val = _read_secret(key, default)
    if not val:
        return val
    # 去掉首尾的各种引号(可能粘贴时连外面的引号一起复制了)
    val = val.strip().strip('"\'“”‘’「」『』《》')
    # 正文中间的不可见字符/全角空白也一并清掉
    for ch in ('\u200b', '\u200c', '\u200d', '\ufeff', '\u00a0', '\u3000'):
        val = val.replace(ch, '')
    return val.strip()


LLM_PROVIDER = _clean_secret('STUDY_LLM_PROVIDER', 'offline')  # offline / openai / deepseek / qwen
LLM_API_KEY = _clean_secret('STUDY_LLM_API_KEY', '')
LLM_BASE_URL = _clean_secret('STUDY_LLM_BASE_URL', '')
LLM_MODEL = _clean_secret('STUDY_LLM_MODEL', '')

# ---- 看图(Vision): 与讲题分开配置, 互不干扰 ----
# 讲题用 DeepSeek(纯文本) 完全够用; 看图必须换一个"带眼睛"的模型.
# 可选: 通义 qwen-vl-max / 智谱 glm-4v-flash / 硅基流动 Qwen2.5-VL / OpenAI gpt-4o-mini
VISION_API_KEY = _clean_secret('STUDY_VISION_API_KEY', '')
VISION_BASE_URL = _clean_secret('STUDY_VISION_BASE_URL', '')
VISION_MODEL = _clean_secret('STUDY_VISION_MODEL', '')


def vision_model() -> str:
    """
    当前真正用的看图模型名。

    顺序: 界面上选的(存在 settings 表) > Secrets 里的 STUDY_VISION_MODEL。
    为什么要多这一层: 模型名只能在 Secrets 里改, 改完要等云端重启; 而"这个模型今天排不上队"
    是随时会变的事 —— 让家长在「设置」里点两下就能换, 比教他改 TOML 靠谱。
    """
    try:
        import db
        ov = (db.get_setting('vision_model', '') or '').strip()
        if ov:
            return ov
    except Exception:
        pass
    return VISION_MODEL


def vision_host() -> str:
    """通道域名, 报错时说清楚"是哪家不给你名额" —— 只说'限流'等于什么都没说"""
    u = (VISION_BASE_URL or '').strip()
    for pre in ('https://', 'http://'):
        if u.startswith(pre):
            u = u[len(pre):]
    return u.split('/')[0] or '(未填 STUDY_VISION_BASE_URL)'


def set_vision_model(model: str):
    """界面换模型。传空串 = 回到 Secrets 里的默认值。"""
    import db
    m = (model or '').strip()
    if m:
        db.set_setting('vision_model', m)
    else:
        db.set_setting('vision_model', '')



def _non_ascii_cfg_problem() -> str:
    """
    检查视觉配置里是否仍有非英文字符(自动清洗救不回来的, 比如中间夹了汉字).
    返回人话问题描述; 没问题返回空串.
    """
    for name, val in (('STUDY_VISION_API_KEY', VISION_API_KEY),
                      ('STUDY_VISION_BASE_URL', VISION_BASE_URL),
                      ('STUDY_VISION_MODEL', vision_model())):
        if val and not val.isascii():
            return (f"{name} 里混入了非英文字符(多半是复制时带上了中文引号/汉字/全角空格)。"
                    "请到 Streamlit Cloud 的 Secrets 把这一行删掉重新粘贴, "
                    "值里只能有英文、数字、短横线和点。")
    return ''


def is_llm_enabled() -> bool:
    return LLM_PROVIDER != 'offline' and bool(LLM_API_KEY)


def vision_status() -> dict:
    """
    看图功能状态. 返回 {enabled, reason, model, base_url}
    reason 是给人看的话, UI 直接显示即可(避免"配置错了却提示成功"的假象)
    """
    _m = vision_model()
    if not VISION_API_KEY:
        return {'enabled': False, 'reason': '未配 Key',
                'model': _m or '(未填)', 'base_url': VISION_BASE_URL or '(未填)'}
    if not VISION_BASE_URL or not _m:
        return {'enabled': False,
                'reason': 'Key 有了, 但还缺 STUDY_VISION_BASE_URL 或 STUDY_VISION_MODEL',
                'model': _m or '(未填)', 'base_url': VISION_BASE_URL or '(未填)'}
    if 'deepseek' in VISION_BASE_URL.lower():
        return {'enabled': False,
                'reason': 'DeepSeek 不支持看图, 请换成通义/智谱/OpenAI/硅基流动等视觉模型',
                'model': _m, 'base_url': VISION_BASE_URL}
    bad = _non_ascii_cfg_problem()
    if bad:
        return {'enabled': False, 'reason': bad,
                'model': _m, 'base_url': VISION_BASE_URL}
    return {'enabled': True, 'reason': '已启用', 'model': _m, 'base_url': VISION_BASE_URL}


def is_vision_enabled() -> bool:
    return vision_status()['enabled']


_VISION_PROMPT = """你是"题目抄写员"。请只看图、照抄，不要动脑解题。

任务: 把图片里的题目原文【完整、逐字】抄录出来。

要求:
1) 只输出题目文字本身, 不要写"这道题是关于…"之类的话, 不要解题, 不要给答案。
2) 数学式子要尽量还原: 分数写成"分子/分母"或"½"都行, 乘号用 ×, 除号用 ÷, 平方写成 ^2 或 ²。
3) 手写体也要尽力辨认; 实在认不出的字用"□"代替, 不要瞎猜。
4) 有多道题就按"1) 2) 3)"分行写, 保留原来的题号。
5) 孩子写的解题过程/答案如果也在图里, 单独用一行"--- 孩子的作答 ---"隔开抄出来。
6) 输出简体中文正文, 不要 Markdown 代码块。"""


_VISION_VERIFY_PROMPT = """你是"校对员"。下面给你一张题目图片和一份从这张图抄出来的草稿。

任务: 把草稿和图片【逐字核对】, 输出修正后的最终题目文字。

核对要点:
1) 抄错的字、漏掉的字、多出来的字, 一律按图片修正
2) 数学式子重点核对: 数字、运算符号(× ÷ + -)、分数的分子分母、括号、指数/次数
3) 页面上不是题目本身的内容(如"班级""姓名""答题纸"这种印刷的表头), 如果草稿里有, 删掉
4) 分数写成"分子/分母", 乘号用 ×, 除号用 ÷
5) 只输出修正后的题目文字, 不要解释、不要 Markdown 代码块
6) 如果草稿已经完全正确, 就原样输出
7) 草稿末尾若有"--- 孩子的作答 ---"这一段, **必须原样保留**, 放在最后, 不要删、不要改
   （实测踩过: 校对那遍会把这一段整段吞掉, 错题本就只剩题干, 判错时看不到孩子怎么算的）"""


ANSWER_MARK = '--- 孩子的作答 ---'


def _keep_answer_block(draft: str, fixed: str) -> str:
    """
    校对完把"孩子的作答"补回来。

    实测(真卷 1023x767, 2026-10-28): 第一遍抄写会把孩子的算式单列成一段,
    第二遍校对只盯着"题目文字", 于是整段作答消失 —— 32B 和 30B 都这样。
    作答是错题本判断"错在哪"的核心信息, 丢了比抄错一个字严重得多, 所以代码兜一层。
    """
    if not draft or ANSWER_MARK not in draft:
        return fixed
    if ANSWER_MARK in (fixed or ''):
        return fixed
    tail = draft.split(ANSWER_MARK, 1)[1].strip()
    if not tail:
        return fixed
    return (fixed or '').rstrip() + '\n\n' + ANSWER_MARK + '\n' + tail


VISION_WAITS = (0, 15, 30)   # 首次 + 最多两次重试(等 15s / 30s)
# 被"秒拒"时的等待计划: 只有 0 和 5 秒, 合计约 7 秒就给结论.
# 判据来自实测(2026-10-28 13:14 线上那次 55.1 秒全失败):
#   55.1 - (15+30) = 10.1 秒 ≈ 3 次调用各 3 秒 —— 每次都是服务端立刻拒, 根本没有"排队"这回事.
#   对"模型今天没名额"(1305/429)和"模型名不存在"(20012)这两种, 干等 45 秒纯烧时间:
#   前者要换模型, 后者要改名, 等多久都不会变。
FAST_FAIL_WAITS = (0, 5)
REJECT_SECONDS = 2.5     # 2.5 秒内被拒 = 服务端直接拒, 不是"慢慢排到队尾"

# 超过这个面积就不做第二遍校对(约 1000x600).
# 实测(真卷单题框 1023x767 = 78 万像素, 2026-10-28):
#   Qwen3-VL-30B-A3B 单遍 11.4 秒 / 两遍 18.9 秒, 题目正文两次几乎一字不差;
#   而 32B 单遍 33 秒 / 两遍 226 秒(高峰排队), 二校那次反而把"孩子的作答"整段弄丢了。
# 结论: 大块图的第二遍校对收益 ≈ 0, 代价是配额和时间翻倍 —— 阈值从 90 万降到 60 万。
VERIFY_MAX_PIXELS = 600_000
VERIFY_MAX_BYTES = 90 * 1024   # 拿不到尺寸时按体积退而求其次


def auto_verify(nbytes: int, pixels: int = None) -> bool:
    """这张图还要不要跑第二遍校对. pixels 拿不到时按体积退化判断."""
    if pixels:
        return int(pixels) <= VERIFY_MAX_PIXELS
    return int(nbytes or 0) <= VERIFY_MAX_BYTES


def _pixels_of(image_bytes: bytes):
    """读图尺寸, 只为决定要不要校对; 读不出来就返回 None"""
    try:
        import io as _io
        from PIL import Image as _Image
        w, h = _Image.open(_io.BytesIO(image_bytes)).size
        return int(w) * int(h)
    except Exception:
        return None


def _vision_call(client, on_wait=None, **kw):
    """
    带限流重试的视觉模型调用.

    两种失败要区别对待(2026-10-28 线上实测后改的):
    - **真排队**(429, 慢): 服务端让你等, 等 15/30 秒重试 1-2 次有意义;
    - **秒拒**(1305/1302 或模型名不存在, 2.5 秒内回): 这是"这条路走不通", 不是"排在队尾"。
      前者要换模型, 后者要改名, 等 45 秒只会让孩子盯着白屏。
    所以第一次就被秒拒时, 后面只再等 5 秒试一次, 合计约 7 秒给结论。

    on_wait(attempt, seconds): 进入等待前回调. 界面靠它显示"正在排队, 还有 N 秒".
    """
    import time as _time
    plan = list(VISION_WAITS)
    idx = 0
    while True:
        w = plan[idx]
        if w:
            if on_wait:
                try:
                    on_wait(idx, w)
                except Exception:
                    pass        # 界面回调炸了不能影响重试本身
            _time.sleep(w)
        t0 = _time.time()
        try:
            return client.chat.completions.create(**kw)
        except Exception as e:
            dt = _time.time() - t0
            code = error_code(str(e))
            idx += 1
            if idx >= len(plan):
                raise
            if idx == 1 and code in ('rate_limit', 'model_missing') and dt < REJECT_SECONDS:
                plan = list(FAST_FAIL_WAITS)   # 秒拒: 不再硬等 15/30
                idx = 1
                continue
            if code not in ('rate_limit', 'timeout'):
                raise          # 鉴权/余额/模型名错: 重试一百次也一样


def error_code(msg: str) -> str:
    """把异常归成几类, 界面据此给不同的下一步建议(别用一句'限流'糊所有情况)"""
    m = (msg or '').lower()
    # 模型名不存在: 硅基流动 20012 / OpenAI 风格 model_not_found / 各家 404 文案
    if ('model does not exist' in m or '20012' in m or 'model_not_found' in m
            or 'model not found' in m or 'invalid model' in m or 'no such model' in m
            or ('does not exist' in m and 'model' in m)):
        return 'model_missing'
    if '429' in m or '1302' in m or '1305' in m or 'rate limit' in m or '访问量过大' in m:
        return 'rate_limit'
    if 'timeout' in m or 'timed out' in m:
        return 'timeout'
    if '401' in m or 'invalid_api_key' in m or 'authentication' in m:
        return 'auth'
    if '402' in m or 'insufficient balance' in m:
        return 'balance'
    if 'ascii' in m and 'codec' in m:
        return 'bad_secret'
    return 'other'


def read_image(image_bytes: bytes, mime: str = 'image/jpeg', subject: str = '通用',
               verify: bool = None, on_wait=None) -> dict:
    """
    AI 看图: 把图片交给视觉模型抄出题目原文.

    verify=None 时按体积自动决定: 小图(单题裁剪块)校对两遍, 大图(整页)只抄一遍.
    手写题第一遍总会有几个字认岔, 校对一遍能拦掉大部分; 但整页图发两遍的代价是
    配额翻倍、时间翻倍, 而服务端高峰本来就容易 429 —— 先保证能出结果.

    on_wait(attempt, seconds): 等配额时的回调, 给界面显示进度用.

    返回: {'success': bool, 'text': str, 'error': str|None, 'code': str, 'model': str}
    """
    stt = vision_status()
    _model = vision_model()
    if not stt['enabled']:
        return {'success': False, 'text': '', 'error': f"看图功能未启用: {stt['reason']}",
                'code': 'disabled', 'model': stt['model']}
    if not image_bytes:
        return {'success': False, 'text': '', 'error': '图片为空',
                'code': 'empty', 'model': stt['model']}
    bad = _non_ascii_cfg_problem()
    if bad:
        return {'success': False, 'text': '', 'error': bad,
                'code': 'bad_secret', 'model': stt['model']}
    if verify is None:
        verify = auto_verify(len(image_bytes), _pixels_of(image_bytes))
    try:
        from openai import OpenAI
        import base64 as _b64
        client = OpenAI(api_key=VISION_API_KEY, base_url=VISION_BASE_URL or None)
        b64 = _b64.b64encode(image_bytes).decode('utf-8')
        img_part = {"type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"}}

        # ---- 第一遍: 抄 ----
        resp = _vision_call(
            client,
            on_wait=on_wait,
            model=_model,
            messages=[
                {"role": "system", "content": _VISION_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": f"学科: {subject}\n请把图里的题目原文抄出来。"},
                    img_part,
                ]},
            ],
            temperature=0.0,
            timeout=90,
        )
        text = (resp.choices[0].message.content or '').strip()
        if not text:
            return {'success': False, 'text': '', 'error': '模型返回空内容',
                    'code': 'empty_result', 'model': _model}

        # ---- 第二遍: 校对(失败不影响结果, 退回第一遍草稿) ----
        verified = False
        if verify:
            try:
                resp2 = _vision_call(
                    client,
                    on_wait=on_wait,
                    model=_model,
                    messages=[
                        {"role": "system", "content": _VISION_VERIFY_PROMPT},
                        {"role": "user", "content": [
                            {"type": "text",
                             "text": f"学科: {subject}\n—— 第一遍抄写的草稿 ——\n{text}\n\n"
                                     "请和原图逐字核对, 输出修正后的最终题目文字。"},
                            img_part,
                        ]},
                    ],
                    temperature=0.0,
                    timeout=90,
                )
                fixed = (resp2.choices[0].message.content or '').strip()
                if fixed:
                    text = _keep_answer_block(text, fixed)
                    verified = True
            except Exception:
                pass  # 校对挂了不算失败, 用第一遍结果

        return {'success': True, 'text': text, 'error': None, 'code': 'ok',
                'verified': verified, 'model': _model}
    except Exception as e:
        raw = str(e)
        return {'success': False, 'text': '', 'error': _humanize_error(raw),
                'code': error_code(raw), 'model': _model}


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


def _humanize_error(msg: str, model: str = '') -> str:
    """把 LLM 原始异常翻译成人话。

    措辞只说我们确定的事：
    - 撞了限流就说"服务商名额"，不说"免费档"——型号可能是付费的，说错会误导排查方向；
    - 超时就说超时，别混进限流里（两者下一步动作不同：超时该重试，鉴权错重试一百次也没用）；
    - **必须带上"哪家通道 + 哪个模型 + 服务端原话"**：只说"太挤"，家长没法判断是网络、
      是模型名写错了、还是该换模型。2026-10-28 那次就是一句"排队中"把真正原因盖住了。
    """
    code = error_code(msg)
    _m = (model or vision_model())
    _host = vision_host()
    raw = (msg or '').replace('\n', ' ')[:120]
    if code == 'bad_secret':
        return ("请求里混入了非英文字符, 发不出去 — 多半是 Key/接口地址复制时带上了"
                "中文引号或全角空格。请到 Secrets 检查 STUDY_VISION_* 的值, "
                "只保留英文/数字/短横线。")
    if code == 'balance':
        return ("服务商账户余额不足 — 请到服务商后台充值后再试 "
               "(DeepSeek 最低充 10 元, 按用量扣费)")
    if code == 'auth':
        return f"API Key 无效或已失效（{_host}）— 请检查 STUDY_VISION_API_KEY 是否填错/被删除"
    if code == 'model_missing':
        return (f"看图模型名不对：{_host} 上没有「{_m}」这个模型。"
                f"服务端原话：{raw}。\n"
                "→ 到「⚙️ 设置 → 看图模型」点「测一下」，选一个能用的名字（换模型不用改 Secrets）。")
    if code == 'rate_limit':
        return (f"「{_m}」当前没名额（{_host} 返回排队/限流，服务端原话：{raw}）。\n"
                "→ 这不是网络问题，也不是你操作错了：这类共享配额的型号高峰期基本排不上队，"
                "干等也没用。程序已自动重试过，仍然进不去 —— **换一个模型比等更有效**："
                "⚙️ 设置 → 看图模型 → 测一下。")
    if code == 'timeout':
        return (f"请求超时（{_m} @ {_host}）— 服务商这会儿响应慢，隔一会儿点「只重认这道」重试即可")
    if '404' in msg:
        return (f"接口地址或模型名不对（{_host} / {_m}）— "
                "请检查 STUDY_VISION_BASE_URL 和 STUDY_VISION_MODEL")
    return f"{raw}（通道 {_host} / 模型 {_m}）"


def self_test(model: str = '', timeout: float = 25) -> dict:
    """
    「测一下看图」：用一张内置小图真打一次请求，把**真实结果**原样带回来。

    为什么要有: 2026-10-28 那天，家长是"孩子拍完卷子、等 55 秒、只看到一句排队中"，
    才知道看图坏了。这种信息不该等到使用时才暴露。现在点一下按钮，1~20 秒内拿到
    OK/错在哪，且不影响错题本里的任何数据。

    返回 {'ok', 'seconds', 'model', 'code', 'error', 'text', 'host'}
    """
    import io as _io
    import time as _time
    _m = (model or vision_model()).strip()
    _host = vision_host()
    stt = vision_status()
    if not stt['enabled']:
        return {'ok': False, 'seconds': 0.0, 'model': _m, 'code': 'disabled',
                'error': f"看图没开启：{stt['reason']}", 'text': '', 'host': _host}
    try:
        from PIL import Image as _Image, ImageDraw as _ImageDraw
        im = _Image.new('RGB', (320, 120), 'white')
        d = _ImageDraw.Draw(im)
        d.text((16, 20), '2 + 3 = 5', fill='black')      # 默认字体即可, 只验证"能不能看图"
        d.text((16, 60), 'TEST 1 2 3', fill='black')
        buf = _io.BytesIO()
        im.save(buf, 'PNG')
        img = buf.getvalue()
    except Exception:
        img = None
    if not img:
        return {'ok': False, 'seconds': 0.0, 'model': _m, 'code': 'other',
                'error': '生成测试图失败（Pillow 有问题）', 'text': '', 'host': _host}
    t0 = _time.time()
    try:
        from openai import OpenAI
        import base64 as _b64
        client = OpenAI(api_key=VISION_API_KEY, base_url=VISION_BASE_URL or None)
        r = client.chat.completions.create(
            model=_m,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "图里写的算式是什么？只把算式原样输出。"},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + _b64.b64encode(img).decode()}},
            ]}],
            temperature=0.0, timeout=timeout,
        )
        txt = (r.choices[0].message.content or '').strip()
        dt = round(_time.time() - t0, 1)
        ok = '5' in txt
        return {'ok': ok, 'seconds': dt, 'model': _m, 'code': 'ok' if ok else 'weird',
                'error': '' if ok else f"连上了，但回的内容不对：{txt[:60]}",
                'text': txt[:120], 'host': _host}
    except Exception as e:
        dt = round(_time.time() - t0, 1)
        return {'ok': False, 'seconds': dt, 'model': _m, 'code': error_code(str(e)),
                'error': _humanize_error(str(e), _m), 'text': '', 'host': _host}


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
        'vision': vision_status(),
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
