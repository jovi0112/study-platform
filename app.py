"""
学习积累平台 - Streamlit 主应用
- 标签: 拍照录入 / 错题本 / 复习计划 / 知识点图谱 / 家长端
- 全部本地 SQLite 存储, 图像本地保存
"""
import os
import sys
import time
import json
import base64
import streamlit as st
from datetime import datetime
from PIL import Image, ImageOps

# 项目内部模块
import db
import ocr
import llm
import web_search
import backup
import quiz
import diagram
# ============== 路径 ==============
APP_DIR = os.path.dirname(__file__)
UPLOAD_DIR = os.path.join(APP_DIR, 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)
SUBJECTS = ['数学', '语文', '英语', '物理', '化学', '生物', '历史', '地理', '政治', '其他']
WRONG_REASONS = ['概念不清', '计算失误', '审题错误', '公式记错', '思路不对', '粗心', '时间不够', '没学过']

# 侧边栏签名(孩子可在「我的工作室 → 我的签名」里改, 20 汉字以内)
DEFAULT_SIGNATURE = '13 岁孩子学习错题本地化记录'
SIGNATURE_MAX = 20


@st.fragment(run_every=1.0)
def _qz_countdown(pid: int):
    """
    每秒只刷新这一小块, 不打断孩子正在写的答案。
    时间到就把"该收卷了"这个意图放进 session_state, 再整页重跑让主流程收卷 ——
    收卷必须走主流程, 否则拿不到页面上的答案。
    """
    info = quiz.paper_time_left(pid)
    if not info.get('limited'):
        st.caption('⏱ 本卷不限时')
        return
    left = int(info.get('left') or 0)
    if left <= 0:
        st.error('⏰ 时间到，正在自动收卷…')
        st.session_state['qz_force_submit'] = 'timeout'
        st.rerun()
        return
    m, s = divmod(left, 60)
    color = '#C0392B' if left <= 60 else '#1F4B75'
    st.markdown(
        f"<div style='display:flex;align-items:baseline;gap:12px'>"
        f"<span style='font-size:26px;font-weight:700;color:{color};"
        f"font-variant-numeric:tabular-nums'>⏳ {m:02d}:{s:02d}</span>"
        f"<span style='color:#777;font-size:13px'>本卷限时 {quiz.limit_text(info.get('total'))}，"
        f"时间到自动交卷</span></div>", unsafe_allow_html=True)
    if left <= 60:
        st.caption('还剩不到 1 分钟，先把最有把握的写完')


def show_diagram(svg: str, note: str = ''):
    """
    显示讲解思维导图(SVG)。
    用 iframe 而不是 st.markdown: markdown 会把 svg 里的标签当 HTML 过滤掉, 图会变空白;
    st.image 只吃路径/URL/字节, 不吃 SVG 字符串。
    优先 st.iframe(新版 API), 老版本退回 components.html —— 免得哪天 Streamlit 升级把旧接口删了图就没了。
    """
    if not svg:
        return
    h = 420
    if 'viewBox="0 0 ' in svg:
        try:
            h = int(float(svg.split('viewBox="0 0 ')[1].split('"')[0].split()[1]))
        except Exception:
            pass
    html = ('<style>html,body{margin:0;padding:0;overflow:hidden;background:transparent}</style>' + svg)
    shown = False
    try:
        st.iframe(html, height=h + 12)
        shown = True
    except (AttributeError, TypeError):
        shown = False
    if not shown:
        try:
            import streamlit.components.v1 as components
            components.html(html, height=h + 12, scrolling=False)
            shown = True
        except Exception as e:
            st.caption(f'这张图没能显示出来({e}), 但讲解文字还在下面。')
    if shown and note:
        st.caption(note)


st.set_page_config(
    page_title='学习积累平台',
    page_icon='📚',
    layout='wide',
    initial_sidebar_state='expanded',
)

# 初始化数据库
db.init_db()
# 每日测试模块的表在下面"模块自检"之后建, 免得旧模块缺函数时先崩在这里

# ============== 模块新旧自检(结构性防御) ==============
# Streamlit 只热重载主脚本 app.py, 被 import 的 quiz.py / diagram.py 不会重载。
# 于是"新界面 + 进程里的旧模块"会撞出 AttributeError, 整页变红报错 —— 用户看到的就是
# "一打开就报错", 只有英文 traceback, 完全不知道该点哪里。
# 这里的顺序是: 探一遍 -> 缺了就自动 reload 一次(磁盘文件通常已经是新的) -> 还缺才说人话停下。
_QUIZ_NEED = ('SUBJECTS', 'TYPE_NAMES', 'init_quiz_db', 'create_paper', 'pick_questions',
              'save_answer', 'grade_paper', 'get_paper', 'list_papers', 'get_mastery',
              'wrong_summary', 'scope_stats', 'progress_hint', 'md_safe',
              'current_phase', 'get_offset', 'set_offset',
              # 手动指定章节(2026-10-27)
              'phase_options', 'set_manual', 'clear_manual', 'get_manual',
              'phase_log', 'manual_summary',
              # 讲解图(2026-10-27)
              'diagram_is_old', 'render_mistake_diagram', 'gen_diagram',
              'gen_diagram_for_mistake', 'gen_diagrams_for_paper', 'sync_diagrams_to_mistakes',
              # 限时收卷 + 错题重做(2026-10-27 下午)
              'limit_seconds', 'limit_text', 'paper_time_left', 'find_unfinished',
              'abandon_paper', 'retry_mistake', 'coverage_of', 'choice_letter')


def _quiz_missing():
    return [n for n in _QUIZ_NEED if not hasattr(quiz, n)]


_MISSING_API = _quiz_missing()
if _MISSING_API:
    # 先尝试自己修: 进程里的模块对象是旧的, 但磁盘上的 quiz.py 通常已经是新的
    try:
        import importlib
        importlib.reload(quiz)
        _MISSING_API = _quiz_missing()
    except Exception as _e:      # reload 都失败就只能让用户重启
        _MISSING_API = _MISSING_API or ['reload 失败：%s' % _e]

HAS_MANUAL_CHAPTER = all(hasattr(quiz, n) for n in
                         ('phase_options', 'set_manual', 'clear_manual', 'get_manual',
                          'phase_log', 'manual_summary'))
HAS_DIAGRAM = all(hasattr(quiz, n) for n in
                  ('render_mistake_diagram', 'gen_diagram', 'gen_diagram_for_mistake',
                   'diagram_is_old', 'gen_diagrams_for_paper')) and hasattr(diagram, 'render_svg')

if _MISSING_API:
    st.error(
        '后台服务还在跑旧版本的 quiz 模块（缺 '
        + '、'.join(_MISSING_API[:3]) + ('…' if len(_MISSING_API) > 3 else '')
        + '），所以这页打不开。\n\n'
        '**这不是你的操作问题，代码本身是对的** —— 是运行中的服务没重启，内存里还是老模块。'
        '我试过自动重载一次，没成功。\n\n'
        '- 线上（Streamlit Cloud）：右下角 **Manage app → Reboot app**，等 30 秒刷新。\n'
        '- 本地：关掉跑 streamlit 的那个窗口，重新 `streamlit run app.py`。'
    )
    st.stop()

# 每日测试模块的表(试卷/题目/掌握度/AI 题库)
quiz.init_quiz_db()

# 云端数据是临时存储(每次重新部署都会被清空), 所以一启动就先把备份里的数据找回来
_backup_start = backup.startup_sync()
# 回头再确保备份里有一份最新的(刚配好令牌时, 靠这一步建立第一份备份)
backup.maybe_push_on_startup()


# ============== 工具函数 ==============
def fmt_when(iso) -> str:
    """把 ISO 时间说成孩子看得懂的话: 今天 14:32 / 昨天 20:10 / 10-12 09:05"""
    if not iso:
        return '未知时间'
    try:
        dt = datetime.fromisoformat(str(iso))
    except Exception:
        return str(iso)[:16]
    now = datetime.now()
    if dt.date() == now.date():
        return f'今天 {dt:%H:%M}'
    if (now.date() - dt.date()).days == 1:
        return f'昨天 {dt:%H:%M}'
    return f'{dt:%m-%d %H:%M}'


def save_uploaded_image(uploaded_file) -> str:
    """保存上传图片到 uploads/, 返回相对路径"""
    if uploaded_file is None:
        return None
    ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    name = f"{ts}_{uploaded_file.name}"
    path = os.path.join(UPLOAD_DIR, name)
    with open(path, 'wb') as f:
        f.write(uploaded_file.getbuffer())
    return name  # 用相对名(数据库可移植)


def relative_to_abs(rel_path: str) -> str:
    if not rel_path:
        return None
    return os.path.join(UPLOAD_DIR, rel_path)


def prep_image_for_ai(abs_path: str, max_side: int = 2000, quality: int = 88) -> bytes:
    """
    把图片压到视觉模型友好的大小再上传:
    - 平板/手机原图动辄 3-8MB, 又慢又容易被接口拒收
    - 2000px + JPEG q88 对"抄题目"来说清晰度绰绰有余
    处理失败时退回原始字节, 不让识别中断.
    """
    try:
        from io import BytesIO
        img = Image.open(abs_path)
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        img = img.convert('RGB')
        longest = max(img.size)
        if longest > max_side:
            s = max_side / longest
            img = img.resize((int(img.width * s), int(img.height * s)), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format='JPEG', quality=quality)
        return buf.getvalue()
    except Exception:
        with open(abs_path, 'rb') as f:
            return f.read()


def run_ai_read(abs_path: str, subject: str = '通用') -> dict:
    """AI 看图统一入口(内部含第二遍校对), 所有页面都走这里"""
    raw = prep_image_for_ai(abs_path)
    return llm.read_image(raw, mime='image/jpeg', subject=subject)


def tag_pill(text: str, color: str = '#1f77b4'):
    return f'<span style="background:{color};color:white;padding:2px 8px;border-radius:8px;font-size:12px">{text}</span>'


# ============== AI 讲题标签 (L2 提问 / L3 审问 / L4 共创) ==============
FOLLOWUP_LIBRARY = {
    'more': '🔍 再讲细一点',
    'alt': '🔁 换个方法',
    'similar': '📝 出 3 道同类型变式',
    'error': '🖊️ 红笔批改(我填了答案, 请找茬)',
    'teachback': '🎤 我讲给你听(我复述一遍, 请评分)',
}


def _render_ai_tab(m):
    """讲题标签: 默认 hint(只讲思路) → 看答案(full) → 4 类追问 → 我的好提示词"""
    mid = m['id']
    sid_hint = f'ai_step_{mid}'
    sid_hist = f'ai_history_{mid}'
    sid_kid_ans = f'ai_kid_ans_{mid}'
    sid_kid_tb = f'ai_kid_tb_{mid}'
    sid_last_save = f'ai_last_save_{mid}'

    # 初始化 session_state
    st.session_state.setdefault(sid_hint, None)         # None | 'hint' | 'full'
    st.session_state.setdefault(sid_hist, [])            # [{role, text, source}, ...]
    st.session_state.setdefault(sid_kid_ans, '')
    st.session_state.setdefault(sid_kid_tb, '')
    st.session_state.setdefault(sid_last_save, None)

    # 步骤指示
    step = st.session_state[sid_hint]
    if step is None:
        st.caption('点击下方按钮开始 AI 讲题')
    else:
        st.caption('①思路 → ②看答案 → ③追问 → ④我的好提示词')

    # ====== 按钮 1: 触发讲题(hint 模式) ======
    if st.button('🤖 让我先想想，给我点思路', key=f'ai_hint_{mid}',
                 type='primary', use_container_width=True):
        if step is None:
            with st.spinner('AI 在想思路(只讲思路, 不给答案)…'):
                res = llm.solve(m['question'], m['subject'], mode='hint')
            st.session_state[sid_hist] = [{'role': 'hint', 'text': res.get('explanation', ''),
                                          'source': res.get('source', '')}]
            st.session_state[sid_hint] = 'hint'
            st.rerun()

    # ====== 历史对话渲染 ======
    for i, turn in enumerate(st.session_state[sid_hist]):
        role = turn['role']
        text = turn.get('text', '')
        if role == 'hint':
            st.info('💡 **思路提示**(没有最终答案, 让你自己想)\n\n' + (text or '(空)'))
        elif role == 'full':
            st.success('✅ **答案**\n\n' + (text or '(空)'))
        elif role == 'custom':
            label = f"✨ 我的模板 · {turn.get('prompt_name', '自定义')}"
            with st.expander(f"{label} · 展开", expanded=True):
                st.write(text or '(空)')
        elif role in FOLLOWUP_LIBRARY:
            label = FOLLOWUP_LIBRARY[role]
            with st.expander(f"{label} · 展开", expanded=True):
                st.write(text or '(空)')
                # "保存为我的好提示词"按钮
                save_key = f'save_prompt_{mid}_{i}'
                if st.button(f'⭐ 把这次追问存为"我的好提示词"', key=save_key):
                    short = FOLLOWUP_LIBRARY[role].split(' ', 1)[1] if ' ' in FOLLOWUP_LIBRARY[role] else FOLLOWUP_LIBRARY[role]
                    saved = db.append_good_prompt(mid, short)
                    if saved:
                        st.session_state[sid_last_save] = short
                        st.success(f'已收藏: {short}')
                    else:
                        st.info('这条已经收藏过啦')
                    time.sleep(0.3)
                    st.rerun()

    # ====== 按钮 2: 看答案(full 模式) ======
    if step == 'hint':
        st.divider()
        if st.button('👀 我想好啦, 看答案', key=f'ai_full_{mid}', use_container_width=True):
            with st.spinner('正在生成完整答案…'):
                res = llm.solve(m['question'], m['subject'], mode='full')
            # 不清空历史, 在末尾追加
            st.session_state[sid_hist].append({
                'role': 'full',
                'text': res.get('answer', '') or '(暂无)',
                'source': res.get('source', '')
            })
            st.session_state[sid_hint] = 'full'
            st.rerun()

    # ====== 追问区 (4 个按钮 + 1 个我讲给你听) ======
    if step in ('hint', 'full'):
        st.divider()
        st.markdown('**③ 继续追问(让 AI 帮你再深一步)**')

        # 取出最近一次的讲解文本, 作为"上轮上下文"
        last_text = (st.session_state[sid_hist][-1].get('text', '') if st.session_state[sid_hist] else '') or ''

        c1, c2 = st.columns(2)
        with c1:
            if st.button('🔍 再讲细一点', key=f'ask_more_{mid}', use_container_width=True):
                with st.spinner('细化中…'):
                    res = llm.ask_more(m['question'], m['subject'], last_text)
                st.session_state[sid_hist].append({'role': 'more', 'text': res.get('explanation', ''),
                                                  'source': res.get('source', '')})
                st.rerun()
            if st.button('📝 出 3 道同类型变式', key=f'ask_similar_{mid}', use_container_width=True):
                with st.spinner('出题中…'):
                    res = llm.ask_similar(m['question'], m['subject'], last_text, n=3)
                st.session_state[sid_hist].append({'role': 'similar', 'text': res.get('explanation', ''),
                                                  'source': res.get('source', '')})
                st.rerun()
        with c2:
            if st.button('🔁 换个方法', key=f'ask_alt_{mid}', use_container_width=True):
                with st.spinner('找另解中…'):
                    res = llm.ask_alternative(m['question'], m['subject'], last_text)
                st.session_state[sid_hist].append({'role': 'alt', 'text': res.get('explanation', ''),
                                                  'source': res.get('source', '')})
                st.rerun()
            if st.button('🖊️ 红笔批改', key=f'ask_error_{mid}', use_container_width=True):
                # 红笔批改需要孩子先填答案, 这里只展开输入框, 不直接调 AI
                pass

        # ===== 红笔批改: 输入框 + 触发按钮 =====
        with st.expander('🖊️ 红笔批改(填上你的答案, AI 帮你看错在哪)', expanded=False):
            st.session_state[sid_kid_ans] = st.text_input(
                '你的答案',
                value=st.session_state[sid_kid_ans],
                key=f'kid_ans_in_{mid}',
                placeholder='把你刚才写的答案粘进来'
            )
            corr = st.text_input('正确答案(知道就填, 不知道留空)',
                                 key=f'corr_in_{mid}',
                                 placeholder='可选')
            if st.button('请 AI 找茬', key=f'run_error_{mid}'):
                kid_ans = st.session_state[sid_kid_ans].strip()
                if not kid_ans:
                    st.warning('请先填你的答案')
                else:
                    with st.spinner('批改中…'):
                        res = llm.ask_explain_error(m['question'], m['subject'], kid_ans, corr or None)
                    st.session_state[sid_hist].append({'role': 'error',
                                                      'text': res.get('explanation', ''),
                                                      'source': res.get('source', '')})
                    st.rerun()

        # ===== 我讲给你听: 复述 + 触发按钮 =====
        with st.expander('🎤 我讲给你听(用自己的话讲一遍, AI 给你评分)', expanded=False):
            st.session_state[sid_kid_tb] = st.text_area(
                '请用自己的话讲一遍这道题',
                value=st.session_state[sid_kid_tb],
                key=f'kid_tb_in_{mid}',
                height=120,
                placeholder='例如: 这道题要先用公式把 X 算出来, 然后代入, 最后验证…'
            )
            if st.button('请 AI 点评', key=f'run_teachback_{mid}'):
                kid_tb = st.session_state[sid_kid_tb].strip()
                if not kid_tb:
                    st.warning('请先复述一遍')
                else:
                    with st.spinner('听你讲…'):
                        res = llm.ask_teach_back(m['question'], m['subject'], kid_tb)
                    st.session_state[sid_hist].append({'role': 'teachback',
                                                      'text': res.get('explanation', ''),
                                                      'source': res.get('source', '')})
                    st.rerun()

        # ===== 我自己造的追问 (提示词工坊模板) =====
        my_prompts = db.list_custom_prompts()
        if my_prompts:
            with st.expander(f'✨ 我自己造的追问 ({len(my_prompts)} 个, 在"我的工作室"里管理)', expanded=False):
                for cp in my_prompts:
                    used_tag = f" · 用过 {cp['use_count']} 次" if cp['use_count'] else ''
                    if st.button(f"✨ {cp['name']}{used_tag}", key=f'cp_{mid}_{cp["id"]}',
                                 use_container_width=True,
                                 help=cp['prompt_text'][:80]):
                        with st.spinner(f'按"{cp["name"]}"处理中…'):
                            res = llm.ask_custom(m['question'], m['subject'],
                                                 cp['prompt_text'], last_text)
                        db.bump_prompt_use(cp['id'])
                        display = f"【我的模板 · {cp['name']}】\n\n{res.get('explanation', '')}"
                        st.session_state[sid_hist].append({'role': 'custom',
                                                          'text': display,
                                                          'source': res.get('source', ''),
                                                          'prompt_name': cp['name']})
                        st.rerun()

    # ====== 我的好提示词 ======
    st.divider()
    st.markdown('**④ 我的好提示词(她点过的追问模板)**')
    saved = db.get_good_prompts(mid)
    if not saved:
        st.caption('还没有收藏. 上方每次追问后, 都能点"⭐ 保存为我的好提示词".')
    else:
        st.success(f'已收藏 {len(saved)} 条')
        for ln in saved:
            c1, c2 = st.columns([6, 1])
            with c1:
                st.markdown(f'- {ln}')
            with c2:
                if st.button('🗑️', key=f'del_prompt_{mid}_{hash(ln) & 0xffff}'):
                    db.delete_good_prompt(mid, ln)
                    st.rerun()

    # 重置按钮: 清掉当前这道题的对话状态, 重新开始
    if step is not None:
        st.divider()
        if st.button('🔄 清空这次对话, 重新开始', key=f'reset_ai_{mid}', type='secondary'):
            st.session_state[sid_hint] = None
            st.session_state[sid_hist] = []
            st.session_state[sid_kid_ans] = ''
            st.session_state[sid_kid_tb] = ''
            st.rerun()


# ============== 侧边栏 ==============
st.sidebar.title('📚 学习积累平台')
st.sidebar.caption(db.get_setting('signature', DEFAULT_SIGNATURE) or DEFAULT_SIGNATURE)
page = st.sidebar.radio('导航', [
    '📝 每日测试',
    '📷 拍照录入',
    '📖 错题本',
    '🔁 复习计划',
    '🧠 知识点图谱',
    '📊 学习统计',
    '🧪 我的工作室',
    '👨‍👩‍👧 家长端',
    '⚙️ 设置',
])

st.sidebar.divider()
ocr_status = "✅ 就绪" if ocr.is_available() else f"❌ {ocr.engine_error() or '加载中'}"
st.sidebar.markdown(f"**识图(本地)**: {ocr_status}", unsafe_allow_html=True)
llm_info = llm.config_info()
llm_status = "✅ 已启用" if llm_info['enabled'] else "⚠️ 离线"
st.sidebar.markdown(f"**AI 老师**: {llm_status} ({llm_info['provider_display']})", unsafe_allow_html=True)
_vs = llm.vision_status()
if _vs['enabled']:
    st.sidebar.markdown(f"**AI 看图**: ✅ 已启用（{_vs.get('model', '')}）", unsafe_allow_html=True)
else:
    st.sidebar.markdown('**AI 看图**: ⚪ 未开启', unsafe_allow_html=True)
    st.sidebar.caption(f"不开也能用：本地识图已就绪，只是手写体、拍歪的题识别率低些。"
                       f"开启需配视觉模型 Key（DeepSeek 不支持看图）。原因：{_vs.get('reason', '未配置')}")

# 云端备份: 明说状态, 失败就报失败, 不假装已备份
_bs = backup.status()
if not _bs['enabled']:
    st.sidebar.markdown('**云端备份**: ⚪ 未开启', unsafe_allow_html=True)
    st.sidebar.caption("数据现在只存在这台机器上，换电脑或重装系统会全部丢失。"
                       "开启需配 GitHub token（STUDY_BACKUP_TOKEN）。")
elif _bs['last_push_ok'] is False:
    st.sidebar.markdown('**云端备份**: ⚠️ 备份失败', unsafe_allow_html=True)
    if _bs.get('last_push_error'):
        st.sidebar.caption(f"最近一次失败原因：{str(_bs['last_push_error'])[:80]}")
elif _bs['last_push_at']:
    st.sidebar.markdown(f"**云端备份**: ✅ 已备份 {_bs['last_push_at']}", unsafe_allow_html=True)
else:
    st.sidebar.markdown('**云端备份**: ✅ 待备份', unsafe_allow_html=True)

# 启动时自动找回的提示(孩子看得懂的版本)
if _backup_start.get('restored'):
    st.sidebar.success(f"🔄 已从云端备份找回 {_backup_start['restored']} 条记录")

st.sidebar.divider()
st.sidebar.caption(f"v0.1 · {datetime.now().strftime('%Y-%m-%d %H:%M')}")


# ============== 页面 0: 每日测试 (按朝阳区进度出题 → 自动判卷 → 错题讲解) ==============
if page == '📝 每日测试':
    st.title('📝 每日测试')
    st.caption('按北京朝阳区教学进度出题 → 自动判卷 → 错题自动进错题本。选择题填空题本地硬判; 简答作文由 AI 给参考分, 需家长确认。')

    def _qz_reset():
        """换科目时把上一套卷子清掉, 免得答的是 A 科卷、判的是 B 科标准"""
        for k in ('qz_questions', 'qz_pid', 'qz_subject_done', 'qz_report'):
            st.session_state[k] = None

    # ---- 今日进度 ----
    st.subheader('📅 今天该练到哪')
    _pc = st.columns(3)
    for _i, _s in enumerate(quiz.SUBJECTS):
        _ph = quiz.current_phase(_s)
        with _pc[_i]:
            if _ph:
                _man = _ph.get('manual')
                st.markdown(f"**{_s}** · {_ph['label']}"
                            + ('　`📌 手动指定`' if _man else ''), unsafe_allow_html=True)
                st.caption(_ph.get('detail', ''))
                if _man:
                    st.caption(f"指定于 {_ph.get('manual_set_at') or '—'}（改回自动见下方「手动指定章节」）")
                else:
                    st.caption('按教学日历自动推算')
            else:
                st.warning(f'{_s}: 教学日历没读到, 检查 data/quiz_calendar.json')

    # ---- 手动指定章节(学校跳着讲时用) ----
    if not HAS_MANUAL_CHAPTER:
        # 后台是旧模块时不要整块崩, 说清原因并降级
        with st.expander('📌 学校跳着讲？手动指定章节（后台未就绪）'):
            st.warning('这块要新版 quiz 模块才能用，当前进程里还是旧模块。'
                       '按页面最上面的提示重启一下服务就好，其它功能不受影响。')
    else:
        with st.expander('📌 学校跳着讲？手动指定章节（选完长期生效，每次改动都记录在案）'):
            _ms = st.selectbox('哪一科', quiz.SUBJECTS, key='qz_man_subject')
            _opts = quiz.phase_options(_ms)
            if not _opts:
                st.warning('这一科的教学日历是空的，没法指定')
            else:
                _cur = quiz.get_manual(_ms)
                # 控件 key 里带上"当前已存状态"的指纹: 保存/恢复后指纹变化 -> 控件按库里真实状态重建,
                # 否则会出现"已改回自动, 界面还显示旧选择"的假象(Streamlit 的 widget 状态按 key 复用)
                _fp = f"{_cur.get('set_at', '')}|{','.join(map(str, _cur.get('indices') or []))}"
                _labels = [f"第{i}段 · {o['label']}（{o['from']} ~ {o['to']}）" for i, o in enumerate(_opts)]
                _picked = st.multiselect(
                    '现在学校在讲哪几段？（可多选，最多 3 段；选了就按这个出题，不再按日期推算）',
                    _labels,
                    default=[_labels[i] for i in (_cur.get('indices') or []) if 0 <= i < len(_labels)],
                    key=f'qz_man_phases_{_ms}_{_fp}')
                _picked_idx = [_labels.index(x) for x in _picked]

                # 二级：段内再缩到具体知识点（学校可能连小节都跳）
                _topics_pool = []
                for _i2 in _picked_idx:
                    for _t in _opts[_i2]['topics']:
                        if _t not in _topics_pool:
                            _topics_pool.append(_t)
                _topics_sel = []
                if _topics_pool:
                    _def_topics = [t for t in (_cur.get('topics') or []) if t in _topics_pool] or _topics_pool
                    _topics_sel = st.multiselect(
                        '再细一点：本段里只练哪些知识点？（默认全选，不细就用默认）',
                        _topics_pool, default=_def_topics,
                        key=f'qz_man_topics_{_ms}_{_fp}_{hash(tuple(_picked_idx)) % 9999}')
                _note = st.text_input('备注（为什么改，给自己留个说明，可不填）',
                                      value=_cur.get('note', ''), key=f'qz_man_note_{_ms}')

                st.caption(f"当前生效：**{quiz.manual_summary(_ms) or '按教学日历自动'}**")

                _b1, _b2 = st.columns(2)
                with _b1:
                    if st.button('✅ 保存并立即按这个出题', type='primary', use_container_width=True,
                                 key=f'qz_man_save_{_ms}', disabled=not _picked_idx):
                        _r = quiz.set_manual(_ms, _picked_idx, _topics_sel, _note)
                        if _r['ok']:
                            for _k in ('qz_questions', 'qz_pid', 'qz_subject_done', 'qz_report'):
                                st.session_state[_k] = None
                            st.success(_r['msg'])
                            st.rerun()
                        else:
                            st.error(_r['msg'])
                with _b2:
                    if st.button('↩️ 改回按日历自动', use_container_width=True, key=f'qz_man_clear_{_ms}'):
                        _r = quiz.clear_manual(_ms, _note)
                        for _k in ('qz_questions', 'qz_pid', 'qz_subject_done', 'qz_report'):
                            st.session_state[_k] = None
                        st.info(_r['msg'])
                        st.rerun()

                _logs = quiz.phase_log(_ms, limit=8)
                if _logs:
                    st.markdown('**改动记录**（最近的在上）')
                    for _lg in _logs:
                        _what = '手动指定' if _lg['action'] == 'set_manual' else '改回自动'
                        st.caption(f"{_lg['at'][:16].replace('T', ' ')} · {_what}：{_lg['after_text']}"
                                   + (f"　（备注：{_lg['note']}）" if _lg.get('note') else ''))

    st.divider()

    # ---- 选科出题 ----
    _c1, _c2, _c3 = st.columns([2, 2, 2])
    with _c1:
        qz_subject = st.selectbox('科目', quiz.SUBJECTS, key='qz_subject_sel', on_change=_qz_reset)
    with _c2:
        _size = st.selectbox('卷子大小', ['小测 · 10 题', '标准 · 15 题', '大卷 · 20 题', '冲刺 · 30 题'],
                             index=1, key='qz_size')
        qz_n = {'小测 · 10 题': 10, '标准 · 15 题': 15, '大卷 · 20 题': 20, '冲刺 · 30 题': 30}[_size]
    with _c3:
        _sc = quiz.scope_stats(qz_subject)
        st.metric('当前进度段可出题', f"{_sc['in_scope']} 道", f"题库共 {_sc['bank']} 道",
                  delta_color='off')
    _sc_types = _sc.get('types') or {}
    if _sc_types:
        st.caption('当前进度段题型：' + ' · '.join(
            f"{quiz.TYPE_NAMES.get(k, k)} {v}" for k, v in sorted(_sc_types.items(), key=lambda x: -x[1])))
    if qz_n > _sc['in_scope']:
        st.caption(f"⚠️ 选了 {qz_n} 题，但当前进度段只有 {_sc['in_scope']} 道 —— 差的部分会先用"
                   f"「已学过的其它章节」补复习题，再用 AI 现出；题量越大等待越久。")

    _l1, _l2, _l3 = st.columns([2, 2, 2])
    with _l1:
        _lsel = st.selectbox('⏱ 本卷限时', ['按题量自动', '宽松一些', '不限时'], index=0,
                             key='qz_limit_mode',
                             help='按题量自动：10 题 15 分 / 15 题 20 分 / 20 题 25 分 / 30 题 35 分。'
                                  '时间到会自动收卷，没做完的题进错题本并注明「时间到未作答」。')
    _lmode = {'按题量自动': '标准', '宽松一点': '宽松', '宽松一些': '宽松', '不限时': '不限时'}[_lsel]
    _plan_sec = quiz.limit_seconds(qz_n, _lmode)
    with _l2:
        st.metric('本卷限时', quiz.limit_text(_plan_sec))
    with _l3:
        qz_use_ai = st.checkbox('题库不够时用 AI 补题', value=True, key='qz_use_ai',
                                help='AI 出的题会自动验算，稍慢一点')

    if st.button('🎲 刷新一套新题', type='primary'):
        with st.spinner('正在按进度组卷, AI 出的题要过验算, 请稍等...'):
            _r = quiz.pick_questions(qz_subject, n=qz_n, use_ai=qz_use_ai)
        if not _r.get('ok'):
            st.error(f"没出成卷: {_r.get('msg', '未知原因')}。可以减少题数, 或勾上 AI 补充再试。")
        else:
            _pid = quiz.create_paper(qz_subject, _r['questions'],
                                     (_r.get('phase') or {}).get('label', ''),
                                     limit_sec=_plan_sec)
            st.session_state['qz_pid'] = _pid
            st.session_state['qz_subject_done'] = qz_subject
            st.session_state['qz_report'] = None
            st.session_state['qz_force_submit'] = None
            st.session_state['qz_started_at'] = time.time()
            # 上一套卷的答案还留在 session_state 里, 不清掉会串到新卷
            for _k in [k for k in st.session_state if str(k).startswith('qz_q_')]:
                st.session_state.pop(_k, None)
            st.success(f"出好了 {len(_r['questions'])} 题 · {_r.get('msg', '')}"
                       + (f"　限时 {quiz.limit_text(_plan_sec)}" if _plan_sec else '　不限时'))
            st.rerun()

    _pid = st.session_state.get('qz_pid')
    _qs_subject = st.session_state.get('qz_subject_done')
    _rep_now = st.session_state.get('qz_report')

    # ---- 上次没交的卷: 接着做, 别让刷新把卷子弄丢 ----
    if (not _pid or not quiz.get_paper(_pid).get('items')) and not _rep_now:
        _u = quiz.find_unfinished(qz_subject)
        if _u:
            _pid = _u['id']
            _qs_subject = _u['subject']
            st.session_state['qz_pid'] = _pid
            st.session_state['qz_subject_done'] = _qs_subject
            _ui = quiz.paper_time_left(_pid)
            st.info(f"接着上次那张没交的卷继续（{_u['subject']} · {_u.get('total') or 0} 题"
                    + (f" · 还剩 {quiz.limit_text(_ui['left'])}" if _ui.get('limited') else '')
                    + '）。不想做了就点下面的「放弃这张，重新出题」。')
            if st.button('🗑 放弃这张，重新出题'):
                quiz.abandon_paper(_pid)
                for _k in [k for k in st.session_state if str(k).startswith('qz_q_')]:
                    st.session_state.pop(_k, None)
                st.session_state['qz_pid'] = None
                st.session_state['qz_subject_done'] = None
                st.rerun()

    _items = quiz.get_paper(_pid).get('items') if _pid else []

    def _collect_answers(_pid, _items):
        """把页面上的答案写回库。每一轮都做 —— 时间到被收卷时, 来不及点交卷的答案也不会丢。

        只写非空的: 空多半是"这题还没动", 如果照写就会把上次已存的答案冲掉
        (刷新页面后控件是空的, 一轮同步就能把答案清光 —— 这个坑必须堵住)。
        """
        for _i, _it in enumerate(_items):
            _k = f'qz_q_{_i}'
            if _k not in st.session_state:
                continue
            _v = st.session_state.get(_k) or ''
            if _v == '（先选一个）':
                _v = ''
            if isinstance(_v, list):
                _v = ''.join(sorted(x.strip()[:1] for x in _v if x.strip()))
            elif (_it.get('qtype') or '') == 'choice' and str(_v).strip():
                # 界面上渲染的是整条选项文字("C. -2ab"), 存库前收成字母, 库里答案才统一
                _v = quiz.choice_letter(_v)
            if str(_v).strip():
                quiz.save_answer(_it['id'], _v)

    def _submit_quiz(_pid, _subj, _reason='manual'):
        with st.spinner('判卷中, 主观题要请 AI 老师批...'):
            _rep = quiz.grade_paper(_pid, _subj, close_reason=_reason)
        # 判完立刻给错题画讲解图(并发; 同一道题画过就复用, 不再花 AI 调用)
        _wrong_now = [x for x in quiz.get_paper(_pid)['items'] if x.get('correct') != 1]
        _rep['draw_failed'] = 0
        if _wrong_now and HAS_DIAGRAM:
            try:
                _stt = st.status('判卷完成，正在画讲解图…', expanded=True)
                _dstat = quiz.gen_diagrams_for_paper(
                    _pid, on_done=lambda d, t: _stt.update(label=f'讲解图 {d}/{t} 张…'))
                quiz.sync_diagrams_to_mistakes(_rep.get('pairs') or [])
                _stt.update(
                    label=f'讲解图好了：新画 {_dstat["ok"]} 张'
                          + (f'，{_dstat["skipped"]} 张之前画过直接复用' if _dstat['skipped'] else '')
                          + (f'，{_dstat["failed"]} 张没画出来' if _dstat['failed'] else ''),
                    state='complete', expanded=False)
                _rep['diagram_stat'] = _dstat
                _rep['draw_failed'] = _dstat['failed']
            except Exception as e:
                # 画图整段失败不能连累判卷结果, 报告照样要出来
                _rep['draw_failed'] = len(_wrong_now)
                st.warning(f'讲解图没画出来（{e}）, 分数和判卷结果不受影响。')
        st.session_state['qz_report'] = _rep
        st.session_state['qz_force_submit'] = None
        return _rep

    # ---- 答题 ----
    if _pid and _items and not _rep_now:
        _collect_answers(_pid, _items)          # 先把已写的存住(后面可能触发收卷)
        _info = quiz.paper_time_left(_pid)
        if st.session_state.get('qz_force_submit') or _info.get('over'):
            _submit_quiz(_pid, _qs_subject, 'timeout')
            st.rerun()

        st.divider()
        _type_cnt = {}
        for _q in _items:
            _tk = _q.get('qtype') or 'fill'
            _type_cnt[_tk] = _type_cnt.get(_tk, 0) + 1
        st.subheader(f'✏️ 答题（{_qs_subject} · 共 {len(_items)} 题）')
        st.caption('题型分布：' + ' · '.join(
            f"{quiz.TYPE_NAMES.get(k, k)}{v}题" for k, v in _type_cnt.items())
            + '　｜　答案边写边存，关掉页面再回来能接着做')
        _qz_countdown(_pid)

        for _i, _q in enumerate(_items):
            _source = _q.get('source') or 'seed'
            _tag = 'AI 出题·已验算' if _source == 'ai' else '题库'
            _qtype_name = quiz.TYPE_NAMES.get(_q.get('qtype'), '')
            _qtype_raw = _q.get('qtype') or 'fill'
            st.markdown(
                f"**第 {_i + 1} 题**　`{_q.get('topic', '')}`　`{_qtype_name}`　难度 {'★' * int(_q.get('difficulty') or 3)}　"
                f"<span style='color:#888888'>{_tag}</span>", unsafe_allow_html=True)
            # 阅读题: 原文跟题一起给, 孩子手里没课本也做得了
            if _q.get('passage'):
                with st.container(border=True):
                    st.caption('📖 原文')
                    st.markdown('\n'.join('> ' + _ln
                                            for _ln in quiz.md_safe(_q['passage']).splitlines()))
            st.write(quiz.md_safe(_q['stem']))
            _opts_raw = None
            if _q.get('options_json'):
                try:
                    _opts_raw = json.loads(_q['options_json'])
                except Exception:
                    _opts_raw = None
            _kkey = f'qz_q_{_i}'
            _labels = ([f"{k}. {quiz.md_safe(v)}" for k, v in _opts_raw.items()]
                       if _opts_raw else [])
            # 刷新页面/接着做: 把库里已存的答案回填到控件上。
            # 不回填的话, 控件是空的, 下一轮同步就会以为"孩子把答案擦了"。
            _saved = (_q.get('user_answer') or '').strip()
            if _saved and _kkey not in st.session_state:
                if _qtype_raw == 'choice' and _labels:
                    _hitlab = [x for x in _labels if x.startswith(_saved + '.')]
                    st.session_state[_kkey] = _hitlab[0] if _hitlab else '（先选一个）'
                elif _qtype_raw == 'multi' and _labels:
                    st.session_state[_kkey] = [x for x in _labels if x[:1] in _saved]
                elif _qtype_raw == 'judge':
                    st.session_state[_kkey] = _saved if _saved in ('对', '错') else None
                else:
                    st.session_state[_kkey] = _saved
            if _qtype_raw == 'choice' and _labels:
                _opts = ['（先选一个）'] + _labels
                st.radio('选项', _opts, key=_kkey, label_visibility='collapsed')
            elif _qtype_raw == 'multi' and _labels:
                st.multiselect('多选（少选或多选都算错）', _labels, key=_kkey,
                               label_visibility='collapsed', placeholder='可选多个选项')
            elif _qtype_raw == 'judge':
                st.radio('判断', ['对', '错'], key=_kkey, horizontal=True,
                         label_visibility='collapsed', index=None)
            elif _qtype_raw == 'answer':
                st.text_area('作答', key=_kkey, height=110, label_visibility='collapsed',
                             placeholder='分点写，一点一行' if _q.get('points_json') else '')
            else:
                st.text_input('答案', key=_kkey, label_visibility='collapsed')

        _bc1, _bc2 = st.columns([1, 2])
        with _bc1:
            _submitted = st.button('📤 交卷', type='primary', use_container_width=True)
        with _bc2:
            st.caption('交卷后才判分；中途离开不会丢答案，时间到会自动收卷。')

        _collect_answers(_pid, _items)
        if _submitted:
            _submit_quiz(_pid, _qs_subject, 'manual')
            st.rerun()

    # ---- 判卷报告 ----
    _rep = st.session_state.get('qz_report')
    if _rep:
        st.divider()
        if not _rep.get('ok'):
            st.error(f"判卷失败: {_rep.get('msg', '未知原因')}")
        else:
            st.subheader('📋 判卷结果')
            _timed_out = (_rep.get('close_reason') == 'timeout')
            if _timed_out:
                st.error(f"⏰ 时间到，已自动收卷。{_rep.get('timeout_cnt') or 0} 道题空着没写 —— "
                         f"这些题已经进错题本，标了「时间到未作答」，随时可以在错题本里重做。")
            st.metric('得分', f"{_rep['score']} 分", f"答对 {_rep['right']}/{_rep['total']}")
            if _rep.get('pending_ai'):
                st.warning(f"有 {len(_rep['pending_ai'])} 道主观题没能自动评分（AI 未配置或调用失败）, 需要家长手动批改。")
            for _i, _it in enumerate(quiz.get_paper(_pid)['items']):
                _icon = '✅' if _it['correct'] else '❌'
                _miss = _it.get('miss_reason') or ''
                _flag = ''
                if _miss == 'timeout':
                    _flag = ' · ⏰ 时间到未作答'
                elif _miss == 'blank':
                    _flag = ' · ⬜ 交卷时没写'
                with st.expander(f"{_icon} 第 {_i + 1} 题 · {_it['topic'] or _it['chapter'] or ''}{_flag}",
                                 expanded=not _it['correct']):
                    if _it.get('passage'):
                        with st.container(border=True):
                            st.caption('📖 原文')
                            st.markdown('\n'.join('> ' + _ln
                                                  for _ln in quiz.md_safe(_it['passage']).splitlines()))
                    st.write(quiz.md_safe(_it['stem']))
                    if _it['qtype'] == 'answer':
                        _pts, _hits = [], []
                        try:
                            _pts = json.loads(_it.get('points_json') or '[]')
                            _hits = json.loads(_it.get('hits_json') or '[]')
                        except Exception:
                            pass
                        if _pts:
                            _got = sum(1 for h in _hits if h)
                            _cov = int(round(_got / len(_pts) * 100)) if _pts else 0
                            st.markdown(f"**要点覆盖率 {_cov}%**（{_got}/{len(_pts)} 条，"
                                        f"{'达到 60%，算掌握' if _cov >= 60 else '不到 60%，算没掌握'}）")
                            for _pi, _p in enumerate(_pts):
                                _hitp = bool(_hits[_pi]) if _pi < len(_hits) else False
                                st.markdown((f"<span style='color:#2E8B57;font-weight:700'>✓ 你写到了</span>"
                                             if _hitp else
                                             f"<span style='color:#C0504D;font-weight:700'>✗ 这条漏了</span>")
                                            + f"　<b>{quiz.md_safe(_p.get('no') or '')}</b> "
                                            + quiz.md_safe(_p.get('text') or ''),
                                            unsafe_allow_html=True)
                        st.warning(f"AI 参考分 {_it['ai_score']}/10 —— 这是参考分, 以老师或家长为准")
                        if _it.get('ai_comment'):
                            st.info(_it['ai_comment'])
                        st.caption('参考答案要点: ' + quiz.md_safe(str(_it['answer'])[:300]))
                    else:
                        st.write(f"你的答案: **{quiz.md_safe(_it['user_answer']) or '(没作答)'}**　"
                                 f"|　正确答案: **{quiz.md_safe(_it['answer'])}**")
                    if _it.get('explain'):
                        st.caption('讲解: ' + quiz.md_safe(_it['explain']))
                    # 讲解思维导图: 错题默认都配一张
                    if not _it['correct'] and HAS_DIAGRAM:
                        _svg = quiz.render_mistake_diagram(_it)
                        if _svg:
                            st.markdown('**🧭 讲解图**')
                            st.caption('看图顺序：先看主线，再看红色标记 —— 图最下面的红色卡片写着'
                                       '「你答的是 / 错在哪一步 / 为什么错 / 应该这样想」。')
                            show_diagram(_svg)
                            if st.button('🎨 换一张图', key=f'qz_redraw_{_it["id"]}'):
                                with st.spinner('重画中...'):
                                    quiz.gen_diagram(_it['id'], force=True)
                                st.rerun()
                        else:
                            st.caption('这道题还没画讲解图。')
                            if st.button('🎨 画一张讲解图', key=f'qz_draw_{_it["id"]}'):
                                with st.spinner('画图中(约 3~6 秒)...'):
                                    _dr = quiz.gen_diagram(_it['id'])
                                if not _dr['ok']:
                                    st.error(f"没画出来: {_dr['error']}")
                                else:
                                    st.rerun()
                    if not _it['correct']:
                        if st.button('🧠 让 AI 老师再讲一遍', key=f'qz_more_{_it["id"]}'):
                            with st.spinner('AI 老师正在准备讲解...'):
                                _sol = llm.solve(_it['stem'], _qs_subject, mode='full')
                            if _sol.get('error') or not _sol.get('explanation'):
                                st.error(f"讲解失败: {_sol.get('error') or 'AI 没返回结果'}")
                            else:
                                st.markdown(_sol['explanation'])
            _ds = _rep.get('diagram_stat') or {}
            if _ds.get('failed'):
                st.warning(f"有 {_ds['failed']} 道错题的讲解图没画出来, 可以在对应题目里点「画一张讲解图」重试。")
            st.success('错题已经自动放进错题本了, 去「📖 错题本」能看到完整讲解和讲解图。')
            if st.button('🔄 再来一套', key='qz_again'):
                _qz_reset()
                st.rerun()

    # ---- 错题归纳 / 掌握度 / 历史 ----
    st.divider()
    st.subheader(f'📊 {qz_subject} 错题归纳（近 30 天）')
    _w = quiz.wrong_summary(qz_subject, days=30)
    if _w:
        st.dataframe([{'知识点': x['topic'], '所在章节': x['chapter'], '错次数': x['c']} for x in _w],
                     use_container_width=True)
    else:
        st.caption('最近 30 天这个科目还没有测试错题记录。')

    _ms = quiz.get_mastery(qz_subject)
    if _ms:
        with st.expander('📈 掌握度（决定下次出题难度）'):
            st.dataframe([{'知识点': k, '难度档': v['level'], '答对': v['right_cnt'], '答错': v['wrong_cnt']}
                          for k, v in sorted(_ms.items(), key=lambda x: x[1]['level'])],
                         use_container_width=True)
            st.caption('答对升档（题变难）, 答错降档（题变简单）, 范围 1~5 档。')

    _hist = [p for p in quiz.list_papers(qz_subject, limit=12) if p['status'] == 'done']
    if _hist:
        with st.expander('🗂 最近几次测试成绩'):
            st.dataframe([{'时间': p['created_at'][:16], '进度': p['phase_label'], '得分': p['score'],
                           '对/总': f"{p['right_cnt']}/{p['total']}", '题源': p['source']} for p in _hist],
                         use_container_width=True)

    # ---- 家长调进度 ----
    st.divider()
    with st.expander('👨‍👩‍👧 家长: 学校进度和日历不一致时, 在这里调'):
        st.caption('两种调法，**上面的「手动指定章节」优先**：')
        st.caption('· 学校整章跳着讲 → 用页面顶部的「📌 手动指定章节」，直接选章节，最准。')
        st.caption('· 只是快/慢一两周、章节顺序没变 → 用下面的天数偏移更省事。')
        for _s in quiz.SUBJECTS:
            st.markdown(f"**{_s}** · 现在按：{quiz.progress_hint(_s)}")
            _cur = quiz.get_offset(_s)
            _v = st.number_input(f'{_s} 进度偏移（天）', value=_cur, step=1, key=f'qz_off_{_s}')
            if _v != _cur:
                quiz.set_offset(_s, _v)
                st.rerun()
        st.caption('注：这一科一旦用了「手动指定章节」，天数偏移对它就不起作用了（手动指定优先）。')


# ============== 页面 1: 拍照录入 ==============
if page == '📷 拍照录入':
    st.title('📷 拍照录入错题')
    st.caption('拍题上传 → 本地自动识图(多方案择优) → 可选 AI 看图复核 → 你改一改再存. 本地识图不联网.')

    col1, col2 = st.columns([1, 1])

    with col1:
        st.subheader('1. 上传题目照片')
        src = st.radio('图片来源', ['手机拍照上传', '电脑选图'], horizontal=True)
        if src == '手机拍照上传':
            up = st.file_uploader('选照片', type=['jpg', 'jpeg', 'png', 'webp', 'heic'], key='up_phone')
        else:
            up = st.file_uploader('选图', type=['jpg', 'jpeg', 'png', 'webp'], key='up_pc')

        # AI 看图开着就自动跑, 孩子不用再多点一步(想省流量可勾掉)
        if llm.is_vision_enabled():
            st.checkbox('上传后自动用 AI 看图', value=True, key='ai_auto')

        if up:
            # 同一张图只在首次上传时识别一次(换图才重跑), 避免每次交互都重复保存+识别
            up_sig = f"{up.name}|{up.size}"
            if st.session_state.get('up_sig') != up_sig:
                rel_path = save_uploaded_image(up)
                abs_path = relative_to_abs(rel_path)
                with st.spinner('本地识图中(会试几种预处理方案, 稍等)…'):
                    ocr_res = ocr.ocr_image_strong(abs_path)
                    st.session_state['up_sig'] = up_sig
                    st.session_state['ocr_res'] = ocr_res
                    st.session_state['img_rel'] = rel_path
                    st.session_state.pop('ai_ocr', None)
                    st.session_state.pop('final_text', None)
                if llm.is_vision_enabled() and st.session_state.get('ai_auto', True):
                    with st.spinner('AI 看图中(手写题更准, 先抄一遍再校对一遍, 通常 10 秒左右; '
                                    '赶上服务商限流会自动排队重试, 最坏可能要 1-2 分钟, 别关页面)…'):
                        st.session_state['ai_ocr'] = run_ai_read(abs_path)

            try:
                img = Image.open(up)
                st.image(img, caption='预览', use_container_width=True)
            except Exception as e:
                st.warning(f'这张图打不开({type(e).__name__}), 多半是 HEIC 格式。请在手机相册里'
                           '「设置→格式→兼容性最佳」再拍, 或改成 JPG/PNG。识别结果不受影响。')

    with col2:
        st.subheader('2. 核对/编辑识别结果')
        ocr_res = st.session_state.get('ocr_res')
        _ai = st.session_state.get('ai_ocr')
        _ai_ok = bool(_ai and _ai.get('success'))
        local_text = (ocr_res.get('text') or '') if (ocr_res and ocr_res.get('success')) else ''
        ai_text = (_ai.get('text') or '') if _ai_ok else ''
        use_local = st.session_state.get('final_text') == 'local'

        # AI 结果可用时默认填 AI 的(它更准), 孩子仍可一键退回本地结果
        default_text = local_text if (use_local or not _ai_ok) else ai_text

        if ocr_res:
            if not ocr_res['success']:
                st.error(f"本地识图失败: {ocr_res.get('error', '未知')}")
                st.info('可以先手动输入题目, 图片仍会保存.')
            elif not local_text:
                st.warning('本地识图没认出文字 — 试试: 拍正一点、光线亮一点、镜头离近一点.')
            else:
                st.success(
                    f"本地识图完成 · 最佳方案「{ocr_res.get('variant') or '原图'}」"
                    f"(共试 {ocr_res.get('tried', 1)} 种) · 平均置信度 {ocr_res['avg_confidence']:.1%} "
                    f"· {len(ocr_res['lines'])} 行"
                )
                with st.expander('逐行预览 / 各方案对比', expanded=False):
                    for i, line in enumerate(ocr_res['lines'], 1):
                        st.write(f"{i}. {line}")
                    if ocr_res.get('variants'):
                        st.caption('—— 各预处理方案得分(取最高者) ——')
                        for v in ocr_res['variants']:
                            st.write(f"· {v['name']}: 置信度 {v['avg_confidence']:.1%}, "
                                     f"{len(v['lines'])} 行, 得分 {v['score']:.3f}")
                if ocr_res.get('degraded'):
                    st.caption('⚠️ 图像预处理库不可用, 本次只跑了原图直出(识别质量会差一些).')

        # ====== AI 看图: 开着就自动跑, 这里只负责展示结果和切换 ======
        if st.session_state.get('img_rel'):
            _vs = llm.vision_status()
            if _vs['enabled']:
                if _ai_ok:
                    if use_local:
                        st.info('当前题面用的是**本地识图**结果')
                        if st.button('🤖 改用 AI 看图的结果', use_container_width=True):
                            st.session_state['final_text'] = 'ai'
                            st.rerun()
                    else:
                        st.success(f"🤖 AI 看图完成({_ai.get('model')}) — 已自动填入下方题面, 保存前扫一眼")
                        if st.button('↩️ AI 认错了? 改用本地识图结果', use_container_width=True):
                            st.session_state['final_text'] = 'local'
                            st.rerun()
                elif _ai:
                    st.error(f"AI 看图失败: {_ai.get('error')}")
                if st.button('🔁 再认一次(重新看图)', use_container_width=True):
                    with st.spinner('AI 在看图…'):
                        st.session_state['ai_ocr'] = run_ai_read(
                            relative_to_abs(st.session_state['img_rel']))
                    st.session_state.pop('final_text', None)
                    st.rerun()
            else:
                st.caption(f"🤖 AI 看图未开启({_vs['reason']}) — 现在用的是本地识图.")

        with st.form('mistake_form'):
            col_a, col_b = st.columns(2)
            with col_a:
                subject = st.selectbox('学科', SUBJECTS, index=0)
                grade = st.text_input('年级', placeholder='如 初一')
            with col_b:
                wrong_reason = st.selectbox('错因', WRONG_REASONS, index=0)
                title = st.text_input('错题简述', placeholder='如 一元二次方程求根')

            question = st.text_area('题面', value=default_text, height=160,
                                    placeholder='OCR 结果会在此显示, 可修改')

            with st.expander('孩子写的答案 & 正确答案(可选)', expanded=False):
                col_c, col_d = st.columns(2)
                with col_c:
                    my_answer = st.text_area('你的答案', height=80)
                with col_d:
                    correct_answer = st.text_area('正确答案', height=80)
                explanation = st.text_area('解答思路(可后补)', height=80)

            with st.expander('联网搜题(辅助理解)', expanded=False):
                if st.form_submit_button('🔍 联网搜索相关解答', help='当前页面不会自动跑, 提交整张题后到错题详情页可点搜'):
                    st.info('请先"保存错题", 然后到错题详情页点"联网搜题".')

            saved = st.form_submit_button('💾 保存错题', type='primary', use_container_width=True)

        if saved:
            if not question.strip():
                st.error('题面不能为空')
            else:
                img_rel = st.session_state.get('img_rel')
                kid = db.add_mistake(
                    subject=subject, grade=grade or None, title=title or None,
                    question=question.strip(),
                    answer=my_answer or None, correct_answer=correct_answer or None,
                    explanation=explanation or None, wrong_reason=wrong_reason,
                    image_path=img_rel, source='photo' if img_rel else 'typing',
                )
                st.success(f'已保存! ID = {kid}. 到"错题本"标签查看.')
                backup.push_after_change()   # 云端是临时存储, 存一条就往备份送一条
                st.balloons()


# ============== 页面 2: 错题本 ==============
elif page == '📖 错题本':
    st.title('📖 错题本')

    c1, c2, c3 = st.columns(3)
    with c1:
        sub_filter = st.selectbox('学科', ['全部'] + SUBJECTS, key='sub_filter')
    with c2:
        master_filter = st.selectbox('状态', ['全部', '未掌握', '已掌握', '⏰ 没做完的'], key='master_filter')
    with c3:
        limit = st.number_input('显示条数', 10, 500, 50, key='limit')

    _only_unfinished = (master_filter == '⏰ 没做完的')
    items = db.list_mistakes(
        subject=sub_filter,
        mastered=None if master_filter in ('全部', '⏰ 没做完的')
        else (0 if master_filter == '未掌握' else 1),
        limit=limit,
    )
    if _only_unfinished:
        items = [m for m in items if (m.get('miss_reason') or '') in ('timeout', 'blank')]

    if not items:
        st.info('还没有错题, 去"拍照录入"添加第一道吧.' if not _only_unfinished
                else '没有「没做完」的错题。')
    else:
        st.caption(f'共 {len(items)} 条')
        for m in items:
            _mr = m.get('miss_reason') or ''
            _st_txt = ('✅ 已掌握' if m['mastered']
                       else ('⏰ ' if _mr == 'timeout' else '⬜ ' if _mr == 'blank' else '⚠️ ')
                       + (m.get('wrong_reason') or '未掌握'))
            with st.expander(
                f"#{m['id']} [{m['subject']}] 📅{fmt_when(m['created_at'])} "
                f"{m.get('title') or m['question'][:40]} {_st_txt}"
            ):
                tabs = st.tabs(['题目', '✍️ 重做', '🧭 讲解图', '图片', 'AI 讲题', '搜题', '复习记录', '操作'])

                with tabs[0]:
                    if m.get('passage'):
                        st.markdown('**📖 原文**')
                        st.markdown('\n'.join('> ' + _ln
                                              for _ln in quiz.md_safe(m['passage']).splitlines()))
                    st.markdown('**题面**')
                    st.write(quiz.md_safe(m['question']))
                    if m.get('miss_reason') == 'timeout':
                        st.caption('⏰ 这道题交卷时时间到了，没写完 —— 到「✍️ 重做」里可以不限时再做一遍。')
                    if m.get('answer'):
                        st.markdown(f"**你的答案**: {quiz.md_safe(m['answer'])}")
                    if m.get('correct_answer'):
                        st.markdown(f"**正确答案**: {quiz.md_safe(m['correct_answer'])}")
                    if m.get('explanation'):
                        st.markdown(f"**解答**: {m['explanation']}")
                    st.caption(f"📅 录入于 {fmt_when(m['created_at'])}"
                               f" | 来源: {m.get('source') or '手动'}"
                               f" | 已复习 {m['review_count']} 次"
                               f" | 上次复习 {fmt_when(m.get('last_reviewed')) if m.get('last_reviewed') else '还没复习过'}")

                with tabs[1]:
                    st.caption('把这道题再做一遍 —— 答对了归到「已掌握」；还错的话, '
                               '讲解图会按你这次的答案重新画，题仍留在错题本。')
                    if m.get('miss_reason') == 'timeout':
                        st.info('⏰ 这道题上次是**时间到没做完**，不是不会 —— 现在给你不限时，慢慢想。')
                    if m.get('passage'):
                        with st.container(border=True):
                            st.caption('📖 原文')
                            st.markdown('\n'.join('> ' + _ln
                                                    for _ln in quiz.md_safe(m['passage']).splitlines()))
                    st.write(quiz.md_safe(m['question']))

                    _rk_ans = f'rd_{m["id"]}'
                    _opts = None
                    if m.get('options_json'):
                        try:
                            _opts = json.loads(m['options_json'])
                        except Exception:
                            _opts = None
                    _qt = m.get('qtype') or 'fill'
                    if _qt == 'choice' and _opts:
                        _labels = ['（先选一个）'] + [f"{k}. {quiz.md_safe(v)}" for k, v in _opts.items()]
                        _val = st.radio('选项', _labels, key=_rk_ans, label_visibility='collapsed')
                    elif _qt == 'multi' and _opts:
                        _labels = [f"{k}. {quiz.md_safe(v)}" for k, v in _opts.items()]
                        _val = st.multiselect('多选', _labels, key=_rk_ans,
                                              label_visibility='collapsed', placeholder='可选多个选项')
                    elif _qt == 'judge':
                        _val = st.radio('判断', ['对', '错'], key=_rk_ans, horizontal=True,
                                        label_visibility='collapsed', index=None)
                    elif _qt == 'answer':
                        _val = st.text_area('你的答案', key=_rk_ans, height=120,
                                            label_visibility='collapsed', placeholder='分点写，一点一行')
                    else:
                        _val = st.text_input('你的答案', key=_rk_ans, label_visibility='collapsed')
                    if st.button('✅ 提交这次的答案', key=f'rd_sub_{m["id"]}', type='primary'):
                        _v = _val or ''
                        if _v == '（先选一个）':
                            _v = ''
                        if isinstance(_v, list):
                            _v = ''.join(sorted(x.strip()[:1] for x in _v if x.strip()))
                        elif _qt == 'choice' and str(_v).strip():
                            _v = quiz.choice_letter(_v)
                        with st.spinner('判题中…'):
                            _rr = quiz.retry_mistake(m['id'], _v)
                        st.session_state[f'rd_res_{m["id"]}'] = _rr
                        backup.push_after_change()
                        st.rerun()

                    _res = st.session_state.get(f'rd_res_{m["id"]}')
                    if _res:
                        if not _res.get('ok'):
                            st.error(_res.get('msg') or '重做失败')
                        elif _res.get('correct'):
                            st.success(_res.get('msg') or '做对了')
                        else:
                            st.warning(_res.get('msg') or '还差点')
                        if _res.get('comment'):
                            st.caption(_res['comment'])
                        st.caption('要看讲解就切到「🧭 讲解图」，图最下面的红色卡片写着这次错在哪。'
                                   '（答错后讲解图会重画，第一次打开要等 3~6 秒）')

                    _hist = db.list_redos(m['id'], limit=8)
                    if _hist:
                        st.markdown('**重做记录**')
                        for _h in _hist:
                            st.caption(f"{_h['at'][:16].replace('T', ' ')} · "
                                       f"{'✅ 对' if _h['correct'] else '❌ 错'} · "
                                       f"答: {(_h['answer'] or '（没写）')[:40]}"
                                       + (f" · 覆盖率 {int(round((_h['coverage'] or 0) * 100))}%"
                                          if _h.get('coverage') is not None else ''))

                with tabs[2]:
                    if not HAS_DIAGRAM:
                        st.warning('讲解图要新版后台模块才能用，当前进程里还是旧模块。'
                                   '重启服务后这里就会恢复。')
                        _svg = ''
                    else:
                        _svg = quiz.render_mistake_diagram(m)
                    if _svg:
                        st.caption('一张图看清解法主线: 已知条件 → 关键判断 → 分情况 → 逐条结论; '
                                   '图最下面的红色卡片标出你错在哪一步、为什么错')
                        # 老版本图没有错点标记, 提示重画(不静默留着旧图)
                        if quiz.diagram_is_old(m):
                            st.info('这张图是旧版，还没标出错点。点下面的按钮重画一张带红标的。')
                            if st.button('🔴 重画一张带错点标记的图', key=f'md_upg_{m["id"]}'):
                                with st.spinner('重画中(约 3~6 秒)…'):
                                    _r = quiz.gen_diagram_for_mistake(m['id'], force=True)
                                if _r['ok']:
                                    st.rerun()
                                else:
                                    st.error(f"没画出来: {_r['error']}")
                        show_diagram(_svg)
                        if st.button('🎨 换一张图重新画', key=f'md_redraw_{m["id"]}'):
                            with st.spinner('重画中(约 3~6 秒)…'):
                                _r = quiz.gen_diagram_for_mistake(m['id'], force=True)
                            if _r['ok']:
                                st.rerun()
                            else:
                                st.error(f"没画出来: {_r['error']}")
                    else:
                        st.caption('这道错题还没有讲解图。点下面的按钮, AI 会把这题的解法画成一张思维导图。')
                        if st.button('🎨 画一张讲解图', key=f'md_draw_{m["id"]}'):
                            with st.spinner('画图中(约 3~6 秒)…'):
                                _r = quiz.gen_diagram_for_mistake(m['id'])
                            if _r['ok']:
                                st.rerun()
                            else:
                                st.error(f"没画出来: {_r['error']}")
                                if m.get('explanation'):
                                    st.caption('这道题有解析文字, 但画图失败了; 上面的「题目」里能看到文字讲解。')

                with tabs[3]:
                    if m.get('image_path'):
                        try:
                            st.image(relative_to_abs(m['image_path']), use_container_width=True)
                        except Exception as e:
                            st.error(f'图片加载失败: {e}')

                        # 以前存过的题, 如果当时没认清楚, 可以在这里重新识一次并覆盖题面
                        _rk = f'reocr_{m["id"]}'
                        _ca, _cb = st.columns(2)
                        with _ca:
                            if st.button('🔍 重新识图(本地)', key=f'rl_{m["id"]}'):
                                with st.spinner('识图中(多方案择优)…'):
                                    st.session_state[_rk] = ocr.ocr_image_strong(
                                        relative_to_abs(m['image_path']))
                        with _cb:
                            if llm.is_vision_enabled():
                                if st.button('🤖 AI 看图重识', key=f'rvi_{m["id"]}'):
                                    with st.spinner('AI 在看图(先抄一遍再校对一遍)…'):
                                        _vr = run_ai_read(relative_to_abs(m['image_path']),
                                                          subject=m['subject'])
                                        st.session_state[_rk] = {
                                            'success': _vr['success'],
                                            'text': _vr['text'],
                                            'lines': _vr['text'].splitlines(),
                                            'avg_confidence': 1.0,
                                            'error': _vr.get('error'),
                                            'variant': f"AI 看图({_vr.get('model')})",
                                        }
                            else:
                                st.caption('AI 看图未开启')

                        _rr = st.session_state.get(_rk)
                        if _rr:
                            if _rr.get('success') and _rr.get('text'):
                                st.text_area('识别结果', value=_rr['text'], height=120,
                                             key=f'rk_view_{m["id"]}')
                                st.caption(f"方案: {_rr.get('variant')}")
                                if st.button('✅ 用这个结果更新题面', key=f'rk_apply_{m["id"]}'):
                                    db.update_mistake(m['id'], question=_rr['text'])
                                    st.session_state.pop(_rk, None)
                                    st.success('题面已更新')
                                    time.sleep(0.5)
                                    st.rerun()
                            else:
                                st.error(f"重新识图失败: {_rr.get('error') or '没识别出文字'}")
                    else:
                        st.info('无图片')

                with tabs[4]:
                    st.caption(f"当前 AI 服务: {llm.config_info()['provider_display']}")
                    _render_ai_tab(m)

                with tabs[5]:
                    if st.button(f'🌐 联网搜题 #{m["id"]}', key=f'search_{m["id"]}'):
                        with st.spinner('搜索中...'):
                            res = web_search.solve_with_search(m['question'], m['subject'])
                            cn = (res.get('cn_summary') or '').strip()
                            if cn and not cn.startswith('['):
                                st.markdown('**AI 中文解读**')
                                st.write(cn)
                                st.divider()
                            for r in res['results']:
                                st.markdown(f"- [{r['title']}]({r['href']})")
                                if r.get('snippet'):
                                    st.caption(r['snippet'])
                            st.divider()
                            st.markdown('**原始网页摘要**')
                            with st.expander('展开查看(原文多为英文)', expanded=not cn):
                                st.write(res['summary'])

                with tabs[6]:
                    st.write(f"复习次数: {m['review_count']}, 上次: {m.get('last_reviewed') or '未复习'}")
                    rating = st.slider('这次复习掌握程度', 1, 4, 3, key=f'r_{m["id"]}',
                                       help='1=忘了 2=模糊 3=记得 4=熟练')
                    note = st.text_input('备注', key=f'n_{m["id"]}')
                    if st.button(f'✅ 标记已复习 #{m["id"]}', key=f'rv_{m["id"]}'):
                        db.mark_reviewed(m['id'], rating, note)
                        backup.push_after_change()
                        st.success('已记录')
                        time.sleep(0.5)
                        st.rerun()

                with tabs[7]:
                    st.markdown('**✏️ 修改这条错题**(识图认错了? 直接在这里改, 不用重新拍)')
                    new_q = st.text_area('题面', value=m['question'], height=120, key=f'q_{m["id"]}')
                    new_ans = st.text_input('孩子当时的答案', value=m.get('answer') or '', key=f'ans_{m["id"]}')
                    new_reason = st.selectbox('错因', WRONG_REASONS,
                                              index=WRONG_REASONS.index(m['wrong_reason']) if m.get('wrong_reason') in WRONG_REASONS else 0,
                                              key=f'reason_{m["id"]}')
                    new_corr = st.text_input('正确答案', value=m.get('correct_answer') or '', key=f'corr_{m["id"]}')
                    new_exp = st.text_area('补充思路', value=m.get('explanation') or '', key=f'exp_{m["id"]}')
                    if st.button(f'💾 保存修改 #{m["id"]}', key=f'sv_{m["id"]}'):
                        db.update_mistake(m['id'],
                                          question=new_q.strip() or m['question'],
                                          answer=new_ans or None,
                                          wrong_reason=new_reason,
                                          correct_answer=new_corr or None,
                                          explanation=new_exp or None)
                        backup.push_after_change()
                        st.success('已更新')
                        time.sleep(0.5)
                        st.rerun()

                    # ---- 删除: 两步确认, 防止孩子手滑把错题点没了 ----
                    st.divider()
                    _ck = f'confirm_del_{m["id"]}'
                    if st.session_state.get(_ck):
                        st.warning(f'确定要删除 #{m["id"]} 这条错题吗? 删了就找不回来了!')
                        _c1, _c2 = st.columns(2)
                        if _c1.button(f'❗ 确认删除 #{m["id"]}', key=f'delyes_{m["id"]}', type='primary'):
                            # 顺带清理这张题的图片文件(只动上传目录里的, 不碰别处)
                            _img = m.get('image_path')
                            if _img:
                                try:
                                    _p = relative_to_abs(_img)
                                    if _p and os.path.exists(_p) and \
                                       os.path.abspath(_p).startswith(os.path.abspath(UPLOAD_DIR)):
                                        os.remove(_p)
                                except Exception:
                                    pass  # 图片删不掉不影响条目删除
                            db.delete_mistake(m['id'])
                            backup.push_after_change(force=True)   # 删除必须立刻同步, 否则会被备份复活
                            st.session_state.pop(_ck, None)
                            st.success('已删除')
                            time.sleep(0.5)
                            st.rerun()
                        if _c2.button(f'↩️ 不删了 #{m["id"]}', key=f'delno_{m["id"]}'):
                            st.session_state.pop(_ck, None)
                            st.rerun()
                    else:
                        if st.button(f'🗑️ 删除这条错题 #{m["id"]}', key=f'del_{m["id"]}'):
                            st.session_state[_ck] = True
                            st.rerun()


# ============== 页面 3: 复习计划 ==============
elif page == '🔁 复习计划':
    st.title('🔁 复习计划 (艾宾浩斯曲线)')
    st.caption('基于艾宾浩斯遗忘曲线 1/2/4/7/15/30 天的间隔, 自动算哪些到日子该复习.')

    plan = db.get_review_plan(limit=20)
    if not plan:
        st.success('🎉 没有需要复习的题目!')
    else:
        st.warning(f'有 {len(plan)} 道题到期, 建议优先复习超期最久的')
        for m in plan:
            overdue = m['due_days']
            color = '🔴' if overdue > 7 else '🟡' if overdue > 0 else '🟢'
            with st.container():
                c1, c2, c3 = st.columns([6, 2, 2])
                with c1:
                    st.markdown(f"{color} **#{m['id']}** [{m['subject']}] {m.get('title') or m['question'][:50]}")
                with c2:
                    st.caption(f'超期 {overdue} 天 · 录入 {fmt_when(m["created_at"])}')
                with c3:
                    if st.button(f'去复习', key=f'go_{m["id"]}'):
                        st.session_state['jump_mistake'] = m['id']
                        st.info(f'请到"错题本"标签查找 #{m["id"]}')


# ============== 页面 4: 知识点图谱 ==============
elif page == '🧠 知识点图谱':
    st.title('🧠 知识点图谱')
    st.caption('按学科和章节聚合错题, 高频错的知识点标红.')

    ks = db.list_knowledge()
    if not ks:
        st.info('暂未关联知识点. 在错题录入/编辑时, 选择"知识点"标签即可.')
    else:
        st.dataframe(ks, use_container_width=True)

    with st.expander('➕ 新建知识点', expanded=False):
        with st.form('add_kg'):
            c1, c2 = st.columns(2)
            with c1:
                ksub = st.selectbox('学科', SUBJECTS, key='ksub')
                kname = st.text_input('知识点名', placeholder='如 一元二次方程求根公式')
            with c2:
                kchapter = st.text_input('所属章节', placeholder='如 第二章 一元二次方程')
                kdiff = st.slider('难度', 1, 5, 2)
            if st.form_submit_button('保存'):
                if kname.strip():
                    db.add_knowledge(ksub, kname.strip(), kchapter or None, kdiff)
                    st.success(f'已添加: {kname}')
                    st.rerun()
                else:
                    st.error('名称不能为空')

    # 简易图谱: 按学科聚合显示
    st.divider()
    st.subheader('错题分布(按学科)')
    stats = db.stats_overview()
    by = stats['by_subject']
    if by:
        for s in by:
            c1, c2, c3 = st.columns([3, 1, 5])
            with c1:
                st.markdown(f"**{s['subject']}**")
            with c2:
                st.markdown(f"{s['c']} 题")
            with c3:
                st.progress(min(s['c'] / max(b['c'] for b in by), 1.0))


# ============== 页面 5: 学习统计 ==============
elif page == '📊 学习统计':
    st.title('📊 学习统计')
    s = db.stats_overview()

    c1, c2, c3 = st.columns(3)
    c1.metric('总错题', s['total'])
    c2.metric('已掌握', s['mastered'])
    c3.metric('未掌握', s['unmastered'])

    st.divider()
    st.subheader('学科分布')
    if s['by_subject']:
        st.bar_chart({r['subject']: r['c'] for r in s['by_subject']})
    else:
        st.info('还没有错题')

    st.divider()
    st.subheader('近 30 天录入趋势')
    if s['recent_30d']:
        st.line_chart({r['d']: r['c'] for r in s['recent_30d']})
    else:
        st.info('近 30 天没录入')


# ============== 页面 5.5: 我的工作室 (孩子自主运营入口) ==============
elif page == '🧪 我的工作室':
    st.title('🧪 我的工作室')
    st.caption('这个平台是你的. 觉得哪里不好用? 想要新功能? 自己造追问? 都在这里.')

    tab_wish, tab_forge, tab_guide, tab_sign = st.tabs(['💡 许愿池', '✨ 提示词工坊', '📖 使用手册', '✍️ 我的签名'])

    # ---------- ① 许愿池 ----------
    with tab_wish:
        st.subheader('💡 许愿池')
        st.caption('发现平台的不足就记下来. 记录本身就是一种能力 —— 你在像产品经理一样思考.')

        with st.form('wish_form', clear_on_submit=True):
            w_cat = st.selectbox('类型', ['不好用', '想要功能', '想法'])
            w_content = st.text_area('具体说说',
                                      placeholder='例如: 错题本翻页太麻烦, 想要搜索框\n例如: 想要一个"每周错题排行"页面',
                                      height=100)
            if st.form_submit_button('📮 投递愿望', type='primary', use_container_width=True):
                if w_content.strip():
                    db.add_wish(w_content.strip(), w_cat)
                    st.success('愿望已投递! 以后自己(或让 AI)来实现它.')
                else:
                    st.error('内容不能为空')

        st.divider()
        st.subheader('我的愿望清单')
        wishes = db.list_wishes(limit=100)
        if not wishes:
            st.info('还没有愿望. 用得越认真, 越容易发现值得改进的地方.')
        else:
            status_emoji = {'新': '🆕', '尝试中': '🔧', '已解决': '✅', '放弃': '🗑️'}
            for w in wishes:
                emoji = status_emoji.get(w['status'], '❓')
                with st.expander(f"{emoji} #{w['id']} [{w['category']}] {w['content'][:60]}"):
                    st.caption(f"投递于 {w['created_at']}" +
                               (f" · 更新于 {w['updated_at']}" if w.get('updated_at') else ''))
                    c1, c2 = st.columns([3, 2])
                    with c1:
                        new_status = st.selectbox('状态', ['新', '尝试中', '已解决', '放弃'],
                                                  index=['新', '尝试中', '已解决', '放弃'].index(w['status'])
                                                  if w['status'] in ['新', '尝试中', '已解决', '放弃'] else 0,
                                                  key=f'ws_{w["id"]}')
                        new_note = st.text_input('我的实验记录(可选)', value=w.get('note') or '',
                                                 key=f'wn_{w["id"]}',
                                                 placeholder='记下你尝试了什么、结果如何')
                    with c2:
                        st.write('')  # 对齐
                        if st.button('💾 更新', key=f'wu_{w["id"]}', use_container_width=True):
                            db.update_wish(w['id'], status=new_status, note=new_note or None)
                            st.success('已更新')
                            time.sleep(0.3)
                            st.rerun()
                        if st.button('🗑️ 删除', key=f'wd_{w["id"]}', use_container_width=True):
                            db.delete_wish(w['id'])
                            st.rerun()

    # ---------- ② 提示词工坊 ----------
    with tab_forge:
        st.subheader('✨ 提示词工坊')
        st.caption('自己写追问模板, 写好后在每道题的"AI 讲题"里一键使用. 不用写代码, 你就是在开发这个平台.')

        with st.form('forge_form', clear_on_submit=True):
            f_name = st.text_input('模板名字(简短好记)',
                                   placeholder='例如: 打比方版讲解 / 中考真题版')
            f_tpl = st.text_area('模板内容(你想让 AI 做什么)',
                                 placeholder=('例如: 请把这道题的解法用一个生活中的打比方重新讲一遍, '
                                              '让我秒懂. 最后总结一句"核心思想".'),
                                 height=120)
            st.caption('提示: 想让模板自动带上题目, 可以在内容里写 {题目} 两个汉字加大括号; '
                       '不写也行, 系统会自动把题目附在后面.')
            if st.form_submit_button('🔨 打造模板', type='primary', use_container_width=True):
                if f_name.strip() and f_tpl.strip():
                    db.add_custom_prompt(f_name.strip(), f_tpl.strip())
                    st.success(f'模板「{f_name.strip()}」已打造! 去"错题本 → AI 讲题"里试试.')
                else:
                    st.error('名字和内容都要填')

        st.divider()
        st.subheader('我的模板库')
        prompts = db.list_custom_prompts()
        if not prompts:
            st.info('还没有模板. 试着造一个: 比如让 AI 用"漫画分镜"的方式讲题?')
        else:
            for cp in prompts:
                used = cp.get('use_count') or 0
                last = cp.get('last_used') or '没用过'
                with st.expander(f"✨ {cp['name']} · 已用 {used} 次 · 最近: {last}"):
                    st.text_area('模板内容', value=cp['prompt_text'], height=100,
                                 disabled=True, key=f'cpv_{cp["id"]}')
                    st.caption(f'创建于 {cp["created_at"]}')
                    if st.button('🗑️ 删除这个模板', key=f'cpd_{cp["id"]}'):
                        db.delete_custom_prompt(cp['id'])
                        st.rerun()

    # ---------- ③ 使用手册 ----------
    with tab_guide:
        st.subheader('📖 平台使用手册')
        guide_path = os.path.join(APP_DIR, 'KIDS_GUIDE.md')
        if os.path.exists(guide_path):
            with open(guide_path, 'r', encoding='utf-8') as f:
                st.markdown(f.read())
        else:
            st.info('手册文件(KIDS_GUIDE.md)不存在, 仓库里找.')

    # ---------- ④ 我的签名 ----------
    with tab_sign:
        st.subheader('✍️ 我的签名')
        st.caption('左上角标题下面那句话, 现在归你了 —— 想写什么写什么, 20 个汉字以内.')

        cur_sig = db.get_setting('signature', DEFAULT_SIGNATURE) or DEFAULT_SIGNATURE
        # 用 session_state 托管输入框, 便于"恢复默认"时同步刷新
        if 'sig_input' not in st.session_state:
            st.session_state['sig_input'] = cur_sig

        sig = st.text_input('我的个性签名', key='sig_input', max_chars=SIGNATURE_MAX,
                            placeholder='例如: 每天进步一点点 / 数学, 我来啦')

        c1, c2, c3 = st.columns([2, 1, 1])
        with c1:
            if st.button('💾 保存签名', type='primary', use_container_width=True):
                s = (sig or '').strip()
                if not s:
                    st.error('签名不能为空哦')
                elif len(s) > SIGNATURE_MAX:
                    st.error(f'太长了, {SIGNATURE_MAX} 个汉字以内')
                else:
                    db.set_setting('signature', s)
                    st.success('已保存! 看左上角 👈')
                    time.sleep(0.4)
                    st.rerun()
        with c2:
            if st.button('↩️ 恢复默认', use_container_width=True):
                db.set_setting('signature', DEFAULT_SIGNATURE)
                st.session_state['sig_input'] = DEFAULT_SIGNATURE
                st.rerun()
        with c3:
            st.caption(f'已写 {len(sig or "")}/{SIGNATURE_MAX} 字')

        st.divider()
        st.caption('提示: 这句话会一直显示在左上角, 每次打开平台都能看到 —— 像给自己取的一句口头禅.')


# ============== 页面 6: 家长端 ==============
elif page == '👨‍👩‍👧 家长端':
    st.title('👨‍👩‍👧 家长端')
    st.caption('查看孩子学习情况 / 配置系统 / 数据导出')

    pin = db.ensure_parent_pin()
    st.info(f'🔐 家长 PIN 码: **{pin}** (首次访问自动生成, 可在"设置"页改)')

    with st.expander('🔓 登录家长端', expanded=False):
        with st.form('parent_login'):
            entered = st.text_input('输入 PIN 码', type='password', max_chars=4)
            if st.form_submit_button('登录'):
                if entered == pin:
                    st.session_state['parent_authed'] = True
                    st.success('已登录')
                else:
                    st.error('PIN 错误')

    if st.session_state.get('parent_authed'):
        st.success('✅ 已登录家长端')
        s = db.stats_overview()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric('总错题', s['total'])
        c2.metric('已掌握', s['mastered'])
        c3.metric('未掌握', s['unmastered'])
        c4.metric('⭐ 好提示词', s.get('good_prompts_count', 0))

        st.divider()
        st.subheader('最近录入的 20 条错题')
        items = db.list_mistakes(limit=20)
        for m in items:
            n_prompts = len(db.get_good_prompts(m['id']))
            prompt_tag = f' · ⭐{n_prompts}' if n_prompts else ''
            st.markdown(
                f"#{m['id']} [{m['subject']}] {m.get('title') or m['question'][:30]} "
                f"— {m.get('wrong_reason') or '-'} (复习{m['review_count']}次) "
                f"{'✅' if m['mastered'] else ''}{prompt_tag}"
            )

        st.divider()
        st.subheader('⭐ 她最近收藏的好提示词')
        recent = db.list_recent_good_prompts(limit=10)
        if not recent:
            st.caption('还没有. 孩子在 AI 讲题标签点"⭐ 保存为我的好提示词"后, 会出现在这里.')
        else:
            for mid, ln in recent:
                st.markdown(f"- `#{mid}` — {ln}")

        st.divider()
        st.subheader('☁️ 云端自动备份')
        _bs = backup.status()
        # 防御: 部署切换瞬间进程里可能还是旧 db 模块(没有 count_all), 别让过渡态把整页崩掉
        try:
            _cnt = db.count_all()
            st.caption(
                f"当前库: 错题 {_cnt['mistakes']} 条 / 复习记录 {_cnt['reviews']} 条 / "
                f"知识点 {_cnt['knowledge']} 个 / 提示词模板 {_cnt['custom_prompts']} 个 / 愿望 {_cnt['wishes']} 条"
            )
        except AttributeError:
            st.caption('当前库: 条数统计暂不可用(应用刚更新, 重启一次即恢复)')

        if not _bs['enabled']:
            st.error('自动备份未开启 —— 云端数据是临时存储, 一重启就会被清空, 目前只能靠手动下载。')
            with st.expander('怎么开启(免费, 2 分钟)', expanded=True):
                st.markdown(
                    '1. 打开 `https://github.com/settings/tokens` → **Generate new token (classic)**\n'
                    '2. **只勾选 `gist` 一项**, 有效期选 No expiration, 点生成并复制\n'
                    '3. 回到本应用 → 右下角 **Settings → Secrets**, 加一行:\n'
                    '   `STUDY_BACKUP_TOKEN = "粘贴刚才的令牌"`\n'
                    '4. 保存, 应用自动重启后侧边栏「云端备份」会变成 ✅ 已备份'
                )
        else:
            _ca, _cb = st.columns(2)
            with _ca:
                if st.button('☁️ 立即备份', use_container_width=True):
                    with st.spinner('正在备份到你自己的私有 Gist…'):
                        _r = backup.push(force=True)
                    if _r['ok']:
                        st.success('备份完成')
                    else:
                        st.error(_r.get('error') or '备份失败')
            with _cb:
                if st.button('🔄 立即找回', use_container_width=True):
                    with st.spinner('正在从备份找回本地缺的数据…'):
                        _r2 = backup.pull(force=True)
                    if _r2['ok']:
                        if _r2['restored']:
                            st.success(f"找回 {_r2['restored']} 条")
                            time.sleep(0.6)
                            st.rerun()
                        else:
                            st.info('备份里没有本地缺的数据, 不用恢复')
                    else:
                        st.error(_r2.get('error') or '找回失败')

            _rc = _bs['remote_counts'] or {}
            _detail = []
            if _bs['last_push_at']:
                _detail.append(f"上次备份: {_bs['last_push_at']}")
            if _rc:
                _detail.append(f"备份里有 {_rc.get('mistakes', 0)} 条错题")
            if _bs['last_pull_at']:
                _detail.append(f"上次找回: {_bs['last_pull_at']}")
                if _bs['last_pull_restored']:
                    _detail.append(f"找回 {_bs['last_pull_restored']} 条")
            if _detail:
                st.caption(' · '.join(_detail))
            if _bs['last_push_error']:
                st.warning(f"上次备份没成功: {_bs['last_push_error']}")
            if _bs['gist_url']:
                st.caption(f"备份存放在你账号的私有 Gist: {_bs['gist_url']}")

        st.divider()
        st.subheader('💾 手动备份(下载到电脑)')
        st.caption('自动备份之外的另一道保险: 存一份在自己电脑上, 任何时候都能上传恢复。')
        import json as _json
        st.download_button(
            label='📥 下载完整备份(JSON)',
            data=_json.dumps(db.export_all(), ensure_ascii=False, indent=2),
            file_name=f"study_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.json",
            mime='application/json',
            use_container_width=True,
        )
        with st.expander('♻️ 从备份文件恢复', expanded=False):
            _up = st.file_uploader('选择之前下载的备份 JSON', type=['json'], key='restore_up')
            _merge = st.checkbox('合并导入(跳过已存在的编号, 不覆盖现有内容)', value=True,
                                 key='restore_merge')
            if _up is not None:
                st.caption(f'待导入文件: {_up.name} ({_up.size / 1024:.1f} KB)')
                if st.button('开始恢复', type='primary', key='restore_go'):
                    try:
                        _r = db.import_all(_json.loads(_up.read().decode('utf-8')), merge=_merge)
                        if _r['ok']:
                            backup.push_after_change()   # 恢复完立刻同步一份, 免得刚恢复又丢
                            st.success(f"导入完成: {_r['imported']}")
                            if any(_r['skipped'].values()):
                                st.caption(f"跳过(已存在或格式不符): {_r['skipped']}")
                            time.sleep(0.8)
                            st.rerun()
                        else:
                            st.error(_r['error'])
                    except Exception as _e:
                        st.error(f'恢复失败: {_e}')

        st.divider()
        st.subheader('⚠️ 危险操作')
        with st.expander('重置 PIN 码', expanded=False):
            new_pin = st.text_input('新 PIN 码(4 位数字)', max_chars=4)
            if st.button('保存新 PIN'):
                if new_pin.isdigit() and len(new_pin) == 4:
                    db.set_setting('parent_pin', new_pin)
                    st.success(f'新 PIN: {new_pin}')
                else:
                    st.error('必须是 4 位数字')

        with st.expander('清空所有错题(慎用)', expanded=False):
            if st.button('清空', type='secondary'):
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM mistakes")
                    conn.execute("DELETE FROM reviews")
                st.warning('已清空, 刷新页面查看.')


# ============== 页面 7: 设置 ==============
elif page == '⚙️ 设置':
    st.title('⚙️ 设置')

    st.subheader('AI 老师配置')
    info = llm.config_info()
    st.json(info)

    st.markdown("""
    **接入 LLM 步骤**:

    **本地运行** — 启动 streamlit 前设置环境变量:
    ```
    set STUDY_LLM_PROVIDER=deepseek
    set STUDY_LLM_API_KEY=sk-xxx
    set STUDY_LLM_MODEL=deepseek-chat
    ```

    **云端部署 (share.streamlit.io)** — 不用设环境变量, 在 Streamlit Cloud 后台
    你的 App → Settings → Secrets 里粘贴:
    ```toml
    STUDY_LLM_PROVIDER = "deepseek"
    STUDY_LLM_API_KEY = "sk-xxx"
    STUDY_LLM_MODEL = "deepseek-chat"
    ```

    推荐服务商 (任选):
    - **DeepSeek** (国产, 极便宜, ¥1/百万token) — `STUDY_LLM_PROVIDER=openai`, `STUDY_LLM_BASE_URL=https://api.deepseek.com`, `STUDY_LLM_MODEL=deepseek-chat`
    - **通义千问** (阿里, 国内快) — `STUDY_LLM_PROVIDER=openai`, `STUDY_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1`, `STUDY_LLM_MODEL=qwen-turbo`
    - **智谱 GLM** (国产, 学术场景强) — 类似配置, base_url 不同

    重启后"AI 老师"状态会变成 ✓ 已启用
    """)

    st.divider()
    st.subheader('家长 PIN')
    cur = db.get_setting('parent_pin', '(未设置)')
    st.write(f'当前 PIN: **{cur}**')

    st.divider()
    st.subheader('数据位置')
    st.code(f"数据库: {db.DB_PATH}\n图片: {UPLOAD_DIR}")

    st.divider()
    st.subheader('关于')
    st.markdown("""
    - **学习积累平台 v0.1**
    - 纯本地: 数据存 SQLite, 图片存本地
    - AI 老师: 默认离线, 预留接口
    - 13 岁孩子学习用, 家长端可监控
    """)
