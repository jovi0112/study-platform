"""
学习积累平台 - Streamlit 主应用
- 标签: 拍照录入 / 错题本 / 复习计划 / 知识点图谱 / 家长端
- 全部本地 SQLite 存储, 图像本地保存
"""
import os
import sys
import time
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

# ============== 路径 ==============
APP_DIR = os.path.dirname(__file__)
UPLOAD_DIR = os.path.join(APP_DIR, 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)
SUBJECTS = ['数学', '语文', '英语', '物理', '化学', '生物', '历史', '地理', '政治', '其他']
WRONG_REASONS = ['概念不清', '计算失误', '审题错误', '公式记错', '思路不对', '粗心', '时间不够', '没学过']

# 侧边栏签名(孩子可在「我的工作室 → 我的签名」里改, 20 汉字以内)
DEFAULT_SIGNATURE = '13 岁孩子学习错题本地化记录'
SIGNATURE_MAX = 20

st.set_page_config(
    page_title='学习积累平台',
    page_icon='📚',
    layout='wide',
    initial_sidebar_state='expanded',
)

# 初始化数据库
db.init_db()
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
                st.markdown(f"**{_s}** · {_ph['label']}")
                st.caption(_ph.get('detail', ''))
            else:
                st.warning(f'{_s}: 教学日历没读到, 检查 data/quiz_calendar.json')

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
    qz_use_ai = st.checkbox('题库不够时用 AI 出题补充（会自动验算, 稍慢）', value=True, key='qz_use_ai')

    if st.button('🎲 刷新一套新题', type='primary'):
        with st.spinner('正在按进度组卷, AI 出的题要过验算, 请稍等...'):
            _r = quiz.pick_questions(qz_subject, n=qz_n, use_ai=qz_use_ai)
        if not _r.get('ok'):
            st.error(f"没出成卷: {_r.get('msg', '未知原因')}。可以减少题数, 或勾上 AI 补充再试。")
        else:
            _pid = quiz.create_paper(qz_subject, _r['questions'], (_r.get('phase') or {}).get('label', ''))
            st.session_state['qz_questions'] = _r['questions']
            st.session_state['qz_pid'] = _pid
            st.session_state['qz_subject_done'] = qz_subject
            st.session_state['qz_report'] = None
            st.success(f"出好了 {len(_r['questions'])} 题 · {_r.get('msg', '')}")

    _qs = st.session_state.get('qz_questions')
    _pid = st.session_state.get('qz_pid')
    _qs_subject = st.session_state.get('qz_subject_done')

    # ---- 答题 ----
    if _qs and _pid and not st.session_state.get('qz_report'):
        st.divider()
        _type_cnt = {}
        for _q in _qs:
            _type_cnt[_q.get('qtype', 'fill')] = _type_cnt.get(_q.get('qtype', 'fill'), 0) + 1
        _type_txt = ' · '.join(f"{quiz.TYPE_NAMES.get(k, k)}{v}题" for k, v in _type_cnt.items())
        st.subheader(f'✏️ 答题（{_qs_subject} · 共 {len(_qs)} 题）')
        st.caption(f"题型分布：{_type_txt}")
        with st.form('qz_form'):
            for _i, _q in enumerate(_qs):
                if _q.get('source') == 'ai':
                    _tag = 'AI 出题·已验算'
                elif _q.get('review'):
                    _tag = '题库·跨章节复习'
                else:
                    _tag = '题库'
                _qtype_name = quiz.TYPE_NAMES.get(_q.get('qtype'), '')
                st.markdown(
                    f"**第 {_i + 1} 题**　`{_q.get('topic', '')}`　`{_qtype_name}`　难度 {'★' * int(_q.get('difficulty', 3))}　"
                    f"<span style='color:#888888'>{_tag}</span>", unsafe_allow_html=True)
                st.write(_q['stem'])
                if _q.get('qtype') == 'choice' and _q.get('options'):
                    _opts = ['（先选一个）'] + [f"{k}. {v}" for k, v in _q['options'].items()]
                    st.radio('选项', _opts, key=f'qz_q_{_i}', label_visibility='collapsed')
                elif _q.get('qtype') == 'multi' and _q.get('options'):
                    _opts = [f"{k}. {v}" for k, v in _q['options'].items()]
                    st.multiselect('多选（少选或多选都算错）', _opts, key=f'qz_q_{_i}',
                                   label_visibility='collapsed', placeholder='可选多个选项')
                elif _q.get('qtype') == 'judge':
                    st.radio('判断', ['对', '错'], key=f'qz_q_{_i}', horizontal=True,
                             label_visibility='collapsed', index=None)
                elif _q.get('qtype') == 'answer':
                    st.text_area('作答', key=f'qz_q_{_i}', height=110, label_visibility='collapsed')
                else:
                    st.text_input('答案', key=f'qz_q_{_i}', label_visibility='collapsed')
            _submitted = st.form_submit_button('📤 交卷', type='primary', use_container_width=True)

        if _submitted:
            _items = quiz.get_paper(_pid)['items']
            for _i, _it in enumerate(_items):
                _v = st.session_state.get(f'qz_q_{_i}', '')
                if _v == '（先选一个）':
                    _v = ''
                if isinstance(_v, list):
                    # 多选题存成 ABC 形式, 空格分隔的选项文字去掉
                    _v = ''.join(sorted(x.strip()[:1] for x in _v if x.strip()))
                quiz.save_answer(_it['id'], _v)
            with st.spinner('判卷中, 主观题要请 AI 老师批...'):
                _rep = quiz.grade_paper(_pid, _qs_subject)
            st.session_state['qz_report'] = _rep
            st.rerun()

    # ---- 判卷报告 ----
    _rep = st.session_state.get('qz_report')
    if _rep:
        st.divider()
        if not _rep.get('ok'):
            st.error(f"判卷失败: {_rep.get('msg', '未知原因')}")
        else:
            st.subheader('📋 判卷结果')
            st.metric('得分', f"{_rep['score']} 分", f"答对 {_rep['right']}/{_rep['total']}")
            if _rep.get('pending_ai'):
                st.warning(f"有 {len(_rep['pending_ai'])} 道主观题没能自动评分（AI 未配置或调用失败）, 需要家长手动批改。")
            for _i, _it in enumerate(quiz.get_paper(_pid)['items']):
                _icon = '✅' if _it['correct'] else '❌'
                with st.expander(f"{_icon} 第 {_i + 1} 题 · {_it['topic'] or _it['chapter'] or ''}",
                                 expanded=not _it['correct']):
                    st.write(_it['stem'])
                    if _it['qtype'] == 'answer':
                        st.warning(f"AI 参考分 {_it['ai_score']}/10 —— 这是参考分, 以老师或家长为准")
                        if _it.get('ai_comment'):
                            st.info(_it['ai_comment'])
                        st.caption('参考答案要点: ' + str(_it['answer'])[:300])
                    else:
                        st.write(f"你的答案: **{_it['user_answer'] or '(没作答)'}**　|　正确答案: **{_it['answer']}**")
                    if _it.get('explain'):
                        st.caption('讲解: ' + _it['explain'])
                    if not _it['correct']:
                        if st.button('🧠 让 AI 老师再讲一遍', key=f'qz_more_{_it["id"]}'):
                            with st.spinner('AI 老师正在准备讲解...'):
                                _sol = llm.solve(_it['stem'], _qs_subject, mode='full')
                            if _sol.get('error') or not _sol.get('explanation'):
                                st.error(f"讲解失败: {_sol.get('error') or 'AI 没返回结果'}")
                            else:
                                st.markdown(_sol['explanation'])
            st.success('错题已经自动放进错题本了, 去「📖 错题本」能看到完整讲解。')
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
        st.caption('正数 = 学校讲得慢, 往后推; 负数 = 学校讲得快, 往前提。改完立刻生效。')
        for _s in quiz.SUBJECTS:
            _cur = quiz.get_offset(_s)
            _v = st.number_input(f'{_s} 进度偏移（天）', value=_cur, step=1, key=f'qz_off_{_s}')
            if _v != _cur:
                quiz.set_offset(_s, _v)
                st.rerun()


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
                    with st.spinner('AI 看图中(手写题更准, 会先抄一遍再校对一遍, 约 10-20 秒)…'):
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
        master_filter = st.selectbox('状态', ['全部', '未掌握', '已掌握'], key='master_filter')
    with c3:
        limit = st.number_input('显示条数', 10, 500, 50, key='limit')

    items = db.list_mistakes(
        subject=sub_filter,
        mastered=None if master_filter == '全部' else (0 if master_filter == '未掌握' else 1),
        limit=limit,
    )

    if not items:
        st.info('还没有错题, 去"拍照录入"添加第一道吧.')
    else:
        st.caption(f'共 {len(items)} 条')
        for m in items:
            with st.expander(
                f"#{m['id']} [{m['subject']}] 📅{fmt_when(m['created_at'])} "
                f"{m.get('title') or m['question'][:40]} "
                f"{'✅ 已掌握' if m['mastered'] else '⚠️ ' + (m.get('wrong_reason') or '未掌握')}"
            ):
                tabs = st.tabs(['题目', '图片', 'AI 讲题', '搜题', '复习记录', '操作'])

                with tabs[0]:
                    st.markdown('**题面**')
                    st.write(m['question'])
                    if m.get('answer'):
                        st.markdown(f"**你的答案**: {m['answer']}")
                    if m.get('correct_answer'):
                        st.markdown(f"**正确答案**: {m['correct_answer']}")
                    if m.get('explanation'):
                        st.markdown(f"**解答**: {m['explanation']}")
                    st.caption(f"📅 录入于 {fmt_when(m['created_at'])}"
                               f" | 来源: {m.get('source') or '手动'}"
                               f" | 已复习 {m['review_count']} 次"
                               f" | 上次复习 {fmt_when(m.get('last_reviewed')) if m.get('last_reviewed') else '还没复习过'}")

                with tabs[1]:
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

                with tabs[2]:
                    st.caption(f"当前 AI 服务: {llm.config_info()['provider_display']}")
                    _render_ai_tab(m)

                with tabs[3]:
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

                with tabs[4]:
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

                with tabs[5]:
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
