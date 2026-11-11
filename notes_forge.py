"""
笔记工坊（notes-forge）：把一份课堂笔记 PDF 自动变成笔记平台的纪要数据。

流水线：
  1. ingest        渲染每页为 JPG + 提取印刷文字
  2. profile_pages 逐页用看图模型判断「这页在讲什么、是什么角色」
  3. structure     用文本模型出骨架（一分钟总结 / 思维导图 / 模块目录 + 页码归属）
  4. content       逐模块写正文 blocks
  5. examples      逐例题写题干 / 步骤 / 方法 / 陷阱
  6. quiz          出雷区 + 自测题
  7. validate      结构断言，不合格把错误反馈给模型重试一次
  8. write         写 lesson-XX.js / board-map.js / index.js，平台立即可见

设计原则（照果果爸的要求）：
  - 不许静默降级：任何一页看图失败、任何一次 JSON 解析失败，都要显式记进 report 并在界面显示
  - 结构断言先于写盘：不合格的数据绝不落盘，避免「页面看着有、内容其实是空的」
  - 页码必须真实存在：映射到不存在的页直接报错
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass
# pymupdf 是懒加载的：import 本模块不需要装它，只有真正跑 ingest 才 import。
# 这样"笔记工坊"页面在依赖没装好时也能给出人话提示，而不是整站崩。

# ============================================================
# 配置（与 llm.py 同一套键：STUDY_LLM_* / STUDY_VISION_*，
# 本地走 .streamlit/secrets.toml，云端走 Cloud Secrets）
# ============================================================


def cfg(key: str, default: str = '') -> str:
    """环境变量 > Streamlit secrets。顺手清掉误粘贴的引号和零宽字符。"""
    val = os.environ.get(key, '')
    if not val:
        try:
            import streamlit as st
            if hasattr(st, 'secrets') and key in st.secrets:
                val = str(st.secrets[key])
        except Exception:
            pass
    if not val:
        return default
    val = val.strip().strip('"\'“”‘’「」『』《》')
    for ch in ('\u200b', '\u200c', '\u200d', '\ufeff', '\u00a0', '\u3000'):
        val = val.replace(ch, '')
    return val.strip() or default


def llm_cfg() -> dict:
    return {
        'key': cfg('STUDY_LLM_API_KEY'),
        'base': cfg('STUDY_LLM_BASE_URL', 'https://api.deepseek.com'),
        'model': cfg('STUDY_LLM_MODEL', 'deepseek-chat'),
    }


def vision_cfg() -> dict:
    return {
        'key': cfg('STUDY_VISION_API_KEY'),
        'base': cfg('STUDY_VISION_BASE_URL', 'https://api.siliconflow.cn/v1'),
        'model': cfg('STUDY_VISION_MODEL', 'Qwen/Qwen3-VL-32B-Instruct'),
    }


def cfg_status() -> dict:
    l, v = llm_cfg(), vision_cfg()
    return {
        'text_ready': bool(l['key']),
        'vision_ready': bool(v['key']),
        'text_model': l['model'],
        'vision_model': v['model'],
        'text_base': l['base'],
        'vision_base': v['base'],
    }


# ============================================================
# 通用工具
# ============================================================

def extract_json(text: str):
    """从模型回复里抠出 JSON。模型经常带 ```json 围栏或前后废话。"""
    if not text:
        return None
    s = text.strip()
    s = re.sub(r'^```(?:json)?\s*', '', s)
    s = re.sub(r'\s*```$', '', s)
    try:
        return json.loads(s)
    except Exception:
        pass
    # 从第一个 { 到最后一个 } 硬抠（应对前后有解释文字）
    i, j = s.find('{'), s.rfind('}')
    if i >= 0 and j > i:
        try:
            return json.loads(s[i:j + 1])
        except Exception:
            pass
    return None


def _salvage_array(text: str, field: str) -> list:
    """
    从可能被 max_tokens 截断的回复里，抢救出数组里**已经完整**的对象。
    实测过：一次要 5 道例题的长 JSON 被截断，整段解析失败 → 全部丢掉，
    但前 3 道其实是完整的。这个兜底能把它们捞回来，而不是整批作废。
    """
    i = text.find('"' + field + '"')
    if i < 0:
        return []
    j = text.find('[', i)
    if j < 0:
        return []
    out, depth, start, in_str, esc = [], 0, None, False, False
    for k in range(j + 1, len(text)):
        ch = text[k]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            if depth == 0:
                start = k
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    out.append(json.loads(text[start:k + 1]))
                except Exception:
                    pass
                start = None
    return out


def _retry(fn, tries: int = 3, waits=(0, 3, 8), on_log=None):
    """统一重试：网络抖动 / 429 / 解析失败都走这里，最终失败把最后一次异常抛出去。"""
    last = None
    for i in range(tries):
        if i:
            time.sleep(waits[min(i, len(waits) - 1)])
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if on_log:
                on_log(f'  第 {i + 1} 次失败：{e}')
    raise last  # type: ignore[misc]


# ============================================================
# 1. 入库：渲染页面 + 提取文字
# ============================================================

RENDER_ZOOM = 1.5
JPEG_Q = 72
CACHE_VER = 3   # 识别 prompt 变了就 bump；缓存 key 里带这个版本号


def ingest(pdf_bytes: bytes, lesson_key: str, dist_dir: Path, on_log=None,
           gallery_sub: str | None = None) -> dict:
    """把 PDF 拆成每页 JPG + 提取印刷文字。图片字节直接放进返回值，不依赖磁盘。"""
    import pymupdf  # PyMuPDF（懒加载）
    dist_dir = Path(dist_dir)
    if not gallery_sub:
        gallery_sub = 'L' + lesson_key.replace('lesson', '').lstrip('0')
    out_dir = dist_dir / 'pages' / gallery_sub
    out_dir.mkdir(parents=True, exist_ok=True)
    # 清掉上一次的同名残留，防止页数变少时旧图还留着。
    # 删不掉不许中断：某些环境（沙箱/回收站不可用/文件被预览器占用）会拒绝删除，
    # 但同名文件本来就会被覆盖，删不掉最多是"多出几张旧图"，不该让整次生成失败。
    stale = []
    for old in out_dir.glob('p*.jpg'):
        try:
            old.unlink()
        except Exception as e:  # noqa: BLE001
            stale.append(old.name)
    if stale and on_log:
        on_log(f'  ⚠ 有 {len(stale)} 张旧图没删掉（环境不允许删除），同名图会被覆盖')

    doc = pymupdf.open(stream=pdf_bytes, filetype='pdf')
    pages = []
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=pymupdf.Matrix(RENDER_ZOOM, RENDER_ZOOM))
        jpg = pix.tobytes('jpeg', jpg_quality=JPEG_Q)
        fp = out_dir / f'p{i + 1:02d}.jpg'
        fp.write_bytes(jpg)
        # 另存一张小图专供识别用：宽约 960px，token 约为大图的 1/4，快一倍多。
        # 大图留给页面上放大看（字要清楚），识别不需要那么高的分辨率。
        small = page.get_pixmap(matrix=pymupdf.Matrix(1.0, 1.0))
        pages.append({
            'no': i + 1,
            'text': page.get_text().strip(),
            'img': jpg,
            'img_small': small.tobytes('jpeg', jpg_quality=70),
        })
    if on_log:
        total_kb = sum(len(p['img']) for p in pages) // 1024
        on_log(f'渲染完成：{len(pages)} 页，共 {total_kb} KB → pages/{gallery_sub}/')
    return {'pages': pages, 'gallery_dir': f'pages/{gallery_sub}', 'count': len(pages)}


# ============================================================
# 2. 逐页理解（看图模型）
# ============================================================

VISION_SYS = (
    '你是初中数学教研老师，正在读一份课堂笔记的截图。'
    '你会如实描述页面上有什么，不猜测、不补充页面上没有的内容。只输出 JSON。'
)

VISION_PROMPT = """这是《{course}》**{title}**这一讲的第 {no} 页课堂笔记截图。请判断这一页的内容。

只输出这个 JSON（不要 markdown 围栏、不要解释）：
{{
  "role": "cover | toc | divider | content | example | practice | summary | blank",
  "belongs": "this | prev",
  "topic": "12 字以内说明这页在讲什么（例：去分母示范）",
  "examples": ["例2", "练1"],
  "points": ["这页上的知识点或老师板书的要点，每条不超过 30 字，没有就给空数组"],
  "handwriting": true,
  "page_text": "页面上出现的印刷标题文字，原样抄，没有就空字符串"
}}

role 的判据（严格按这个来）：
- cover       只有课程名/大标题/一张插图，没有任何知识点（如「全章重点回顾」这种大字封面）
- toc         只有目录、表格里列着章节名
- divider     只有专题标题，用来分隔板块（如「专题一 定义新运算」）
- example     页面上有一道具体例题的题目或解答
- practice    课堂练习页
- summary     总结/回顾，有实质内容
- blank       空白页或几乎没内容
- content     其余有实质知识点的页

belongs 的判据（这一步很重要，判错会把上一讲的题算进这一讲）：
本讲主题是「{title}」。
- prev   这一页讲的**不是本讲主题**，而是上一讲的内容（课堂笔记开头常留着上一讲的尾巴）
- this   其余全部（包括本讲封面、目录）

examples 只填**这一页上真正作为题目出现的题号**（例1/例2/练1 这种），
不要填页面上只是"提到"的题号。如果这一页 role 是 toc / cover / divider / blank，examples 一定是 []。"""


# ------------------------------------------------------------
# 科目配置
# ------------------------------------------------------------
# 为什么要有这一层：三个科目「例题」的含义根本不同——
#   数学：一道可解的题，答案唯一，步骤是推导；
#   语文：一道阅读/名著题，答案是"得分点"，步骤是答题句式；
#   英语：阅读/填空/翻译题，答案是英文原词，步骤是定位与同义替换。
# 一套 prompt 硬套三科，会写出"先移项再合并"这种语文里不存在的步骤。
# 只覆盖需要变的键，数学保持原样（原 prompt 已在实战中调过，不动）。
SUBJECTS = {
    'math': {
        'label': '数学',
        'min_sections': 6,
        'example_prefix': ('例', '练'),
    },
    'chinese': {
        'label': '语文',
        'min_sections': 5,
        'example_prefix': ('例', '练习', '练', '题'),
        'vision_sys': (
            '你是初中语文教研老师，正在读一份课堂笔记（讲义 / PPT）的截图。'
            '页面上可能有老师红笔批注。你会如实描述页面上有什么，不猜测、不补充页面上没有的内容。只输出 JSON。'
        ),
        'vision_prompt': """这是《{course}》**{title}**这一讲的第 {no} 页课堂笔记截图。请判断这一页的内容。

只输出这个 JSON（不要 markdown 围栏、不要解释）：
{{
  "role": "cover | toc | divider | content | example | practice | summary | blank",
  "belongs": "this | prev",
  "topic": "12 字以内说明这页在讲什么（例：人物形象分析）",
  "examples": ["例2", "练1"],
  "points": ["这页上的考点、答题方法或老师批注要点，每条不超过 30 字，没有就给空数组"],
  "handwriting": true,
  "page_text": "页面上出现的印刷标题文字，原样抄，没有就空字符串"
}}

role 的判据（严格按这个来）：
- cover       只有课程名/大标题/一张插图，没有任何实质内容
- toc         只有目录、表格里列着章节名
- divider     只有专题标题，用来分隔板块（如「三、两个核心片段」）
- example     页面上有一道具体的阅读题 / 名著题 / 作文题（含题干）
- practice    课堂练习页
- summary     总结/回顾，有实质内容
- blank       空白页或几乎没内容
- content     其余有实质内容的页（考点归纳、方法讲解、范文批注、知识表格）

belongs 的判据（很重要，判错会把上一讲的内容算进这一讲）：
本讲主题是「{title}」。
- prev   这一页讲的**不是本讲主题**，而是上一讲的内容
- this   其余全部（包括本讲封面、目录）

examples 只填**这一页上真正作为题目出现的编号**（例1 / 练1 / 第3题），
不要填页面上只是"提到"的题。如果这一页 role 是 toc / cover / divider / blank，examples 一定是 []。""",
        'struct_sys': (
            '你是资深初中语文教研员，为初一学生整理课堂笔记纪要。'
            '读者是学生本人：话要短、要具体，讲清"这题怎么答、分怎么给、哪里会丢分"，不要空话套话。'
            '只输出 JSON。'
        ),
        'struct_prompt': """下面是一节初中语文课《{title}》全部 {n} 页课堂笔记的逐页摘要。

{digest}

请设计这份纪要的骨架。要求：
1. minute：一分钟总结。headline 一句话说清这讲的核心；points 恰好 5 条，每条 60~110 字，必须点名具体的考点细节（名著就点人物/情节/回目，阅读就点题型和答题句式，用 <b></b> 强调关键词）；closing 一句话收尾。
2. mindmap：center 是主题；branches 6~8 个，每个 {{"t": 分支名(≤8字), "sub": [2~3 条要点(≤20字)], "link": 对应模块 id}}；最常考的分支加 "hot": true。
3. sections：必须 5~8 个模块，按考点块 / 题型块拆（名著按「版本与结构 → 人物 → 核心情节 → 主题与义 → 答题模板」拆；现代文阅读按文体与题型拆；作文按审题、选材、结构、语言拆），按上课顺序。每个 {{"id": "sec-英文短语", "title": "中文标题", "level": "框架|考点|方法|易错|素材|压轴", "pages": [这页属于本模块的页码...]}}。
   - pages 只能从上面出现过的页码里选，且每页最多归属一个模块
   - 纯封面页（role=cover/toc/divider/blank）不要放进任何模块
   - 标了【上一讲，别用】的页不要放进任何模块，也不要按它们建模块
   - 例题页（role=example）不要放进模块，它们会单独成卡片

只输出：
{{"minute": {{"seconds": 60, "headline": "", "points": ["","","","",""], "closing": ""}},
  "mindmap": {{"center": "", "branches": []}},
  "sections": []}}""",
        'section_sys': (
            '你是初中语文老师，在给学生写课堂笔记。'
            '语言要像老师当面讲：先说这类题怎么考，再说怎么答（给可套用的句式），最后点一句最容易丢分的地方。'
            '禁止空话（"要认真阅读""要体会情感"这类）。只输出 JSON。'
        ),
        'section_prompt': """课程：《{title}》
本模块：{sec_title}（层级：{level}）

这一模块覆盖的页面摘要：
{page_info}

这些页上的详细内容（印刷文字 + 老师板书/批注要点）：
{page_text}

写这一模块的正文 blocks。要求：
- 3~6 个 block，按讲解顺序
- 每个 block 是 {{"t": 类型, "v": 内容}}，类型只能是：
  p（普通段落）/ list（v 是字符串数组）/ formula（答题句式，用 ~{{分子}}{{分母}} 会渲染成分数，语文一般不用）/ board（板书/批注还原）/ tip（老师口传的答题口诀）/ trap（易错警示）/ table（需 head + rows）
- 名著题必须引用原文情节与人物，不要编造原文没有的细节
- 阅读题要给"答题句式"，例如：运用了……描写，写出了……，表现了……
- 至少有一个 tip（答题口诀/模板）或 trap（丢分点）
- 总字数 200~500 字，宁可短而准

只输出 {{"blocks": [...]}}""",
        'ex_sys': (
            '你是资深初中语文教研员，正在还原课上讲过的题（阅读题 / 名著简答题 / 作文升格）。'
            '答案要按得分点分条写，不写空泛套话；答题步骤要能照着套。只输出 JSON。'
        ),
        'ex_prompt': """课程：《{title}》

下面列了 {n} 道题（编号由我给，你不要改）。每道题后面是它对应的板书/讲义页内容：

{items}

请按同样的顺序（idx 从 0 开始）整理每道题的内容，不要增删题目、不要合并。
每道题：
{{"idx": 0, "title": "题型或考点(≤14字)",
  "problem": "题干原文（材料关键句照抄，可节选）",
  "answer": "答案要点，分条写，每条一句话",
  "steps": ["答题步骤/得分点1", "..."],
  "method": "这类题的答题模板或口诀（≤60字）",
  "trap": "最容易丢分的地方（≤60字）"}}

只输出 {{"examples": [...]}}""",
        'quiz_sys': (
            '你是资深初中语文老师，在给学生出这一讲的复习自测。'
            '题目要考"会不会答偏、会不会漏得分点"，不要出与考点无关的题。只输出 JSON。'
        ),
        'quiz_prompt': """课程：《{title}》

这一讲的全部内容摘要：
{digest}

请出 8 道自测题。要求：
- 5 道 choice（单选，4 个选项），3 道 fill（填空，答案是关键词语，可能有多种写法）
- 每题格式：
  choice: {{"id": "q1", "type": "choice", "q": "题干", "options": ["A","B","C","D"], "answer": 正确选项的下标(0~3), "explain": "为什么，并指出错误选项错在哪"}}
  fill:   {{"id": "q6", "type": "fill", "q": "题干（要填的空用 ____ 表示）", "answer": ["正确答案", "另一种等价写法"], "explain": "解析，说清得分点"}}
- 每题都要有 explain，且 explain 要能自证
- 选择题的正确项必须唯一且明确，错误项要有真实迷惑性（例如只答表层、张冠李戴的人物、错回目）

只输出 {{"quiz": [...]}}""",
        'pitfall_prompt': """课程：《{title}》

这一讲的全部内容摘要：
{digest}

请总结 7 条雷区（这一讲最容易失分的地方）。每条：
{{"title": "坑的名字(≤10字)", "body": "具体错在哪 + 怎么改(60~100字，尽量引用原文或给出替换句式)"}}

只输出 {{"pitfalls": [...]}}""",
    },
    'english': {
        'label': '英语',
        'min_sections': 5,
        'example_prefix': ('例', '练习', '练', '题', 'Task', 'Part', 'Ex'),
        'vision_sys': (
            '你是初中英语教研老师，正在读一份英语课堂笔记（PPT）的截图。'
            '页面上多为英文，也可能有中文讲解或红色答案。英文原样抄，不翻译、不猜测。只输出 JSON。'
        ),
        'vision_prompt': """这是《{course}》**{title}**这一讲的第 {no} 页课堂笔记截图。请判断这一页的内容。

只输出这个 JSON（不要 markdown 围栏、不要解释）：
{{
  "role": "cover | toc | divider | content | example | practice | summary | blank",
  "belongs": "this | prev",
  "topic": "12 字以内说明这页在讲什么（例：预测后续情节）",
  "examples": ["Task 2", "练习1"],
  "points": ["这页上的语言点/阅读技巧/答案要点，每条不超过 30 字，英文可保留英文，没有就给空数组"],
  "handwriting": true,
  "page_text": "页面上出现的印刷标题文字，原样抄，没有就空字符串"
}}

role 的判据（严格按这个来）：
- cover       只有单元号/大标题/一张插图，没有实质内容
- toc         只有目录或 Part 列表
- divider     只有 Part 标题（如「Part 1 Read & Explore」）
- example     页面上有一道具体的题（阅读理解题、填空题、翻译练习）
- practice    课堂练习页
- summary     总结/回顾，有实质内容
- blank       空白页或几乎没内容
- content     其余有实质内容的页（课文、词汇讲解、语法讲解、表格）

belongs 的判据（很重要，判错会把上一讲的内容算进这一讲）：
本讲主题是「{title}」。
- prev   这一页讲的**不是本讲主题**，而是上一讲的内容
- this   其余全部（包括本讲封面、目录）

examples 只填**这一页上真正作为题目出现的编号**（Task 2 / 练习1 / 1.），
不要填页面上只是"提到"的。如果这一页 role 是 toc / cover / divider / blank，examples 一定是 []。""",
        'struct_sys': (
            '你是资深初中英语教研员，为初一学生整理阅读课笔记纪要。'
            '读者是学生本人：中文讲清"这条技巧什么时候用、考试怎么考"，英文原句和词块保留英文。'
            '只输出 JSON。'
        ),
        'struct_prompt': """下面是一节初中英语课《{title}》全部 {n} 页课堂笔记的逐页摘要。

{digest}

请设计这份纪要的骨架。要求：
1. minute：一分钟总结。headline 一句话说清这讲的核心；points 恰好 5 条，每条 60~110 字，中文为主，必须点名具体的词、短语、题型（英文词块原样写，用 <b></b> 强调关键词）；closing 一句话收尾。
2. mindmap：center 是主题；branches 6~8 个，每个 {{"t": 分支名(≤8字), "sub": [2~3 条要点(≤20字)], "link": 对应模块 id}}；最常考的分支加 "hot": true。
3. sections：必须 5~8 个模块，按 Part / 阅读技巧 / 词汇 / 表达 / 语法 / 练习拆，按上课顺序。每个 {{"id": "sec-英文短语", "title": "中文标题（可带英文）", "level": "框架|词汇|技巧|语法|易错|压轴", "pages": [这页属于本模块的页码...]}}。
   - pages 只能从上面出现过的页码里选，且每页最多归属一个模块
   - 纯封面页（role=cover/toc/divider/blank）不要放进任何模块
   - 标了【上一讲，别用】的页不要放进任何模块，也不要按它们建模块
   - 例题页（role=example）不要放进模块，它们会单独成卡片

只输出：
{{"minute": {{"seconds": 60, "headline": "", "points": ["","","","",""], "closing": ""}},
  "mindmap": {{"center": "", "branches": []}},
  "sections": []}}""",
        'section_sys': (
            '你是初中英语老师，在给学生写课堂笔记。中文讲解 + 英文原句/词块。'
            '每个语言点要说清"什么时候用、考试怎么考"，阅读技巧要写成可执行的动作。'
            '禁止空话（"要多读多背"这类）。只输出 JSON。'
        ),
        'section_prompt': """课程：《{title}》
本模块：{sec_title}（层级：{level}）

这一模块覆盖的页面摘要：
{page_info}

这些页上的详细内容（印刷文字 + 老师板书/答案要点）：
{page_text}

写这一模块的正文 blocks。要求：
- 3~6 个 block，按讲解顺序
- 每个 block 是 {{"t": 类型, "v": 内容}}，类型只能是：
  p（普通段落）/ list（v 是字符串数组）/ formula（句型结构，如 be filled with + n.）/ board（板书还原）/ tip（考试技巧）/ trap（易错警示）/ table（需 head + rows，适合词表）
- 英文原句照抄，中文解释跟在后面；词汇给「词性 + 中文释义」
- 阅读技巧要写成动作链：先圈题干关键词 → 回原文定位 → 比对同义替换
- 至少有一个 tip（考试怎么考）或 trap（易错点：时态、词性、中式表达）
- 总字数 200~450 字，宁可短而准

只输出 {{"blocks": [...]}}""",
        'ex_sys': (
            '你是资深初中英语教研员，正在还原英语课上的阅读题 / 填空题 / 翻译练习。'
            '答案用英文原词，解析讲清定位依据和语法理由。只输出 JSON。'
        ),
        'ex_prompt': """课程：《{title}》

下面列了 {n} 道题（编号由我给，你不要改）。每道题后面是它对应的板书/讲义页内容：

{items}

请按同样的顺序（idx 从 0 开始）整理每道题的内容，不要增删题目、不要合并。
每道题：
{{"idx": 0, "title": "题型(≤14字)",
  "problem": "题干原文（英文原句照抄，要填的空用 ____ 表示）",
  "answer": "英文答案（多个空用 → 分隔）",
  "steps": ["解题思路1：圈出题干关键词", "..."],
  "method": "阅读技巧或语法规则（≤60字）",
  "trap": "最容易错的地方（≤60字，如时态、词性、中式表达）"}}

只输出 {{"examples": [...]}}""",
        'quiz_sys': (
            '你是资深初中英语老师，在给学生出这一讲的复习自测。'
            '考词义辨析、细节定位、主旨判断、语法形式，不要出超纲内容。只输出 JSON。'
        ),
        'quiz_prompt': """课程：《{title}》

这一讲的全部内容摘要：
{digest}

请出 8 道自测题。要求：
- 5 道 choice（单选，4 个选项），3 道 fill（填空，答案是英文单词或短语）
- 每题格式：
  choice: {{"id": "q1", "type": "choice", "q": "题干", "options": ["A","B","C","D"], "answer": 正确选项的下标(0~3), "explain": "为什么，指出错误选项错在哪（中文）"}}
  fill:   {{"id": "q6", "type": "fill", "q": "题干（要填的空用 ____ 表示）", "answer": ["标准答案", "可接受的其他写法"], "explain": "解析（中文），说清语法理由"}}
- fill 的答案要给全可接受写法（如 ["filled with", "be filled with"]），大小写不敏感
- 每题都要有 explain，且 explain 要能自证
- 题干要具体，答案唯一确定

只输出 {{"quiz": [...]}}""",
        'pitfall_prompt': """课程：《{title}》

这一讲的全部内容摘要：
{digest}

请总结 7 条雷区（这一讲最容易错的地方）。每条：
{{"title": "坑的名字(≤10字)", "body": "具体错在哪 + 怎么避免(60~100字，举英文例子更好)"}}

只输出 {{"pitfalls": [...]}}""",
    },
}


def subject_cfg(subject: str | None) -> dict:
    """拿科目配置；未知科目落回数学，绝不抛异常（配置错不该阻断生成）。"""
    return SUBJECTS.get((subject or 'math').strip().lower()) or SUBJECTS['math']


def profile_one(page: dict, course: str, no: str, title: str = '', on_log=None,
                subject: str = 'math') -> dict:
    v = vision_cfg()
    if not v['key']:
        raise RuntimeError('看图模型未配置：需要 STUDY_VISION_API_KEY')
    SC = subject_cfg(subject)

    def _call():
        from openai import OpenAI
        client = OpenAI(api_key=v['key'], base_url=v['base'] or None, timeout=180)
        b64 = base64.b64encode(page.get('img_small') or page['img']).decode()
        resp = client.chat.completions.create(
            model=v['model'],
            messages=[
                {'role': 'system', 'content': SC.get('vision_sys') or VISION_SYS},
                {'role': 'user', 'content': [
                    {'type': 'text', 'text': (SC.get('vision_prompt') or VISION_PROMPT).format(
                        course=course, no=no, title=title or '（未提供）')},
                    {'type': 'image_url',
                     'image_url': {'url': f'data:image/jpeg;base64,{b64}'}},
                ]},
            ],
            temperature=0.1,
            # 语文/英语单页字多（一页能把整章考点压缩成一张表），700 tokens 会被截断，
            # 截断后 JSON 解析失败 → 整页作废。多给 400 换个稳。
            max_tokens=700 if subject == 'math' else 1100,
        )
        return resp.choices[0].message.content or ''

    raw = _retry(_call, tries=3, waits=(0, 3, 8), on_log=on_log)
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise ValueError(f'第 {no} 页返回的不是 JSON：{raw[:80]}')
    return {
        'no': int(no),
        'role': str(data.get('role') or 'content').strip(),
        'belongs': 'prev' if str(data.get('belongs') or 'this').strip() == 'prev' else 'this',
        'topic': str(data.get('topic') or '').strip()[:40],
        'examples': [str(x) for x in (data.get('examples') or [])][:6]
                    if str(data.get('role') or '') not in ('toc', 'cover', 'divider', 'blank') else [],
        'points': [str(x)[:80] for x in (data.get('points') or [])][:8],
        'handwriting': bool(data.get('handwriting')),
        'page_text': str(data.get('page_text') or '')[:200],
    }


def profile_pages(pages: list, course: str, title: str = '', workers: int = 6,
                  on_progress=None, on_log=None, subject: str = 'math') -> dict:
    """并发逐页看图。任何一页失败都不许静默跳过——记进 failed 列表返回。"""
    results, failed = {}, []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(profile_one, p, course, str(p['no']), title, on_log,
                          subject): p['no'] for p in pages}
        for fut in as_completed(futs):
            no = futs[fut]
            try:
                results[no] = fut.result()
            except Exception as e:  # noqa: BLE001
                failed.append({'page': no, 'error': str(e)[:200]})
                if on_log:
                    on_log(f'第 {no} 页看图失败：{str(e)[:100]}')
            done += 1
            if on_progress:
                on_progress(done, len(pages), no)
    out = [results[k] for k in sorted(results)]
    return {'pages': out, 'failed': failed}


# ============================================================
# 3~6. 文本模型生成
# ============================================================

def _chat_json(system: str, user: str, max_tokens: int = 4000, on_log=None) -> dict:
    l = llm_cfg()
    if not l['key']:
        raise RuntimeError('文本模型未配置：需要 STUDY_LLM_API_KEY')

    def _call():
        from openai import OpenAI
        client = OpenAI(api_key=l['key'], base_url=l['base'] or None, timeout=240)
        kw = dict(model=l['model'],
                  messages=[{'role': 'system', 'content': system},
                            {'role': 'user', 'content': user}],
                  temperature=0.3, max_tokens=max_tokens)
        try:
            resp = client.chat.completions.create(
                response_format={'type': 'json_object'}, **kw)
        except Exception:
            resp = client.chat.completions.create(**kw)  # 部分模型不支持 json_object
        return resp.choices[0].message.content or ''

    raw = _retry(_call, tries=3, waits=(0, 4, 10), on_log=on_log)
    data = extract_json(raw)
    if not isinstance(data, dict):
        # 多半是被 max_tokens 截断了。抢救已完整的对象，别整批作废
        salv = {}
        for f in ('examples', 'quiz', 'pitfalls', 'sections', 'blocks', 'branches'):
            arr = _salvage_array(raw, f)
            if arr:
                salv[f] = arr
        if salv:
            salv['_truncated'] = True
            if on_log:
                n_salv = sum(len(v) for v in salv.values() if isinstance(v, list))
                on_log(f'  ⚠ 模型输出被截断，已抢救 {n_salv} 条完整内容')
            return salv
        raise ValueError('模型返回的不是 JSON：' + raw[:150])
    return data


def _pages_digest(pages: list, limit_text: int = 120) -> str:
    """把逐页理解结果压成给文本模型看的大纲。"""
    lines = []
    for p in pages:
        pts = '；'.join(p['points'][:4])
        ex = ('｜例题:' + ','.join(p['examples'])) if p['examples'] else ''
        prev = '【上一讲，别用】' if p.get('belongs') == 'prev' else ''
        lines.append(f"{prev}第{p['no']}页[{p['role']}] {p['topic']}{ex} {pts}"[:260])
    return '\n'.join(lines)


STRUCT_SYS = (
    '你是资深初中数学教研员，为初一学生整理课堂笔记纪要。'
    '你的读者是学生本人：话要短、要具体、要有"为什么容易错"，不要空话套话。'
    '只输出 JSON。'
)

STRUCT_PROMPT = """下面是一节初中数学课《{title}》全部 {n} 页课堂笔记的逐页摘要。

{digest}

请设计这份纪要的骨架。要求：
1. minute：一分钟总结。headline 一句话说清这讲的核心；points 恰好 5 条，每条 60~110 字，必须点名具体的坑（能写出式子的就写式子，用 ~{{分子}}{{分母}} 表示分数，用 <b></b> 强调关键词）；closing 一句话收尾。
2. mindmap：center 是主题；branches 6~8 个，每个 {{"t": 分支名(≤8字), "sub": [2~3 条要点(≤20字)], "link": 对应模块 id}}；最常考的分支加 "hot": true。
3. sections：必须 6~9 个模块（按知识块拆，不要把整讲压成两三个大块），按上课顺序。每个 {{"id": "sec-英文短语", "title": "中文标题", "level": "框架|核心|易错|技巧|压轴", "pages": [这页属于本模块的页码...]}}。
   - pages 只能从上面出现过的页码里选，且每页最多归属一个模块
   - 纯封面页（role=cover/toc/divider/blank）不要放进任何模块
   - 标了【上一讲，别用】的页不要放进任何模块，也不要按它们建模块
   - 例题页（role=example）不要放进模块，它们会单独成卡片

只输出：
{{"minute": {{"seconds": 60, "headline": "", "points": ["","","","",""], "closing": ""}},
  "mindmap": {{"center": "", "branches": []}},
  "sections": []}}"""

MIN_SECTIONS = 4


def build_structure(title: str, pages: list, on_log=None,
                    min_sec: int | None = None, tries: int = 3,
                    subject: str = 'math') -> dict:
    """
    生成骨架。模型有时会把一整讲压成两个大模块（实测过一次：9 个变 2 个），
    所以这里要校验数量并带反馈重试——不能直接交给下游，否则正文只有两大块，孩子没法按知识点跳。
    """
    SC = subject_cfg(subject)
    if min_sec is None:
        min_sec = SC.get('min_sections', MIN_SECTIONS)
    if on_log:
        on_log('正在设计骨架（一分钟总结 / 思维导图 / 模块划分）…')
    user = (SC.get('struct_prompt') or STRUCT_PROMPT).format(
        title=title, n=len(pages), digest=_pages_digest(pages))
    last = None
    for i in range(tries):
        if i and on_log:
            on_log(f'骨架不合格，重试（第 {i + 1} 次）…')
        data = _chat_json(SC.get('struct_sys') or STRUCT_SYS, user,
                          max_tokens=5000, on_log=on_log)
        for k in ('minute', 'mindmap', 'sections'):
            if k not in data:
                data[k] = last.get(k) if last else None
        secs = data.get('sections') or []
        if len(secs) >= min_sec and data.get('minute') and data.get('mindmap'):
            return data
        last = data
        if on_log:
            on_log(f'  这一版只给了 {len(secs)} 个模块（要求 >= {min_sec}）')
        user += (f"\n\n注意：上一版只给了 {len(secs)} 个模块，太粗了。"
                 f"这一讲必须拆成 {min_sec}~{min_sec + 2} 个模块，按知识块拆（一页一个知识点的就一页一个模块），"
                 f"不要把整讲压成两三块。")
    return last or {}


SECTION_SYS = (
    '你是资深初中数学教研员。你在给初一学生写某一节的笔记正文。'
    '语言要像老师当面讲：先说要干什么，再说为什么，最后点一句容易错在哪。'
    '禁止空话（"要认真审题"这类）。只输出 JSON。'
)

SECTION_PROMPT = """课程：《{title}》
本模块：{sec_title}（层级：{level}）

这一模块覆盖的页面摘要：
{page_info}

这些页上的详细内容（印刷文字 + 老师板书要点）：
{page_text}

写这一模块的正文 blocks。要求：
- 3~6 个 block，按讲解顺序
- 每个 block 是 {{"t": 类型, "v": 内容}}，类型只能是：
  p（普通段落）/ list（v 是字符串数组）/ formula（公式或推导链）/ board（板书还原）/ tip（老师口传的诀窍）/ trap（易错警示）/ table（需 head + rows）
- 公式用纯文本写，分数写 ~{{分子}}{{分母}}，乘方写 x²，不要用 LaTeX
- 至少有一个 tip 或 trap
- 总字数 200~450 字，宁可短而准

只输出 {{"blocks": [...]}}"""


def build_section(title: str, plan_row: dict, pages: list, on_log=None,
                  subject: str = 'math') -> list:
    SC = subject_cfg(subject)
    by_no = {p['no']: p for p in pages}
    info = '\n'.join(
        f"第{n}页：{by_no[n]['topic']}" for n in plan_row.get('pages', []) if n in by_no
    ) or '（无）'
    text = '\n'.join(
        (f"第{n}页 " + '；'.join(by_no[n]['points']) + ' ' + by_no[n].get('page_text', ''))[:400]
        for n in plan_row.get('pages', []) if n in by_no
    ) or '（无）'
    user = (SC.get('section_prompt') or SECTION_PROMPT).format(
        title=title, sec_title=plan_row.get('title', ''),
        level=plan_row.get('level', '核心'), page_info=info, page_text=text)
    data = _chat_json(SC.get('section_sys') or SECTION_SYS, user,
                      max_tokens=3000, on_log=on_log)
    blocks = data.get('blocks') or []
    clean = []
    ok_types = {'p', 'list', 'formula', 'board', 'tip', 'trap', 'table'}
    for b in blocks:
        if not isinstance(b, dict) or b.get('t') not in ok_types:
            continue
        if b['t'] == 'table':
            if isinstance(b.get('head'), list) and isinstance(b.get('rows'), list):
                clean.append({'t': 'table', 'head': [str(x) for x in b['head']],
                              'rows': [[str(c) for c in r] for r in b['rows']]})
        elif b['t'] == 'list':
            if isinstance(b.get('v'), list):
                clean.append({'t': 'list', 'v': [str(x) for x in b['v']]})
        elif isinstance(b.get('v'), str) and b['v'].strip():
            clean.append({'t': b['t'], 'v': b['v'].strip()})
    return clean


EX_SYS = (
    '你是资深初中数学教研员，正在还原课上讲过的例题。'
    '解题步骤要一步一步写清，不跳步；方法要能复用；陷阱要写真实踩过的坑。只输出 JSON。'
)

EX_PROMPT = """课程：《{title}》

下面列了 {n} 道题（编号由我给，你不要改）。每道题后面是它对应的板书页内容：

{items}

请按同样的顺序（idx 从 0 开始）整理每道题的内容，不要增删题目、不要合并。
每道题：
{{"idx": 0, "title": "一句话标题(≤14字)",
  "problem": "题干原文（数学式子用纯文本，分数写 ~{{分子}}{{分母}}）",
  "answer": "最终答案（必须把答案代回原式验算一遍再写，算错了要改）",
  "steps": ["第一步", "第二步", "..."],
  "method": "这类题的通法（≤60字）",
  "trap": "最容易错的地方（≤60字）"}}

只输出 {{"examples": [...]}}"""


def group_examples(pages: list, subject: str = 'math') -> list:
    """
    按看图模型识别出的「这页是例几」把页面聚成组。
    这是唯一可信的题号来源——不能让文本模型自己编（实测它会写 "例2/练1"、"③" 这种，
    和 board-map 对不上，正文插图就绑不上去）。

    两道过滤（都是实测踩出来的）：
      - 只认 role 是 example/practice 的页。目录页会列出「例1…例9」，直接采纳会把
        一整讲的题全挂到目录页上（实测 p7 被 例2/例3/例4/例5/例6 同时认领）。
      - 只认 belongs='this' 的页。课堂笔记开头常留着上一讲的尾巴，那里的「例8」不是本讲的。

    科目差异：数学题号稳定是「例N/练N」；语文英语常常根本没有题号（整页就一道题、
    或者写着 Task 2 / 练习1）。所以题号前缀按科目放宽，并且**认不出来时按页兜底**——
    否则语文英语会一道例题都生成不出来（实测过：分组为空 → examples=[] → 例题区整块消失）。
    """
    SC = subject_cfg(subject)
    prefixes = SC.get('example_prefix') or ('例', '练')
    pat = re.compile(r'^(' + '|'.join(re.escape(x) for x in prefixes) + r')\s*([0-9]+)',
                     re.I)
    groups, order = {}, []
    for p in pages:
        if p.get('belongs') == 'prev':
            continue
        if p.get('role') not in ('example', 'practice'):
            continue
        for raw in p['examples']:
            m = pat.match(str(raw).strip())
            if not m:
                continue
            key = m.group(1) + m.group(2)
            if key not in groups:
                groups[key] = []
                order.append(key)
            if p['no'] not in groups[key]:
                groups[key].append(p['no'])
    # 按题号排序（例1 在 例2 前），别按「首次出现」——模型可能先认出例8
    def _sort_key(k):
        m = re.match(r'^(.*?)(\d+)$', k)
        if not m:
            return (99, 99)
        pref = m.group(1)
        rank = prefixes.index(pref) if pref in prefixes else 98
        if subject == 'math':
            rank = 0 if pref == '例' else 1   # 数学保持原口径：例在练前面
        return (rank, int(m.group(2)))
    order.sort(key=_sort_key)
    out = [{'no': k, 'pages': sorted(groups[k])} for k in order]

    if not out and subject != 'math':
        # 兜底：语文/英语常常没有稳定题号（整页就是一道题，或者写着「1. Read...」）。
        # 一页一卡。为什么不做"相邻页合并"省卡片数：实测英语第8讲按相邻合并会把
        # 「单词 1-3」和「练习 5」粘成同一个例题，模型只能挑其中一页写，
        # 孩子看到的就是"题干对不上答案"。宁可卡片多几张，也不能驴唇不对马嘴。
        ex_pages = sorted(p['no'] for p in pages
                          if p.get('belongs') != 'prev'
                          and p.get('role') in ('example', 'practice'))
        out = [{'no': f'题{i + 1}', 'pages': [n]} for i, n in enumerate(ex_pages)]
    return out


def build_examples(title: str, pages: list, groups: list, on_log=None,
                   subject: str = 'math') -> tuple:
    """逐批生成例题正文；题号由 groups 固定，模型只负责填内容。

    返回 (examples, missing)：missing 是最终也没拿到的题号列表，
    由 run() 写进 report warnings —— 缺题必须显式暴露，不许静默跳过。
    """
    SC = subject_cfg(subject)
    if not groups:
        return [], []

    def _render(g) -> dict | None:
        """单题渲染：从模型返回的 dict 转成例题结构，不完整返回 None。"""
        steps = g.get('steps') or []
        if isinstance(steps, str):
            steps = [steps]
        item = {
            'no': g.get('_no', ''),             # ← 题号强制用分组里的，不听模型的
            'title': str(g.get('title') or g.get('_no', '')).strip()[:30],
            'problem': str(g.get('problem') or '').strip(),
            'answer': str(g.get('answer') or '').strip(),
            'steps': [str(s).strip() for s in steps if str(s).strip()],
            'method': str(g.get('method') or '').strip(),
            'trap': str(g.get('trap') or '').strip(),
        }
        if item['problem'] and item['answer'] and item['steps']:
            return item
        return None

    by_no = {p['no']: p for p in pages}
    if on_log:
        on_log(f'正在整理 {len(groups)} 道例题（{sum(len(g["pages"]) for g in groups)} 页板书）…')

    # 数学 4 题一批实测没问题；语文/英语单题文字多，4 题一批会顶到 max_tokens 截断
    # （实测英语题6/7/8 整批丢失），改 2 题一批
    BATCH = 4 if subject == 'math' else 2
    out = []
    missing = []          # 等待单题重试的 (group, reason)

    def _ask(chunk, n_override=None):
        items = []
        for j, g in enumerate(chunk):
            body = '\n'.join(
                f"   第{n}页：{by_no[n]['topic']}｜" + '；'.join(by_no[n]['points'][:6])
                + '｜' + by_no[n].get('page_text', '')[:200]
                for n in g['pages'] if n in by_no
            )
            items.append(f'[{j}] 题号={g["no"]}\n{body}')
        data = _chat_json(SC.get('ex_sys') or EX_SYS,
                          (SC.get('ex_prompt') or EX_PROMPT).format(
                              title=title, n=n_override or len(chunk),
                              items='\n\n'.join(items)),
                          max_tokens=8000, on_log=on_log)
        got = data.get('examples') or []
        by_idx = {}
        for e in got:
            if isinstance(e, dict) and isinstance(e.get('idx'), int):
                by_idx[e['idx']] = e
        return got, by_idx

    for i in range(0, len(groups), BATCH):
        chunk = groups[i:i + BATCH]
        got, by_idx = _ask(chunk)
        for j, g in enumerate(chunk):
            e = by_idx.get(j) or (got[j] if j < len(got) and isinstance(got[j], dict) else None)
            if not e:
                if on_log:
                    on_log(f'  {g["no"]} 没有返回内容，稍后单独重试')
                missing.append((g, '模型没有返回内容（批次可能被截断）'))
                continue
            e = dict(e)
            e['_no'] = g['no']
            item = _render(e)
            if item:
                out.append(item)
            else:
                if on_log:
                    on_log(f'  {g["no"]} 内容不完整（缺题干/答案/步骤），稍后单独重试')
                missing.append((g, '内容不完整（缺题干/答案/步骤）'))

    # 单题重试：一次只要一题，输出量最小，被截断的概率最低
    still_missing = []
    for g, reason in missing:
        if on_log:
            on_log(f'  ↻ {g["no"]} 单独重试（{reason}）…')
        got, by_idx = _ask([g], n_override=1)
        e = by_idx.get(0) or (got[0] if got and isinstance(got[0], dict) else None)
        if e:
            e = dict(e)
            e['_no'] = g['no']
            item = _render(e)
            if item:
                out.append(item)
                continue
        still_missing.append(f'例题 {g["no"]} 生成失败（{reason}），该题在纪要中缺失')
        if on_log:
            on_log(f'  ✗ {g["no"]} 重试后仍失败，记入 warnings')

    # 按分组题号顺序还原，别让重试打乱次序
    order = {g['no']: k for k, g in enumerate(groups)}
    out.sort(key=lambda x: order.get(x['no'], 10 ** 6))
    return out, still_missing


QUIZ_SYS = (
    '你是资深初中数学老师，在给学生出这一讲的复习自测。'
    '题目要考"会不会掉进这一讲的坑"，不要出纯计算。只输出 JSON。'
)

QUIZ_PROMPT = """课程：《{title}》

这一讲的全部内容摘要：
{digest}

请出 8 道自测题。要求：
- 5 道 choice（单选，4 个选项），3 道 fill（填空，答案可能有多种写法）
  choice: {{"id": "q1", "type": "choice", "q": "题干", "options": ["A","B","C","D"], "answer": 正确选项的下标(0~3), "explain": "为什么，指出错误选项错在哪"}}
  fill:   {{"id": "q6", "type": "fill", "q": "题干（要填的空用 ____ 表示）", "answer": ["正确答案", "另一种等价写法"], "explain": "解析"}}
- 每题都要有 explain，且 explain 要能自证
- 题干要具体，能算出确定答案，别出"下列说法正确的是"这种含糊题

只输出 {{"quiz": [...]}}"""

PITFALL_PROMPT = """课程：《{title}》

这一讲的全部内容摘要：
{digest}

请总结 7 条雷区（这一讲最容易错的地方）。每条：
{{"title": "坑的名字(≤10字)", "body": "具体错在哪 + 如何避免(60~100字，能举式子就举)"}}

只输出 {{"pitfalls": [...]}}"""


def build_quiz(title: str, pages: list, on_log=None, subject: str = 'math') -> dict:
    """自测和雷区分两次要。一次要 8 题 + 7 条会被 max_tokens 截断（实测过）。"""
    SC = subject_cfg(subject)
    digest = _pages_digest(pages, 200)
    if on_log:
        on_log('正在出自测…')
    dq = _chat_json(SC.get('quiz_sys') or QUIZ_SYS,
                    (SC.get('quiz_prompt') or QUIZ_PROMPT).format(title=title, digest=digest),
                    max_tokens=6000, on_log=on_log)
    quiz, pitfalls = [], []
    for q in (dq.get('quiz') or []):
        if not isinstance(q, dict) or q.get('type') not in ('choice', 'fill'):
            continue
        if q['type'] == 'choice':
            opts = q.get('options') or []
            ans = q.get('answer')
            if not isinstance(opts, list) or len(opts) < 2:
                continue
            if not isinstance(ans, int) or not (0 <= ans < len(opts)):
                continue
            quiz.append({'id': str(q.get('id') or f'q{len(quiz) + 1}'), 'type': 'choice',
                         'q': str(q.get('q') or ''), 'options': [str(o) for o in opts],
                         'answer': ans, 'explain': str(q.get('explain') or '')})
        else:
            ans = q.get('answer')
            if isinstance(ans, str):
                ans = [ans]
            if not isinstance(ans, list) or not ans:
                continue
            quiz.append({'id': str(q.get('id') or f'q{len(quiz) + 1}'), 'type': 'fill',
                         'q': str(q.get('q') or ''), 'answer': [str(a) for a in ans],
                         'explain': str(q.get('explain') or '')})
    if on_log:
        on_log('正在出雷区…')
    dp = _chat_json(SC.get('quiz_sys') or QUIZ_SYS,
                    (SC.get('pitfall_prompt') or PITFALL_PROMPT).format(
                        title=title, digest=digest),
                    max_tokens=3000, on_log=on_log)
    for p in (dp.get('pitfalls') or []):
        if isinstance(p, dict) and p.get('title') and p.get('body'):
            pitfalls.append({'title': str(p['title']), 'body': str(p['body'])})
    return {'quiz': quiz, 'pitfalls': pitfalls}


# ============================================================
# 7. 结构断言
# ============================================================

FILLER_ROLES = {'cover', 'toc', 'divider', 'blank'}
MARK_PRE_COVER_KEPT: list = []   # 被标题关键词保护、没判成上一讲的页


def _title_tokens(title: str) -> set:
    """把标题拆成中文二元组，用来判断"这页讲的还是不是本讲的东西"。
    例：《水浒传》必考考点 -> {水浒, 浒传, 必考, 考点, ...}"""
    toks = set()
    for w in re.findall(r'[\u4e00-\u9fa5]{2,}', title or ''):
        for i in range(len(w) - 1):
            toks.add(w[i:i + 2])
    return toks


def mark_pre_cover(pages: list, title: str = '') -> int:
    """
    本讲封面之前的页，一律判定为上一讲的尾巴（belongs='prev'）。

    为什么需要这条硬规则：看图模型看单页时判不出「这页是上一讲的例8」——
    它能读出页面上写着「例8」，但不知道这一讲的例1在哪。实测两轮都失败
    （例8 一直被绑到第 1、2 页）。而课堂笔记结构是稳定的：前面若干页是上一讲
    的收尾，紧接着才是本讲封面。所以用「第一个封面/过渡页」当分界线最可靠。

    只在页码靠前（<= 9）时认这个分界线，避免把本讲中段的过渡页当界碑。
    返回分界位置（0 表示没找到或不需要处理）。
    """
    cut = None
    for want in ('cover', 'divider'):
        for i, p in enumerate(pages):
            if i > 9:
                break
            if p['role'] == want:
                cut = i
                break
        if cut is not None:
            break
    if cut:
        toks = _title_tokens(title)
        kept = []
        for p in pages[:cut]:
            # 保护：封面前面但页面讲的就是本讲主题（如语文讲义开头的《水浒传》考点总览），
            # 判成上一讲会白丢一整页内容。命中标题关键词就留在本讲。
            text = (p.get('topic') or '') + (p.get('page_text') or '')
            if toks and any(t in text for t in toks):
                kept.append(p['no'])
                continue
            p['belongs'] = 'prev'
        if kept:
            MARK_PRE_COVER_KEPT[:] = kept
    return cut or 0


def clamp_mid_prev(pages: list, head: int = 10) -> list:
    """
    把中段被误判成「上一讲」的页拉回来。

    实测（英语第8讲）：p40「tell / speak / say / talk 辨析」、p52「break out」都被
    看图模型判成 belongs=prev，于是这两页只留在文末画廊，正文里整块知识点消失——
    而它们是本讲 Words / Expressions 的核心页。孩子看笔记会觉得"这里怎么跳过去了"。

    依据：课堂笔记的结构是固定的，只有**开头**会留着上一讲的尾巴。
    中段（第 10 页之后）出现"上一讲"几乎只能是误判，一律拉回本讲。
    返回被拉回的页号，用于日志和 report。
    """
    fixed = [p['no'] for p in pages if p['no'] > head and p.get('belongs') == 'prev']
    for p in pages:
        if p['no'] > head:
            p['belongs'] = 'this'
    return fixed


def mark_weak_covers(pages: list) -> list:
    """
    兜底：role 判成 content/toc、但没提取到任何要点、印刷文字又极短（≤12 字）的页，
    多半是纯封面/过渡页。实测第8讲 p17「全章易错总结」绿底大字被判成 content，
    混进了正文——这种页的特征很稳定：只有一行大字标题，没有任何知识点。

    改判为 divider，让 FILLER_ROLES 统一处理（不进正文、画廊灰掉）。
    返回改判页号列表，只用于日志与前端提示。
    """
    fixed = []
    for p in pages:
        if p['role'] in ('content', 'toc') and not (p.get('points') or []) \
                and len((p.get('page_text') or '').strip()) <= 12:
            p['role'] = 'divider'
            p['examples'] = []
            fixed.append(p['no'])
    return fixed


def repair_links(lesson: dict) -> list:
    """
    修导图里的死链。

    实测（英语第8讲重跑）：模型给分支写了 link='sec-answer-strategies'，但模块 id
    这次改叫 sec-exam-skills —— 结构重试时模块名会变，link 却照着上一版写，于是孩子
    点方块没反应，页面看起来却"没问题"（静默失效）。

    处理：先按归一化名字找同义模块；找不到就把 link 摘掉（分支还在，只是不可点），
    并把修了哪些返回给日志。绝不留下死链。
    """
    secs = lesson.get('sections') or []
    ids = [s.get('id') for s in secs]
    norm = {re.sub(r'[^a-z0-9]', '', (s.get('id') or '').lower()): s.get('id') for s in secs}
    titles = {re.sub(r'[^\u4e00-\u9fa5a-z0-9]', '', (s.get('title') or '').lower()): s.get('id')
              for s in secs}
    fixed = []
    for b in (lesson.get('mindmap') or {}).get('branches') or []:
        link = b.get('link')
        if not link or link in ids:
            continue
        key = re.sub(r'[^a-z0-9]', '', str(link).lower())
        cand = norm.get(key) or next((v for k, v in norm.items() if key and (key in k or k in key)), None)
        if not cand:
            tk = re.sub(r'[^\u4e00-\u9fa5a-z0-9]', '', str(b.get('t') or '').lower())
            cand = next((v for k, v in titles.items() if tk and (tk in k or k in tk)), None)
        if cand:
            b['link'] = cand
            fixed.append(f'{link} -> {cand}')
        else:
            b.pop('link', None)
            fixed.append(f'{link} -> drop')
    return fixed


def drop_prev_only_sections(sections: list, pages: list, on_log=None) -> list:
    """
    丢掉"只由上一讲页 / 封面页 / 例题页组成的模块"。

    实测（语文第8讲）：讲义前 3 页是上一讲的作文批改与现代文阅读，骨架模型照样
    给它们建了 sec-reading / sec-writing 两个模块，正文写了一堆上一讲的内容——
    这会让孩子以为"这一讲讲了作文"，而且这些模块没有板书图可嵌，断言也过不了。

    规则：模块的页里至少要有 1 页是「本讲 + 非封面/目录/过渡/空白 + 非例题」；
    否则整块丢掉（例题页单独成卡片，不该撑起模块）。
    """
    by_no = {x['no']: x for x in pages}
    keep, dropped = [], []
    for s in sections:
        pgs = [int(n) for n in (s.get('pages') or []) if int(n) in by_no]
        live = [n for n in pgs
                if by_no[n].get('belongs') != 'prev'
                and by_no[n].get('role') not in FILLER_ROLES
                and by_no[n].get('role') not in ('example', 'practice')]
        if live:
            keep.append(s)
        else:
            dropped.append(s.get('title') or s.get('id'))
    if dropped and on_log:
        on_log('   丢掉 %d 个模块（只由上一讲/封面/例题页组成）：%s'
               % (len(dropped), '、'.join(map(str, dropped))))
    return keep


def build_board(pages: list, sections: list, groups: list) -> dict:
    """生成 board-map 的一段：每页说明 + 模块页归属 + 例题页归属 + 封面页黑名单。"""
    by_no = {p['no']: p for p in pages}
    note = {}
    for p in pages:
        tag = {'cover': '封面 · ', 'toc': '目录 · ', 'divider': '过渡 · ', 'blank': '空白页',
               'practice': '练习 · ', 'summary': '总结 · '}.get(p['role'], '')
        note[p['no']] = (tag + (p['topic'] or '')).strip(' ·') or f'第 {p["no"]} 页'
    board = {
        # 上讲课尾页也算「不进正文」：它的内容和本讲无关，嵌进去只会让人以为本讲讲过
        'skipInBody': sorted(p['no'] for p in pages
                             if p['role'] in FILLER_ROLES or p.get('belongs') == 'prev'),
        'pages': {str(k): v for k, v in sorted(note.items())},
        'sections': {},
        'examples': {},
    }
    for s in sections:
        raw_pgs = [int(n) for n in (s.get('pages') or []) if int(n) in by_no]
        # 例题页会单独成卡片，不要同时塞进模块正文（否则同一页出现两次）；
        # 封面/目录/过渡/空白页同样不进模块正文（skipInBody 口径保持一致）
        pgs = [n for n in raw_pgs
               if by_no[n]['role'] not in ({'example', 'practice'} | FILLER_ROLES)
               and by_no[n].get('belongs') != 'prev']
        if not pgs:
            # 整块都是例题页/上讲课页时，至少要留一张，别让模块彻底没图
            skip_set = {p['no'] for p in pages
                        if p['role'] in FILLER_ROLES or p.get('belongs') == 'prev'}
            pgs = [n for n in raw_pgs if n not in skip_set][:1] or raw_pgs[:1]
        if pgs:
            board['sections'][s['id']] = pgs
    # 例题页：直接用 group_examples 的结果，保证和例题卡片的 no 一定对得上
    for g in groups:
        board['examples'][g['no']] = sorted(set(g['pages']))
    return board


def rescue_orphans(board: dict, pages: list, on_log=None) -> list:
    """
    把"没被任何模块/例题收留"的内容页挂到最近的模块上。

    实测（语文第8讲）：p1 是《水浒传》考点总览（内容页、属于本讲），骨架模型没把它
    分给任何模块，于是它只出现在文末画廊——孩子按纪要复习时根本看不到这一页。
    正文缺一页不会报错，只会"悄悄少一块"，所以这里主动兜住。

    只挂真正的内容页（content/summary，非封面非例题）；找页号距离最近的模块。
    返回被挂上的页号，用于日志与 report。
    """
    used = set()
    for pgs in (board.get('sections') or {}).values():
        used |= set(pgs)
    for pgs in (board.get('examples') or {}).values():
        used |= set(pgs)
    skip = set(board.get('skipInBody') or [])
    rescued = []
    for p in sorted(pages, key=lambda x: x['no']):
        n = p['no']
        if n in used or n in skip:
            continue
        if p.get('role') in FILLER_ROLES or p.get('role') in ('example', 'practice'):
            continue
        best_sid, best_d = None, None
        for sid, pgs in (board.get('sections') or {}).items():
            live = [x for x in pgs if x not in skip]
            if not live:
                continue
            d = min(abs(n - x) for x in live)
            if best_d is None or d < best_d:
                best_sid, best_d = sid, d
        if best_sid:
            board['sections'][best_sid] = sorted(set(board['sections'][best_sid]) | {n})
            rescued.append(n)
            if on_log:
                on_log(f'   第 {n} 页没被任何模块收，已挂到 {best_sid}')
    return rescued


def validate(lesson: dict, board: dict, page_count: int) -> dict:
    """
    返回 {'errors': [...], 'warnings': [...]}。
    errors 非空就不许落盘（会生成空壳）；warnings 只提示，降级处理但要说出来。
    规则和 test-data.js 保持一致。
    """
    errs, warns = [], []
    for k in ('id', 'no', 'title', 'tagline', 'minute', 'sections', 'examples', 'pitfalls', 'quiz'):
        if not lesson.get(k):
            errs.append(f'缺少字段 {k}')
    m = lesson.get('minute') or {}
    if not m.get('headline') or len(m.get('points') or []) < 3 or not m.get('closing'):
        errs.append('minute 结构不完整（headline + >=3 points + closing）')
    mm = lesson.get('mindmap') or {}
    if not mm.get('center') or len(mm.get('branches') or []) < 4:
        errs.append('mindmap 至少 4 个分支')
    secs = lesson.get('sections') or []
    if len(secs) < 3:
        errs.append(f'模块只有 {len(secs)} 个，至少 3 个')
    sec_ids = [s.get('id') for s in secs]
    if len(set(sec_ids)) != len(sec_ids):
        errs.append('模块 id 有重复')
    for s in secs:
        if not s.get('id') or not s.get('title'):
            errs.append('模块缺 id/title')
        if len(s.get('blocks') or []) < 2:
            errs.append(f'模块 {s.get("id")} blocks 少于 2')
        if not board.get('sections', {}).get(s.get('id')):
            errs.append(f'模块 {s.get("id")} 没有归属板书页（正文会缺图）')
    if len(lesson.get('examples') or []) < 1:
        errs.append('一道例题都没有')
    for e in (lesson.get('examples') or []):
        for k in ('no', 'problem', 'answer', 'steps'):
            if not e.get(k):
                errs.append(f'例题 {e.get("no")} 缺 {k}')
        # 通法/陷阱缺了不影响孩子做题（只是少两行提示），降级成警告，不作废整份
        for k in ('method', 'trap'):
            if not e.get(k):
                warns.append(f'例题 {e.get("no")} 缺 {k}（那两行留空）')
        if not board.get('examples', {}).get(e.get('no')):
            errs.append(f'例题 {e.get("no")} 在 board-map 里没有对应页')
    if len(lesson.get('quiz') or []) < 5:
        errs.append(f'自测只有 {len(lesson.get("quiz") or [])} 题，至少 5 题')
    for q in (lesson.get('quiz') or []):
        if q.get('type') == 'choice':
            if not isinstance(q.get('answer'), int) or not (0 <= q['answer'] < len(q.get('options') or [])):
                errs.append(f'自测 {q.get("id")} 选择题答案下标越界')
        elif q.get('type') == 'fill':
            if not isinstance(q.get('answer'), list) or not q['answer']:
                errs.append(f'自测 {q.get("id")} 填空答案必须是非空数组')
    for p in (board.get('skipInBody') or []):
        if not (1 <= int(p) <= page_count):
            errs.append(f'skipInBody 页号越界: {p}')
    sec_id_set = {s.get('id') for s in secs}
    for b in ((lesson.get('mindmap') or {}).get('branches') or []):
        if b.get('link') and b['link'] not in sec_id_set:
            errs.append("导图分支「%s」link 指向不存在的模块: %s" % (b.get('t'), b['link']))
    for sid, pgs in (board.get('sections') or {}).items():
        live = [p for p in pgs if p not in (board.get('skipInBody') or [])]
        if not live:
            errs.append(f'模块 {sid} 的板书页全是封面页，正文会没图')
        for p in pgs:
            if not (1 <= int(p) <= page_count):
                errs.append(f'模块 {sid} 页号越界: {p}')
    for no, pgs in (board.get('examples') or {}).items():
        for p in pgs:
            if not (1 <= int(p) <= page_count):
                errs.append(f'例题 {no} 页号越界: {p}')
    return {'errors': errs, 'warnings': warns}


# ============================================================
# 8. 写盘
# ============================================================

def _js(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def write_lesson(lesson: dict, board: dict, dist_dir: Path, gallery_dir: str,
                 page_count: int, original_rel: str) -> dict:
    dist_dir = Path(dist_dir)
    data_dir = dist_dir / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    key = lesson['id']

    # ---- lesson-XX.js ----
    payload = dict(lesson)
    payload['original'] = original_rel
    payload['galleryDir'] = gallery_dir
    payload['pages'] = page_count
    js = (
        f"// {lesson.get('no')} {lesson.get('title')} —— 笔记工坊自动生成\n"
        f"// 生成时间：{time.strftime('%Y-%m-%d %H:%M')}\n"
        f"// 数据说明：文本里 ~{{分子}}{{分母}} 会渲染成分数；其余为 HTML。\n"
        "window.NOTES = window.NOTES || {};\n"
        f"window.NOTES['{key}'] = " + _js(payload) + ";\n"
    )
    # 文件名要能被平台加载器拼出来：'data/lesson-' + id.replace(/^lesson/, '') + '.js'
    # 所以 lesson08 → lesson-08.js，en08 → lesson-en08.js
    data_file = data_dir / ('lesson-' + key.replace('lesson', '').lstrip('-') + '.js')
    data_file.write_text(js, encoding='utf-8')

    # ---- board-map.js：把这一讲的段落插进去（保留原有的） ----
    bm_path = data_dir / 'board-map.js'
    old = bm_path.read_text(encoding='utf-8') if bm_path.exists() else \
        "window.BOARD = window.BOARD || {};\n"
    # 用显式标记包裹本讲这一段。删旧段时只认标记、不猜对象结构——
    # 正则按 "}, " 猜嵌套会在 board 里有内层对象时截断错位（实测会切掉半个 pages 表）
    begin, end = f'/* FORGE:BEGIN {key} */', f'/* FORGE:END {key} */'
    block = (f"  {begin}\n"
             f"  {key}: " + _js(board).replace('\n', '\n  ') + f",\n"
             f"  {end}")
    if begin in old:
        old = re.sub(r'\n?\s*' + re.escape(begin) + r'.*?' + re.escape(end) + r'\n?',
                     '\n', old, flags=re.S)
    idx = old.rstrip().rfind('}')
    if idx < 0:
        old = "window.BOARD = window.BOARD || {};\n"
        idx = old.rfind('}')
    head = old[:idx].rstrip()
    # 尾部若残留本文件风格的 FORGE:END 注释行，先剥掉再判断补逗号——
    # 否则会在"属性},  /*END*/,  下一段"里补出第二个逗号（双逗号非法，实测炸过整个 board-map）
    head = re.sub(r'(?m)\n\s*/\* FORGE:END [A-Za-z0-9_-]+ \*/\s*$', '', head).rstrip()
    # 只在「对象里已经有内容」时补逗号；空对象 {" 后面直接跟逗号是非法的
    head = head.rstrip(',')
    if not head.endswith('{'):
        head += ','
    merged = head + '\n' + block + '\n};\n'
    # 清掉孤立逗号行（历史写入留下的脏字符，一行只有一个逗号必然非法）
    merged = re.sub(r'(?m)^\s*,\s*\n', '', merged)
    # 写盘前自检：大括号不平衡说明这次合并会把整个 board-map 写坏（所有讲都加载不了），
    # 这种错误绝不能静默落盘
    if merged.count('{') != merged.count('}'):
        raise AssertionError(
            f'board-map.js 合并后大括号不平衡 '
            f'({merged.count("{")} vs {merged.count("}")})，拒绝写入，请检查 board-map.js 现状')
    # 保证 window.BOARD 初始化行在
    if 'window.BOARD' not in merged.split('\n')[0]:
        merged = "window.BOARD = window.BOARD || {};\n" + merged
    bm_path.write_text(merged, encoding='utf-8')

    # ---- index.js：把讲号加进名单 ----
    idx_path = data_dir / 'index.js'
    src = idx_path.read_text(encoding='utf-8') if idx_path.exists() else \
        "window.NOTE_INDEX = [];\n"
    m = re.search(r'(\[)([^\]]*)(\])', src)
    if m:
        # 先按完整 key 去重（语文 ch08 / 英语 en08 不是 lessonNN 格式，
        # 以前只抽 lesson(\d+) 判断，结果每跑一次就多一张重复卡片——实测语文 3 张、英语 4 张）
        ids = re.findall(r"'([^']+)'", m.group(2))
        seen, uniq = set(), []
        for i in ids:
            if i not in seen:
                seen.add(i)
                uniq.append(i)
        if key not in seen:
            uniq.append(key)
        src = src[:m.start()] + '[' + ', '.join(f"'{i}'" for i in uniq) + ']' + src[m.end():]
    else:
        src = f"window.NOTE_INDEX = ['{key}'];\n"
    idx_path.write_text(src, encoding='utf-8')
    return {
        'lesson_file': str(data_file),
        'board_map': str(bm_path),
        'index': str(idx_path),
    }


# ============================================================
# 总入口
# ============================================================

def _cache_path(pdf_bytes: bytes, subject: str = 'math') -> Path:
    """缓存 key = PDF 内容指纹 + 科目 + 缓存版本。改了识别 prompt 就 bump CACHE_VER，
    否则会命中旧格式的缓存（缺字段）而静默降级。

    科目也要进 key：同一份 PDF 用数学口径和语文口径识别，出来的 role/points 不一样，
    只按 PDF 指纹存会串味（数学的缓存被语文复用，整份笔记的口径就错了）。
    数学沿用无后缀的文件名，别让已有的缓存白扔。"""
    import hashlib
    d = Path(tempfile.gettempdir()) / 'notes_forge_cache'
    d.mkdir(exist_ok=True)
    tail = '' if subject == 'math' else '-' + subject
    return d / (hashlib.sha256(pdf_bytes).hexdigest()[:16] + tail + f'-v{CACHE_VER}.json')


def run(pdf_bytes: bytes, lesson_key: str, title: str, no: str, course: str,
        date: str, dist_dir: Path | None = None, workers: int = 6,
        on_log=None, on_progress=None, use_cache: bool = True,
        subject: str = 'math', gallery_sub: str | None = None,
        write_outputs: bool = True) -> dict:
    """
    跑完整流水线。返回 {'ok': bool, 'lesson': {...}, 'report': {...}, 'errors': [...]}。
    任何一步失败都会抛出，由界面显示——不静默兜底。

    subject：math / chinese / english，决定用哪套讲解口径（见 SUBJECTS）。
    gallery_sub：图片目录名，默认按讲号推（lesson08 → L08）。
                 多科目时讲号会撞（数学第8讲和语文第8讲），所以允许显式指定（E8 / C8）。
    dist_dir=None：不落静态站目录（云端模式），临时目录只用于可能的缓存。
    write_outputs=False：跳过 ⑧ 写盘（originals/lesson-XX.js/board-map.js），
                 产物由调用方（notes.py）直接推 GitHub —— 结构断言照样全跑。
    """
    t0 = time.time()
    log = on_log or (lambda s: None)
    if dist_dir is None:
        dist_dir = Path(tempfile.mkdtemp(prefix='notes_forge_'))
    dist_dir = Path(dist_dir)
    SC = subject_cfg(subject)

    log(f'① 渲染页面（{len(pdf_bytes) // 1024} KB）…')
    ing = ingest(pdf_bytes, lesson_key, dist_dir, on_log=log, gallery_sub=gallery_sub)

    cache = _cache_path(pdf_bytes, subject)
    pages = None
    if use_cache and cache.exists():
        try:
            cached = json.loads(cache.read_text(encoding='utf-8'))
            if cached.get('pages') and len(cached['pages']) == ing['count']:
                pages = cached['pages']
                log(f'② 复用上次的逐页识别结果（{len(pages)} 页，省掉看图费用）')
        except Exception:
            pages = None

    if pages is None:
        log(f'② 逐页看图理解（{ing["count"]} 页，{workers} 并发）…')
        pf = profile_pages(ing['pages'], course, title=title, workers=workers,
                           on_progress=on_progress, on_log=log, subject=subject)
        if not pf['pages']:
            raise RuntimeError('所有页面都识别失败，检查看图模型配置')
        pages = pf['pages']
        failed = pf['failed']
        try:
            cache.write_text(json.dumps({'pages': pages, 'failed': failed},
                                        ensure_ascii=False), encoding='utf-8')
        except Exception:
            pass
    else:
        failed = []

    # 把印刷文字补进 pages（看图模型抄的标题可能不全）
    by_no = {p['no']: p for p in ing['pages']}
    for p in pages:
        if not p.get('page_text') and by_no.get(p['no']):
            p['page_text'] = by_no[p['no']]['text'][:300]

    # 封面之前的页 = 上一讲尾巴（硬规则，兜住看图模型判不准 belongs 的情况）
    MARK_PRE_COVER_KEPT.clear()
    cut = mark_pre_cover(pages, title=title)
    if MARK_PRE_COVER_KEPT:
        log(f'   第 {",".join(map(str, MARK_PRE_COVER_KEPT))} 页虽然在本讲封面前面，但讲的是本讲主题，保留了')
    if cut:
        log(f'   第 1~{cut} 页判为上一讲的内容，不进本讲正文')

    # 中段"上一讲"误判拉回本讲（否则整块知识点只在画廊里）
    pulled = clamp_mid_prev(pages, head=cut + 6 if cut else 10)
    if pulled:
        log(f'   第 {",".join(map(str, pulled))} 页被判成上一讲，已拉回本讲（中段不可能是别的讲）')

    # 弱内容页兜底：只有一行大字、没有要点的"假内容页"改判为过渡封面
    weak = mark_weak_covers(pages)
    if weak:
        log(f'   第 {",".join(map(str, weak))} 页是纯封面页（只有标题没有内容），不进正文')

    log(f'③ 设计骨架（已识别 {len(pages)} 页，{len(failed)} 页失败）…')
    struct = build_structure(title, pages, on_log=log, subject=subject)
    # 只由上一讲页撑起来的模块先清掉，否则正文会写上一讲的内容、且没有板书图
    struct['sections'] = drop_prev_only_sections(struct.get('sections') or [], pages, on_log=log)

    log('④ 逐模块写正文…')
    sections = []
    for i, plan in enumerate(struct['sections'], 1):
        log(f'  [{i}/{len(struct["sections"])}] {plan.get("title", plan.get("id"))}')
        blocks = build_section(title, plan, pages, on_log=log, subject=subject)
        if blocks:
            sections.append({'id': plan['id'], 'title': plan['title'],
                             'level': plan.get('level', '核心'), 'blocks': blocks})

    log('⑤ 整理例题…')
    groups = group_examples(pages, subject=subject)
    examples, ex_missing = build_examples(title, pages, groups, on_log=log, subject=subject)

    log('⑥ 出雷区与自测…')
    qz = build_quiz(title, pages, on_log=log, subject=subject)

    lesson = {
        'id': lesson_key,
        'no': no,
        'course': course,
        'subject': subject if subject in SUBJECTS else 'math',
        'subjectLabel': SC.get('label', '数学'),
        'date': date,
        'title': title,
        'tagline': (struct.get('minute') or {}).get('headline', '')[:60] or title,
        'tags': [b.get('t') for b in (struct.get('mindmap') or {}).get('branches', [])][:7],
        'mindmap': struct['mindmap'],
        'minute': struct['minute'],
        'sections': sections,
        'examples': examples,
        'pitfalls': qz['pitfalls'],
        'quiz': qz['quiz'],
    }
    # 导图死链修复（必须放在 validate 之前，否则断言只会报错不会自愈）
    link_fixes = repair_links(lesson)
    if link_fixes:
        log('   导图链接修正：' + '；'.join(link_fixes))

    board = build_board(pages, struct['sections'], groups)
    rescue_orphans(board, pages, on_log=log)

    log('⑦ 结构断言…')
    vres = validate(lesson, board, ing['count'])
    errs, warns = vres['errors'], vres['warnings']

    report = {
        'pages_total': ing['count'],
        'pages_profiled': len(pages),
        'pages_failed': failed,
        'sections': len(sections),
        'examples': len(examples),
        'quiz': len(lesson['quiz']),
        'pitfalls': len(lesson['pitfalls']),
        'seconds': round(time.time() - t0, 1),
        'errors': errs,
        'warnings': warns + ex_missing,
    }
    # 完整度暴露：哪些页既没进模块、也没进例题、又不是明说的封面页——
    # 这些页在正文里看不到（只在文末画廊），要显式告诉使用者，别让它静默消失
    used = set()
    for pgs in board['sections'].values():
        used |= set(pgs)
    for pgs in board['examples'].values():
        used |= set(pgs)
    report['orphan_pages'] = sorted(p['no'] for p in pages
                                    if p['no'] not in used
                                    and p['no'] not in set(board['skipInBody'])
                                    and p['role'] != 'blank')

    if errs:
        log(f'❌ 断言未通过（{len(errs)} 项），已停止，不写盘')
        return {'ok': False, 'lesson': lesson, 'board': board, 'report': report,
                'ingest': ing}

    if write_outputs:
        log('⑧ 写盘…')
        original_name = f'{lesson_key.replace("lesson", "L")}-{title}.pdf'
        orig_dir = dist_dir / 'originals'
        orig_dir.mkdir(parents=True, exist_ok=True)
        (orig_dir / original_name).write_bytes(pdf_bytes)
        written = write_lesson(lesson, board, dist_dir, ing['gallery_dir'],
                               ing['count'], f'originals/{original_name}')
        report.update(written)
    else:
        # 云端模式：产物（lesson JSON + 板书图）由调用方推 GitHub 存档
        log('⑧ 产物移交存储层（推 GitHub 存档）…')
    log(f'✅ 完成，用时 {report["seconds"]} 秒')
    return {'ok': True, 'lesson': lesson, 'board': board, 'report': report,
            'ingest': ing}
