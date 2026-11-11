"""
笔记工坊（notes.py）：上传课堂 PDF → 自动生成纪要 → 存 GitHub 仓库 → 平台内查看。

分层的职责（别混）：
- notes_forge.py  纯管线：PDF→逐页识别→结构化→断言。不碰网络存储，产物在返回值里。
- notes.py        存储 + 编排：产物推 jovi0112/notes-data 仓库存档；查看端从仓库读。
- app.py          界面：表单、进度条、渲染入口。

存储选型（2026-11-11 实测后定的，别凭印象换）：
- 产物存独立仓库 notes-data（不是 study-platform 本仓库）——
  Streamlit Cloud 检测到 push 会自动重新部署，往本仓库推产物 = 每生成一讲整站重启一次。
- 读走 cdn.jsdelivr.net（国内可达、免 API 配额），失败自动落 api.github.com（实测最稳的 GitHub 域名）。
- 推送用 STUDY_BACKUP_TOKEN（实测 scopes = gist, repo, workflow，有 repo 权限就能推仓库），
  Git Data API 走 blob→tree→commit→ref，和 _push_via_api.py 同一条已验证的路。
- token / LLM / 视觉三种配置全部复用 Cloud Secrets 已有键，部署侧零新增配置。

铁律（沿用全平台约定）：
- 失败给人话 + 下一步，不静默返回空。
- 生成结果先过结构断言（notes_forge.validate）才入库；断言不过就明说，不给"看起来成功"的假象。
"""
from __future__ import annotations

import json
import re
import time
from datetime import date as _date
from pathlib import Path

MODULE_VER = 1          # 改接口必须 bump，app.py 里 _NOTES_WANT 同步改（模块守卫铁律）
REPO = 'jovi0112/notes-data'
BRANCH = 'main'
CDN = f'https://cdn.jsdelivr.net/gh/{REPO}@{BRANCH}'
API = 'https://api.github.com'

SUBJECT_LABEL = {'math': '数学', 'chinese': '语文', 'english': '英语'}


# ============================================================
# 就绪自检
# ============================================================

def ready() -> tuple[bool, str]:
    """三件事都齐才能开工：pymupdf 已装、看图 key、文本 key。缺什么说什么。"""
    try:
        import pymupdf  # noqa: F401
    except Exception:
        return False, '缺少 PDF 渲染依赖 pymupdf —— 等 Streamlit Cloud 把 requirements.txt 装完（构建要几分钟），刷新即可'
    c = _forge().cfg_status()
    if not c['vision_ready']:
        return False, '看图模型未配置（缺 STUDY_VISION_API_KEY）——在 Cloud 的 Settings → Secrets 里补'
    if not c['text_ready']:
        return False, '文本模型未配置（缺 STUDY_LLM_API_KEY）——在 Cloud 的 Settings → Secrets 里补'
    return True, ''


def _forge():
    import notes_forge
    return notes_forge


def _token() -> str:
    from llm import _read_secret
    tok = _read_secret('STUDY_BACKUP_TOKEN')
    if not tok:
        # 本地兜底：st.secrets 偶发解析失败时（实测出现过一次，表现为 401 Bad
        # credentials，因为拿空 token 去请求），直接读 secrets.toml 再试一次
        try:
            import tomllib
            p = Path(__file__).resolve().parent / '.streamlit' / 'secrets.toml'
            if p.is_file():
                with open(p, 'rb') as f:
                    tok = str(tomllib.load(f).get('STUDY_BACKUP_TOKEN') or '').strip()
        except Exception:  # noqa: BLE001
            pass
    return tok


def token_status() -> dict:
    """存储侧自检：token 在不在、仓库通不通。给界面一行人话。"""
    tok = _token()
    if not tok:
        return {'ok': False, 'reason': '缺 STUDY_BACKUP_TOKEN（Cloud Secrets 里有备份令牌就能复用）'}
    try:
        r = _gh('GET', f'/repos/{REPO}')
        if r.status_code == 200:
            return {'ok': True, 'reason': ''}
        if r.status_code == 404:
            return {'ok': True, 'reason': 'repo_not_created'}   # 首次生成时自动建
        return {'ok': False, 'reason': f'GitHub 返回 {r.status_code}'}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'reason': f'连不上 GitHub：{e}'}


# ============================================================
# GitHub 存取（Git Data API）
# ============================================================

def _gh(method: str, path: str, payload: dict | None = None,
        raw: bytes | None = None, timeout: int = 60):
    import time as _t
    import requests
    headers = {
        'Authorization': f'Bearer {_token()}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'study-platform-notes',
    }
    url = path if path.startswith('http') else API + path
    if raw is not None:
        headers['Content-Type'] = 'application/octet-stream'
        return requests.request(method, url, data=raw, headers=headers, timeout=timeout)
    # GET 幂等，可以安全重试（GitHub 线路抖动实测不少见）；401 也重试一次——
    # 那可能是 st.secrets 偶发抽风给了空 token，_token() 的第二次读取有机会救回来
    if method == 'GET':
        last = None
        for w in (0, 2, 6):
            if w:
                _t.sleep(w)
            try:
                r = requests.request(method, url, json=payload, headers=headers,
                                     timeout=timeout)
            except requests.RequestException:
                last = None
                continue
            if r.status_code < 500 and r.status_code != 401:
                return r
            last = r
            if r.status_code == 401:
                headers['Authorization'] = f'Bearer {_token()}'   # 重新取一次 token
        return last
    return requests.request(method, url, json=payload, headers=headers, timeout=timeout)


def _ensure_repo() -> None:
    """仓库不存在就建（public：jsdelivr 只对 public 仓库出 CDN）。404 之外的错误照常抛。"""
    r = _gh('GET', f'/repos/{REPO}')
    if r.status_code == 200:
        return
    if r.status_code != 404:
        raise RuntimeError(f'检查 notes-data 仓库失败：GitHub 返回 {r.status_code}（{r.text[:120]}）')
    r2 = _gh('POST', '/user/repos', payload={
        'name': REPO.split('/')[1], 'private': False,
        'description': '果果的课堂笔记存档（由学习平台笔记工坊自动写入）',
        'auto_init': True,
    })
    if r2.status_code not in (201, 422):   # 422 = 已存在（并发），也算成功
        raise RuntimeError(f'创建 {REPO} 失败：{r2.status_code}（{r2.text[:160]}）')


def push_files(files: dict, message: str) -> str:
    """
    把 {path: bytes} 一次 commit 推到 notes-data。返回新 commit sha。
    走 Git Data API（_push_via_api.py 验证过的路）：blob → tree → commit → ref。
    """
    _ensure_repo()
    r = _gh('GET', f'/repos/{REPO}/git/ref/heads/{BRANCH}')
    if r.status_code != 200:
        raise RuntimeError(f'取 {BRANCH} 分支失败：{r.status_code}（{r.text[:120]}）')
    base_sha = r.json()['object']['sha']

    base_tree = _gh('GET', f'/repos/{REPO}/git/commits/{base_sha}').json()['tree']['sha']
    tree_items = []
    for path, content in files.items():
        rb = _gh('POST', f'/repos/{REPO}/git/blobs',
                 payload={'content': content if isinstance(content, str) else
                          __import__('base64').b64encode(content).decode(),
                          'encoding': 'utf-8' if isinstance(content, str) else 'base64'})
        if rb.status_code != 201:
            raise RuntimeError(f'上传 blob 失败（{path}）：{rb.status_code}（{rb.text[:120]}）')
        tree_items.append({'path': path, 'mode': '100644', 'type': 'blob',
                           'sha': rb.json()['sha']})
    rt = _gh('POST', f'/repos/{REPO}/git/trees',
             payload={'base_tree': base_tree, 'tree': tree_items})
    if rt.status_code != 201:
        raise RuntimeError(f'建 tree 失败：{rt.status_code}（{rt.text[:160]}）')
    rc = _gh('POST', f'/repos/{REPO}/git/commits', payload={
        'message': message, 'tree': rt.json()['sha'], 'parents': [base_sha]})
    if rc.status_code != 201:
        raise RuntimeError(f'建 commit 失败：{rc.status_code}（{rc.text[:160]}）')
    new_sha = rc.json()['sha']
    ru = _gh('PATCH', f'/repos/{REPO}/git/refs/heads/{BRANCH}',
             payload={'sha': new_sha, 'force': False})
    if ru.status_code != 200:
        raise RuntimeError(f'更新分支失败：{ru.status_code}（{ru.text[:160]}）')
    return new_sha


def _fetch_bytes(path: str, timeout: int = 30, fresh: bool = False) -> bytes:
    """
    读仓库文件。fresh=True（JSON 类）：GitHub API 优先——jsdelivr 对 @main 的更新
    有缓存滞后（实测刚推完就读，CDN 还给旧内容/404），索引和讲次数据必须新鲜。
    fresh=False（图片类）：CDN 优先——图片按名字不可变，CDN 免配额且国内可达；
    API 兜底。
    """
    import requests
    import base64
    cdn_get = lambda: requests.get(f'{CDN}/{path}', timeout=timeout)  # noqa: E731

    def api_get():
        r = _gh('GET', f'/repos/{REPO}/contents/{path}?ref={BRANCH}', timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(f'API 读取 {path} 失败：{r.status_code}')
        return base64.b64decode(r.json().get('content') or '')

    tries = (api_get, cdn_get) if fresh else (cdn_get, api_get)
    err = None
    for get in tries:
        try:
            data = get()
            if data:
                return data
        except Exception as e:  # noqa: BLE001
            err = e
    raise RuntimeError(f'读取 {path} 失败（CDN 与 GitHub 都不通）：{err}')


# ============================================================
# 产物组装与推送
# ============================================================

def lesson_key_for(subject: str, no: int) -> str:
    no = int(no)
    if subject == 'chinese':
        return f'ch{no:02d}'
    if subject == 'english':
        return f'en{no:02d}'
    return f'lesson{no:02d}'


def gallery_sub_for(subject: str, no: int) -> str:
    no = int(no)
    return {'math': f'L{no}', 'chinese': f'C{no}', 'english': f'E{no}'}[subject]


def build_payload(out: dict, updated: str | None = None) -> dict:
    """把 run() 的返回组装成入库 payload（lesson + board + 目录信息，一体存储）。"""
    ing = out['ingest']
    return {
        **out['lesson'],
        'board': out['board'],
        'pages': ing['count'],
        'updated': updated or _date.today().isoformat(),
    }


def no_int(e) -> int:
    """讲号 → 整数（容忍旧数据 '第8讲' / '8' / 8 的写法）。解析不出返回 99 排最后。"""
    m = re.search(r'(\d+)', str((e if isinstance(e, dict) else {}).get('no') or e or ''))
    return int(m.group(1)) if m else 99


def _merge_index(old_index: list, payload: dict) -> list:
    """index.json 合并：同 id 覆盖、按科目→讲号排序。纯函数，可单测。
    讲号要能容忍旧数据的写法（'第8讲' / '8' / 8 都可能出现）。"""
    entry = {k: payload.get(k) for k in
             ('id', 'no', 'course', 'subject', 'subjectLabel', 'title', 'tagline',
              'date', 'pages', 'updated')}

    def _no_int(e) -> int:
        return no_int(e)

    others = [e for e in (old_index or []) if e.get('id') != payload['id']]
    order = {'math': 0, 'chinese': 1, 'english': 2}
    others.append(entry)
    return sorted(others, key=lambda e: (order.get(e.get('subject'), 9), _no_int(e)))


def push_lesson(out: dict, on_log=None) -> dict:
    """生成结果入库：lesson JSON + 全部板书图 + index.json，一次 commit。"""
    log = on_log or (lambda s: None)
    payload = build_payload(out)
    key = payload['id']
    gallery = payload['board'].get('galleryDir') or f"pages/{key}"
    payload['galleryDir'] = gallery
    files: dict = {}
    files[f'lessons/{key}.json'] = json.dumps(payload, ensure_ascii=False,
                                              indent=1).encode('utf-8')
    for p in out['ingest']['pages']:
        files[f'{gallery}/p{p["no"]:02d}.jpg'] = p['img']
    # index.json：先读旧的（不存在就从空开始），合并后写回
    try:
        old = json.loads(_fetch_bytes('index.json', fresh=True).decode('utf-8'))
    except Exception:  # noqa: BLE001
        old = []
    files['index.json'] = json.dumps(_merge_index(old, payload), ensure_ascii=False,
                                     indent=1).encode('utf-8')
    sha = push_files(files, f"笔记工坊: {key} {payload.get('title', '')}")
    log(f'已存档 {len(files)} 个文件到 {REPO}（commit {sha[:8]}）')
    return {'sha': sha, 'files': len(files), 'gallery': gallery}


# ============================================================
# 查看端
# ============================================================

def _cache(ttl: int):
    """网络读的缓存。st.cache_data 对「抛异常」不缓存 —— 失败下次照常重试，
    所以读函数失败一律抛、不许返回 None 吞掉（吞了会被缓存住，页面卡在旧状态）。"""
    import streamlit as st
    return st.cache_data(ttl=ttl, show_spinner=False)


def list_lessons() -> list:
    """讲次目录。缓存 5 分钟：刚生成完要立刻看到，调用方用 .clear() 清。"""
    try:
        return json.loads(_fetch_bytes('index.json', fresh=True).decode('utf-8'))
    except Exception:  # noqa: BLE001
        return []


def load_lesson(key: str) -> dict:
    """整讲数据。取不到就抛（不缓存失败），由调用方给人话。"""
    return json.loads(_fetch_bytes(f'lessons/{key}.json', fresh=True).decode('utf-8'))


def page_image(gallery: str, no: int) -> bytes:
    """板书原图（约 100~150KB/张），缓存一天；取不到就抛。"""
    return _fetch_bytes(f'{gallery}/p{int(no):02d}.jpg', timeout=12)


list_lessons = _cache(300)(list_lessons)
load_lesson = _cache(3600)(load_lesson)
page_image = _cache(86400)(page_image)


# ============================================================
# 生成编排（界面调这个，不直接调 notes_forge.run）
# ============================================================

def generate(pdf_bytes: bytes, subject: str, no: int, title: str,
             on_log=None, on_progress=None, on_wait=None) -> dict:
    """
    端到端：管线 → 断言 → 入库。返回 {'ok', 'report', 'push'?, 'lesson'?}。
    断言不过（ok=False）也返回，界面把 report.errors 明明白白亮出来。
    """
    log = on_log or (lambda s: None)
    key = lesson_key_for(subject, no)
    course = SUBJECT_LABEL.get(subject, '数学')
    gsub = gallery_sub_for(subject, no)
    out = _forge().run(pdf_bytes, key, title, str(no), course,
                       _date.today().isoformat(), dist_dir=None,
                       workers=6, on_log=log, on_progress=on_progress,
                       subject=subject, gallery_sub=gsub, write_outputs=False)
    if not out['ok']:
        return {'ok': False, 'report': out['report'], 'key': key}
    log('⑨ 存档到 GitHub 仓库…')
    push = push_lesson(out, on_log=log)
    return {'ok': True, 'report': out['report'], 'push': push,
            'key': key, 'lesson': out['lesson'], 'board': out['board'],
            'gallery': push['gallery'], 'seconds': out['report'].get('seconds')}


# ============================================================
# 渲染助手（app.py 调用；st 相关的都收在这里，页面代码保持薄）
# ============================================================

def render_lesson(L: dict, board: dict | None = None) -> None:
    """把一讲渲染成页面。board 优先用 L['board']（新格式一体存储）。"""
    import streamlit as st
    board = board or L.get('board') or {}
    subj = SUBJECT_LABEL.get(L.get('subject'), L.get('course', ''))

    st.markdown(
        f"#### {L.get('course', '')} · 第 {L.get('no')} 讲　**{L.get('title')}**",
        unsafe_allow_html=True)
    st.caption(f"{subj} ｜ {L.get('date', '')} ｜ {L.get('pages', '?')} 页板书 ｜ 生成于 {L.get('updated', '?')}")

    minute = L.get('minute') or {}
    if minute.get('headline'):
        st.info(f"**一分钟总结**：{minute['headline']}")
        for pt in minute.get('points') or []:
            st.markdown(pt, unsafe_allow_html=True)

    mm = L.get('mindmap') or {}
    if mm.get('branches'):
        st.markdown('**🧭 本讲导图**')
        for br in mm['branches']:
            st.markdown(f"- **{br.get('t', '')}**" +
                        ('　' + ' · '.join(br.get('sub') or []) if br.get('sub') else ''),
                        unsafe_allow_html=True)

    board_of = (board.get('sections') or {})
    for sec in L.get('sections') or []:
        with st.container(border=True):
            st.markdown(f"##### {sec.get('title', '')}　<span style='font-size:0.75em;color:gray'>{sec.get('level', '')}</span>",
                        unsafe_allow_html=True)
            _render_blocks(st, sec.get('blocks') or [])
            pgs = board_of.get(sec.get('id')) or []
            if pgs:
                with st.expander(f"🖼️ 本模块板书原页（{len(pgs)} 页）"):
                    _show_pages(st, L.get('galleryDir', ''), pgs)

    exs = L.get('examples') or []
    if exs:
        st.markdown('---')
        st.markdown('##### ✏️ 例题精讲')
        ex_pages = (board.get('examples') or {})
        for e in exs:
            with st.expander(f"{e.get('no')} ｜ {e.get('title', '')}"):
                st.markdown(f"**题**：{e.get('problem', '')}")
                for i, s in enumerate(e.get('steps') or [], 1):
                    st.markdown(f"{i}. {s}")
                st.success(f"**答**：{e.get('answer', '')}")
                if e.get('method'):
                    st.caption(f"思路：{e['method']}")
                if e.get('trap'):
                    st.warning(f"易错：{e['trap']}")
                pgs = ex_pages.get(e.get('no')) or []
                if pgs:
                    with st.expander(f"🖼️ 板书原页（{len(pgs)}）"):
                        _show_pages(st, L.get('galleryDir', ''), pgs)

    for pit in L.get('pitfalls') or []:
        st.error(f"**雷区 · {pit.get('title', '')}**：{pit.get('body', '')}")

    qz = L.get('quiz') or []
    if qz:
        st.markdown('---')
        st.markdown('##### ✅ 自测一下')
        for i, q in enumerate(qz):
            opts = q.get('options') or []
            if q.get('type') == 'choice' and opts:
                st.markdown(f"**{i+1}. {q.get('q', '')}**")
                pick = st.radio('你的选择', opts, index=None,
                                key=f"ntq_{L.get('id')}_{q.get('id', i)}",
                                label_visibility='collapsed')
                if pick is not None:
                    if opts.index(pick) == q.get('answer'):
                        st.success('答对了！')
                    else:
                        st.error(f'正确答案是 {chr(65 + q.get("answer", 0))}。')
                with st.expander('看讲解'):
                    st.markdown(q.get('explain', ''))
            else:
                st.markdown(f"**{i+1}. {q.get('q', '')}**")
                with st.expander('看答案'):
                    st.markdown(q.get('explain') or q.get('a') or '')


def _render_blocks(st, blocks: list) -> None:
    for b in blocks:
        t, v = b.get('t'), b.get('v')
        if t == 'p':
            st.markdown(v, unsafe_allow_html=True)
        elif t == 'list':
            items = v if isinstance(v, list) else [v]
            st.markdown('\n'.join(f'- {x}' for x in items), unsafe_allow_html=True)
        elif t == 'formula':
            st.markdown(f"`{v}`" if len(str(v)) < 60 else v, unsafe_allow_html=True)
        elif t == 'table':
            head = b.get('head') or []
            rows = b.get('rows') or []
            lines = ['| ' + ' | '.join(str(h) for h in head) + ' |',
                     '|' + '---|' * max(len(head), 1)]
            lines += ['| ' + ' | '.join(str(c) for c in r) + ' |' for r in rows]
            st.markdown('\n'.join(lines), unsafe_allow_html=True)
        elif t == 'tip':
            st.info(v)
        elif t == 'trap':
            st.warning(v)
        elif t == 'board':
            st.markdown(v, unsafe_allow_html=True)


def _show_pages(st, gallery: str, nos: list) -> None:
    # 千万别在 expander 里直接拉图：expander 折叠时内容照样执行，一讲 40+ 张图
    # 全量预取（个别 CDN 未命中一次卡 30 秒）→ 整页像死机（AppTest 300 秒超时实测）。
    # 所以改成"点按钮才加载"。
    key = f"nt_imgs_{gallery}_{nos[0]}_{len(nos)}"
    if st.button(f'📥 加载这 {len(nos)} 页板书原图', key=key, use_container_width=True):
        ok_n = 0
        for n in nos:
            try:
                st.image(page_image(gallery, n), caption=f'第 {n} 页',
                         use_container_width=True)
                ok_n += 1
            except Exception:  # noqa: BLE001
                st.caption(f'第 {n} 页图片暂时取不到（网络抖动，重新点一次按钮）')
        if ok_n == 0:
            st.caption('一张都没取到 —— 检查网络，或稍后再试')
    else:
        st.caption(f'点上面按钮才加载（共 {len(nos)} 页，约 {len(nos) * 130 // 1024 + 1} MB）')
