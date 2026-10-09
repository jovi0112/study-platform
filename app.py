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
from PIL import Image

# 项目内部模块
import db
import ocr
import llm
import web_search

# ============== 路径 ==============
APP_DIR = os.path.dirname(__file__)
UPLOAD_DIR = os.path.join(APP_DIR, 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)
SUBJECTS = ['数学', '语文', '英语', '物理', '化学', '生物', '历史', '地理', '政治', '其他']
WRONG_REASONS = ['概念不清', '计算失误', '审题错误', '公式记错', '思路不对', '粗心', '时间不够', '没学过']

st.set_page_config(
    page_title='学习积累平台',
    page_icon='📚',
    layout='wide',
    initial_sidebar_state='expanded',
)

# 初始化数据库
db.init_db()


# ============== 工具函数 ==============
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


def tag_pill(text: str, color: str = '#1f77b4'):
    return f'<span style="background:{color};color:white;padding:2px 8px;border-radius:8px;font-size:12px">{text}</span>'


# ============== 侧边栏 ==============
st.sidebar.title('📚 学习积累平台')
st.sidebar.caption('13 岁孩子学习错题本地化记录')
page = st.sidebar.radio('导航', [
    '📷 拍照录入',
    '📖 错题本',
    '🔁 复习计划',
    '🧠 知识点图谱',
    '📊 学习统计',
    '👨‍👩‍👧 家长端',
    '⚙️ 设置',
])

st.sidebar.divider()
ocr_status = "✅ 就绪" if ocr.is_available() else f"❌ {ocr._engine_error or '加载中'}"
st.sidebar.markdown(f"**OCR**: {ocr_status}", unsafe_allow_html=True)
llm_info = llm.config_info()
llm_status = "✅ 已启用" if llm_info['enabled'] else "⚠️ 离线"
st.sidebar.markdown(f"**AI 老师**: {llm_status} ({llm_info['provider']})", unsafe_allow_html=True)

st.sidebar.divider()
st.sidebar.caption(f"v0.1 · {datetime.now().strftime('%Y-%m-%d %H:%M')}")


# ============== 页面 1: 拍照录入 ==============
if page == '📷 拍照录入':
    st.title('📷 拍照录入错题')
    st.caption('用手机拍题目, 上传后自动 OCR 识别, 孩子再补全/修改. 不联网传图, 纯本地.')

    col1, col2 = st.columns([1, 1])

    with col1:
        st.subheader('1. 上传题目照片')
        src = st.radio('图片来源', ['手机拍照上传', '电脑选图'], horizontal=True)
        if src == '手机拍照上传':
            up = st.file_uploader('选照片', type=['jpg', 'jpeg', 'png', 'webp', 'heic'], key='up_phone')
        else:
            up = st.file_uploader('选图', type=['jpg', 'jpeg', 'png', 'webp'], key='up_pc')

        if up:
            img = Image.open(up)
            st.image(img, caption='预览', use_container_width=True)

            with st.spinner('OCR 识别中...'):
                rel_path = save_uploaded_image(up)
                abs_path = relative_to_abs(rel_path)
                ocr_res = ocr.ocr_image(abs_path)
                st.session_state['ocr_res'] = ocr_res
                st.session_state['img_rel'] = rel_path

    with col2:
        st.subheader('2. 核对/编辑识别结果')
        ocr_res = st.session_state.get('ocr_res')
        if ocr_res:
            if not ocr_res['success']:
                st.error(f"OCR 失败: {ocr_res.get('error', '未知')}")
                st.info('请手动输入题目.')
                default_text = ''
            else:
                st.success(f"OCR 成功! 平均置信度 {ocr_res['avg_confidence']:.1%}, 识别 {len(ocr_res['lines'])} 行")
                default_text = ocr_res['text']
                with st.expander('逐行预览', expanded=False):
                    for i, line in enumerate(ocr_res['lines'], 1):
                        st.write(f"{i}. {line}")
        else:
            default_text = ''

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
                f"#{m['id']} [{m['subject']}] {m.get('title') or m['question'][:40]} "
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
                    st.caption(f"录入: {m['created_at']} | 来源: {m.get('source') or '手动'}")

                with tabs[1]:
                    if m.get('image_path'):
                        try:
                            st.image(relative_to_abs(m['image_path']), use_container_width=True)
                        except Exception as e:
                            st.error(f'图片加载失败: {e}')
                    else:
                        st.info('无图片')

                with tabs[2]:
                    st.caption(f"当前 AI 状态: {llm.config_info()['provider']}")
                    if st.button(f'🤖 AI 讲题 #{m["id"]}', key=f'ai_{m["id"]}'):
                        with st.spinner('生成中...'):
                            res = llm.solve(m['question'], m['subject'])
                            if res.get('answer'):
                                st.success(f"**答案**: {res['answer']}")
                            st.markdown('**思路**')
                            st.write(res.get('explanation', '暂无'))
                            st.caption(f"来源: {res.get('source')}")

                with tabs[3]:
                    if st.button(f'🌐 联网搜题 #{m["id"]}', key=f'search_{m["id"]}'):
                        with st.spinner('搜索中...'):
                            res = web_search.solve_with_search(m['question'], m['subject'])
                            for r in res['results']:
                                st.markdown(f"- [{r['title']}]({r['href']})")
                                if r.get('snippet'):
                                    st.caption(r['snippet'])
                            st.divider()
                            st.markdown('**前 2 条页面摘要**')
                            st.write(res['summary'])

                with tabs[4]:
                    st.write(f"复习次数: {m['review_count']}, 上次: {m.get('last_reviewed') or '未复习'}")
                    rating = st.slider('这次复习掌握程度', 1, 4, 3, key=f'r_{m["id"]}',
                                       help='1=忘了 2=模糊 3=记得 4=熟练')
                    note = st.text_input('备注', key=f'n_{m["id"]}')
                    if st.button(f'✅ 标记已复习 #{m["id"]}', key=f'rv_{m["id"]}'):
                        db.mark_reviewed(m['id'], rating, note)
                        st.success('已记录')
                        time.sleep(0.5)
                        st.rerun()

                with tabs[5]:
                    new_reason = st.selectbox('改错因', WRONG_REASONS,
                                              index=WRONG_REASONS.index(m['wrong_reason']) if m.get('wrong_reason') in WRONG_REASONS else 0,
                                              key=f'reason_{m["id"]}')
                    new_corr = st.text_input('改正确答案', value=m.get('correct_answer') or '', key=f'corr_{m["id"]}')
                    new_exp = st.text_area('补充思路', value=m.get('explanation') or '', key=f'exp_{m["id"]}')
                    if st.button(f'💾 保存修改 #{m["id"]}', key=f'sv_{m["id"]}'):
                        db.update_mistake(m['id'],
                                          wrong_reason=new_reason,
                                          correct_answer=new_corr or None,
                                          explanation=new_exp or None)
                        st.success('已更新')
                        time.sleep(0.5)
                        st.rerun()
                    if st.button(f'🗑️ 删除 #{m["id"]}', key=f'del_{m["id"]}', type='secondary'):
                        db.delete_mistake(m['id'])
                        st.success('已删除')
                        time.sleep(0.5)
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
                    st.caption(f'超期 {overdue} 天')
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
        c1, c2, c3 = st.columns(3)
        c1.metric('总错题', s['total'])
        c2.metric('已掌握', s['mastered'])
        c3.metric('未掌握', s['unmastered'])

        st.divider()
        st.subheader('最近录入的 20 条错题')
        items = db.list_mistakes(limit=20)
        for m in items:
            st.markdown(
                f"#{m['id']} [{m['subject']}] {m.get('title') or m['question'][:30]} "
                f"— {m.get('wrong_reason') or '-'} (复习{m['review_count']}次) "
                f"{'✅' if m['mastered'] else ''}"
            )

        st.divider()
        st.subheader('数据导出')
        if st.button('📥 导出全部错题为 JSON'):
            all_items = db.list_mistakes(limit=10000)
            import json as _json
            payload = _json.dumps(all_items, ensure_ascii=False, indent=2)
            st.download_button(
                label='下载',
                data=payload,
                file_name=f"study_export_{datetime.now().strftime('%Y%m%d_%H%M')}.json",
                mime='application/json',
            )

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
