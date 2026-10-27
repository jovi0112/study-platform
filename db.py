"""
数据库层: SQLite, 单文件, 三个核心表 + 一个扩展表
- mistakes   : 错题
- knowledge  : 知识点
- reviews    : 复习记录
- settings   : 家长端配置/单点登录 token
"""
import sqlite3
import os
from datetime import datetime, timedelta
from contextlib import contextmanager

# STUDY_DB 让自动化测试/临时实例能指到别的库文件, 不去碰孩子的真实数据
DB_PATH = os.environ.get('STUDY_DB') or os.path.join(os.path.dirname(__file__), 'data', 'study.db')

SCHEMA = """
CREATE TABLE IF NOT EXISTS mistakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,          -- 学科: 语文/数学/英语/物理/化学/...
    grade TEXT,                     -- 年级: 初一/初二/...
    title TEXT,                     -- 错题简述(手填)
    question TEXT,                  -- 题面(OCR 或手填)
    answer TEXT,                    -- 孩子写的答案
    correct_answer TEXT,            -- 正确答案(联网/AI)
    explanation TEXT,               -- 解答/思路
    wrong_reason TEXT,              -- 错因标签: 概念不清/计算失误/审题错误/...
    knowledge_id INTEGER,           -- 关联知识点
    image_path TEXT,                -- 拍照本地路径
    source TEXT,                    -- 录入方式: photo/typing
    created_at TEXT NOT NULL,
    last_reviewed TEXT,
    review_count INTEGER DEFAULT 0,
    mastered INTEGER DEFAULT 0,     -- 是否掌握
    FOREIGN KEY(knowledge_id) REFERENCES knowledge(id)
);

CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    name TEXT NOT NULL,             -- 知识点名: 一元二次方程求根公式
    chapter TEXT,                   -- 章节
    difficulty INTEGER DEFAULT 1,   -- 1~5
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mistake_id INTEGER NOT NULL,
    reviewed_at TEXT NOT NULL,
    rating INTEGER,                 -- 1=忘了 2=模糊 3=记得 4=熟练
    note TEXT,
    FOREIGN KEY(mistake_id) REFERENCES mistakes(id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS wishes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,           -- 愿望内容: 觉得哪里不好用/想要什么功能
    category TEXT DEFAULT '想法',    -- 不好用/想要功能/想法
    status TEXT DEFAULT '新',        -- 新/尝试中/已解决/放弃
    note TEXT,                       -- 处理备注(她自己做实验的记录)
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS custom_prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,              -- 模板名: "举个例子版讲解"
    prompt_text TEXT NOT NULL,       -- 模板内容, 可含 {题目} 占位符
    use_count INTEGER DEFAULT 0,     -- 被用过几次
    last_used TEXT,                  -- 最近一次使用
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mistakes_subject ON mistakes(subject);
CREATE INDEX IF NOT EXISTS idx_mistakes_knowledge ON mistakes(knowledge_id);
CREATE INDEX IF NOT EXISTS idx_reviews_mistake ON reviews(mistake_id);
CREATE INDEX IF NOT EXISTS idx_wishes_status ON wishes(status);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """初始化数据库(幂等)"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # 兼容旧库: 字段不存在则追加. SQLite 没有 IF NOT EXISTS, 用 PRAGMA 兜底.
        _ensure_column(conn, 'mistakes', 'good_prompts', 'TEXT')
        # 讲解思维导图: 存图解结构(JSON), 渲染很快, 所以存 spec 不存 SVG
        _ensure_column(conn, 'mistakes', 'diagram_json', 'TEXT')
        # 从每日测试进来的错题要能"在错题本里重做", 所以题型/选项/评分点必须一起带过来
        _ensure_column(conn, 'mistakes', 'qtype', 'TEXT')
        _ensure_column(conn, 'mistakes', 'options_json', 'TEXT')
        _ensure_column(conn, 'mistakes', 'points_json', 'TEXT')   # 主观题评分点 [{no,text,w}]
        _ensure_column(conn, 'mistakes', 'passage', 'TEXT')       # 阅读题原文(没课本也做得了)
        _ensure_column(conn, 'mistakes', 'miss_reason', 'TEXT')   # 未做原因: timeout / blank / ''
        _ensure_column(conn, 'mistakes', 'uid', 'TEXT')
        # 后补的列只能在 ALTER 之后建索引 —— 写进 SCHEMA 会在老库上直接把 init_db 打崩
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mistakes_miss ON mistakes(miss_reason)")
        conn.executescript("""
CREATE TABLE IF NOT EXISTS mistake_redo (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mistake_id INTEGER NOT NULL,
    answer TEXT,                    -- 这次重做写的答案
    correct INTEGER,                -- 1 对 / 0 错
    coverage REAL,                  -- 主观题要点覆盖率(0~1)
    comment TEXT,
    graded_by TEXT,                 -- exact / ai / parent
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_redo_mistake ON mistake_redo(mistake_id);
""")


def _ensure_column(conn, table: str, column: str, definition: str):
    """如果列不存在则 ALTER TABLE 追加"""
    cur = conn.execute(f"PRAGMA table_info({table})")
    cols = {r['name'] for r in cur.fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


# ============== 错题 CRUD ==============
def add_mistake(subject, question, **kwargs):
    fields = ['subject', 'question']
    placeholders = ['?', '?']
    values = [subject, question]
    for k, v in kwargs.items():
        if v is not None and k in ('grade', 'title', 'answer', 'correct_answer',
                                     'explanation', 'wrong_reason',
                                     'knowledge_id', 'image_path', 'source',
                                     'diagram_json', 'qtype', 'options_json',
                                     'points_json', 'passage', 'miss_reason', 'uid'):
            fields.append(k)
            placeholders.append('?')
            values.append(v)
    fields.append('created_at')
    placeholders.append('?')
    values.append(datetime.now().isoformat(timespec='seconds'))
    sql = f"INSERT INTO mistakes ({','.join(fields)}) VALUES ({','.join(placeholders)})"
    with get_conn() as conn:
        cur = conn.execute(sql, values)
        return cur.lastrowid


def _mistake_where(subject=None, mastered=None, miss_reason=None):
    """
    拼 WHERE 条件 + 参数. 过滤必须在 SQL 层做完 ——
    以前是"先取最近 N 条、再在内存里筛", 结果 N 条之外的题根本进不了筛选,
    明明有「没做完」的题却显示为空。
    """
    sql, params = "1=1", []
    if subject and subject != '全部':
        sql += " AND subject = ?"
        params.append(subject)
    if mastered is not None:
        sql += " AND mastered = ?"
        params.append(int(mastered))
    if miss_reason:
        if isinstance(miss_reason, (list, tuple)):
            sql += " AND miss_reason IN (%s)" % ','.join('?' * len(miss_reason))
            params += list(miss_reason)
        else:
            sql += " AND miss_reason = ?"
            params.append(miss_reason)
    return sql, params


def count_mistakes(subject=None, mastered=None, miss_reason=None) -> int:
    """同条件下的总条数 —— 分页要显示"共多少条"必须靠它, 不能用 len(当页)"""
    where, params = _mistake_where(subject, mastered, miss_reason)
    with get_conn() as conn:
        return conn.execute(
            f"SELECT COUNT(*) AS c FROM mistakes WHERE {where}", params).fetchone()['c']


def list_mistakes(subject=None, mastered=None, limit=200, offset=0, miss_reason=None):
    where, params = _mistake_where(subject, mastered, miss_reason)
    sql = f"SELECT * FROM mistakes WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_mistake(mid):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM mistakes WHERE id=?", (mid,)).fetchone()
        return dict(r) if r else None


def mistake_by_uid(uid):
    """
    按 uid 找已经进过错题本的同一道题。
    判卷后会自动推错题; 别处再推一次(或网络重试)就会多出一模一样的一条,
    错题本越积越臃肿 —— 推之前先拿 uid 查一下。
    """
    if not uid:
        return None
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM mistakes WHERE uid=? LIMIT 1", (uid,)).fetchone()
        return dict(r) if r else None


_MISTAKE_COLS = ('grade', 'title', 'answer', 'correct_answer', 'explanation', 'wrong_reason',
                 'knowledge_id', 'image_path', 'source', 'diagram_json', 'qtype', 'options_json',
                 'points_json', 'passage', 'miss_reason', 'uid', 'question', 'subject',
                 'last_reviewed', 'review_count', 'mastered')


def update_mistake(mid, **kwargs):
    if not kwargs:
        return
    sets, params = [], []
    for k, v in kwargs.items():
        # 只放行真实存在的列 —— 打错字段名会直接把 SQL 拼坏, 静默吞掉更新
        if k not in _MISTAKE_COLS:
            continue
        sets.append(f"{k}=?")
        params.append(v)
    if not sets:
        return
    params.append(mid)
    with get_conn() as conn:
        conn.execute(f"UPDATE mistakes SET {','.join(sets)} WHERE id=?", params)


def delete_mistake(mid):
    with get_conn() as conn:
        conn.execute("DELETE FROM mistakes WHERE id=?", (mid,))


# ============== 好提示词 (孩子点过的追问模板) ==============
def append_good_prompt(mid, text: str):
    """
    把孩子"收藏的好提示词"追加到错题. 自动去重, 含时间戳.
    存储格式: 每行 "[YYYY-MM-DD HH:MM] <text>"
    """
    text = (text or '').strip()
    if not text:
        return False
    with get_conn() as conn:
        row = conn.execute("SELECT good_prompts FROM mistakes WHERE id=?", (mid,)).fetchone()
        if not row:
            return False
        existing = row['good_prompts'] or ''
        # 去重: 完全相同就不重复加
        if text in existing:
            return False
        ts = datetime.now().strftime('%Y-%m-%d %H:%M')
        line = f"[{ts}] {text}"
        new = (existing + "\n" + line) if existing else line
        conn.execute("UPDATE mistakes SET good_prompts=? WHERE id=?", (new, mid))
        return True


def get_good_prompts(mid) -> list:
    """返回错题的好提示词列表(去掉空行, 保持原顺序)"""
    with get_conn() as conn:
        row = conn.execute("SELECT good_prompts FROM mistakes WHERE id=?", (mid,)).fetchone()
        if not row or not row['good_prompts']:
            return []
        return [ln for ln in row['good_prompts'].split('\n') if ln.strip()]


def delete_good_prompt(mid, text: str):
    """删除一条好提示词(按整行精确匹配)"""
    if not text:
        return
    with get_conn() as conn:
        row = conn.execute("SELECT good_prompts FROM mistakes WHERE id=?", (mid,)).fetchone()
        if not row or not row['good_prompts']:
            return
        lines = [ln for ln in row['good_prompts'].split('\n') if ln.strip() and ln.strip() != text.strip()]
        conn.execute("UPDATE mistakes SET good_prompts=? WHERE id=?",
                     ('\n'.join(lines), mid))


def add_redo(mid, answer, correct, comment='', coverage=None, graded_by=''):
    """记一次"在错题本里重做"的结果, 返回 redo id"""
    now = datetime.now().isoformat(timespec='seconds')
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO mistake_redo(mistake_id, answer, correct, coverage, comment, graded_by, at) "
            "VALUES(?,?,?,?,?,?,?)",
            (mid, str(answer or ''), 1 if correct else 0, coverage, comment, graded_by, now))
        n = conn.execute("SELECT COUNT(*) c FROM mistake_redo WHERE mistake_id=?", (mid,)).fetchone()['c']
        # 重做对了就归到已掌握; 错了继续留在错题本, 复习次数也记上
        conn.execute("UPDATE mistakes SET last_reviewed=?, review_count=review_count+1, "
                     "mastered=CASE WHEN ?=1 THEN 1 ELSE 0 END WHERE id=?",
                     (now, 1 if correct else 0, mid))
        return cur.lastrowid, n


def list_redos(mid, limit=20):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM mistake_redo WHERE mistake_id=? "
                            "ORDER BY id DESC LIMIT ?", (mid, limit)).fetchall()
    return [dict(r) for r in rows]


def mark_reviewed(mid, rating, note=None):
    """记录一次复习"""
    now = datetime.now().isoformat(timespec='seconds')
    with get_conn() as conn:
        conn.execute("INSERT INTO reviews (mistake_id, reviewed_at, rating, note) VALUES (?,?,?,?)",
                     (mid, now, rating, note))
        conn.execute("UPDATE mistakes SET last_reviewed=?, review_count=review_count+1, "
                     "mastered=CASE WHEN ?>=3 THEN 1 ELSE mastered END WHERE id=?",
                     (now, rating, mid))


# ============== 知识点 ==============
def add_knowledge(subject, name, chapter=None, difficulty=1):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO knowledge (subject, name, chapter, difficulty, created_at) VALUES (?,?,?,?,?)",
            (subject, name, chapter, difficulty, datetime.now().isoformat(timespec='seconds')))
        return cur.lastrowid


def find_knowledge(subject, name):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM knowledge WHERE subject=? AND name=?",
                         (subject, name)).fetchone()
        return dict(r) if r else None


def list_knowledge(subject=None):
    sql = "SELECT k.*, (SELECT COUNT(*) FROM mistakes m WHERE m.knowledge_id=k.id) AS mistake_count FROM knowledge k"
    params = []
    if subject and subject != '全部':
        sql += " WHERE k.subject = ?"
        params.append(subject)
    sql += " ORDER BY mistake_count DESC, k.name"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ============== 复习曲线 (艾宾浩斯) ==============
def get_review_plan(limit=20):
    """生成待复习列表: 复习间隔 1/2/4/7/15/30 天"""
    intervals = [1, 2, 4, 7, 15, 30]
    now = datetime.now()
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM mistakes WHERE mastered=0").fetchall()
    plan = []
    for r in rows:
        d = dict(r)
        created = datetime.fromisoformat(d['created_at'])
        last = datetime.fromisoformat(d['last_reviewed']) if d['last_reviewed'] else created
        # 第几次复习
        k = d['review_count']
        if k >= len(intervals):
            target_day = intervals[-1]
        else:
            target_day = intervals[k]
        target = last + timedelta(days=target_day)
        if now >= target:
            d['due_days'] = (now - target).days
            d['interval'] = target_day
            plan.append(d)
    plan.sort(key=lambda x: -x['due_days'])
    return plan[:limit]


# ============== 统计 ==============
def stats_overview():
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM mistakes").fetchone()['c']
        mastered = conn.execute("SELECT COUNT(*) AS c FROM mistakes WHERE mastered=1").fetchone()['c']
        by_subj = conn.execute(
            "SELECT subject, COUNT(*) AS c FROM mistakes GROUP BY subject ORDER BY c DESC"
        ).fetchall()
        recent = conn.execute(
            "SELECT DATE(created_at) AS d, COUNT(*) AS c FROM mistakes "
            "WHERE created_at >= DATE('now','-30 day') GROUP BY d ORDER BY d"
        ).fetchall()
    return {
        'total': total,
        'mastered': mastered,
        'unmastered': total - mastered,
        'by_subject': [dict(r) for r in by_subj],
        'recent_30d': [dict(r) for r in recent],
        'good_prompts_count': _count_good_prompts(),
    }


def _count_good_prompts() -> int:
    """统计全库好提示词总数(按行算, 每行一条)"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT good_prompts FROM mistakes WHERE good_prompts IS NOT NULL AND good_prompts != ''"
        ).fetchall()
    n = 0
    for r in row:
        n += sum(1 for ln in (r['good_prompts'] or '').split('\n') if ln.strip())
    return n


def list_recent_good_prompts(limit: int = 20) -> list:
    """返回最近收藏的好提示词(跨错题), [(mid, line), ...]"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, good_prompts FROM mistakes "
            "WHERE good_prompts IS NOT NULL AND good_prompts != '' "
            "ORDER BY id DESC"
        ).fetchall()
    out = []
    for r in rows:
        for ln in (r['good_prompts'] or '').split('\n'):
            ln = ln.strip()
            if ln:
                out.append((r['id'], ln))
    # 倒序: 后加入的在前
    out.reverse()
    return out[:limit]


# ============== 备份 / 恢复 ==============
# 原因: Streamlit Community Cloud 的文件系统是临时存储, 应用重启/重新部署会清空本地库
# 且不保留任何快照。所以"能导出 + 能导回来"是这套系统的保命绳。
_BACKUP_TABLES = ('mistakes', 'reviews', 'knowledge', 'wishes', 'custom_prompts', 'settings')


def export_all() -> dict:
    """整库导出(错题 + 复习记录 + 知识点 + 愿望 + 提示词模板 + 设置)"""
    out = {'version': 1, 'exported_at': datetime.now().isoformat(), 'tables': {}}
    with get_conn() as conn:
        for t in _BACKUP_TABLES:
            out['tables'][t] = [dict(r) for r in conn.execute(f'SELECT * FROM {t}').fetchall()]
    return out


def count_all() -> dict:
    """各表条数, 给家长端"当前库有多少东西"用"""
    with get_conn() as conn:
        return {t: conn.execute(f'SELECT COUNT(*) AS c FROM {t}').fetchone()['c']
                for t in _BACKUP_TABLES}


def import_all(payload: dict, merge: bool = True) -> dict:
    """
    从备份恢复.
    merge=True : 跳过已存在的编号, 不覆盖现有内容(推荐, 用来找回丢失的题)
    merge=False: 先清空各表再全量导入(用来整体还原)
    返回 {'ok': bool, 'imported': {表: 条数}, 'skipped': {表: 条数}, 'error': str|None}
    """
    if not isinstance(payload, dict) or 'tables' not in payload:
        return {'ok': False, 'imported': {}, 'skipped': {}, 'error': '文件格式不对(里面没有 tables)'}
    res = {'ok': True, 'imported': {}, 'skipped': {}, 'error': None}
    with get_conn() as conn:
        for t, rows in payload.get('tables', {}).items():
            if t not in _BACKUP_TABLES or not rows:
                continue
            if not merge:
                conn.execute(f'DELETE FROM {t}')
            imported = skipped = 0
            for row in rows:
                cols = ','.join(row.keys())
                ph = ','.join(['?'] * len(row))
                try:
                    sql = (f'INSERT OR IGNORE INTO {t} ({cols}) VALUES ({ph})' if merge
                           else f'INSERT INTO {t} ({cols}) VALUES ({ph})')
                    cur = conn.execute(sql, list(row.values()))
                    if cur.rowcount and cur.rowcount > 0:
                        imported += 1
                    else:
                        skipped += 1
                except Exception:
                    skipped += 1
            if imported or skipped:
                res['imported'][t] = imported
                res['skipped'][t] = skipped
    return res


# ============== 家长端 ==============
def get_setting(key, default=None):
    with get_conn() as conn:
        r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r['value'] if r else default


def set_setting(key, value):
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))


def ensure_parent_pin():
    """首次使用: 生成 4 位 PIN, 返回"""
    import secrets
    pin = get_setting('parent_pin')
    if not pin:
        pin = ''.join(str(secrets.randbelow(10)) for _ in range(4))
        set_setting('parent_pin', pin)
        set_setting('parent_pin_initialized', datetime.now().isoformat(timespec='seconds'))
    return pin


# ============== 许愿池 (孩子提需求 / 记录不足) ==============
def add_wish(content: str, category: str = '想法') -> int:
    content = (content or '').strip()
    if not content:
        return None
    now = datetime.now().isoformat(timespec='seconds')
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO wishes (content, category, status, created_at) VALUES (?,?,?,?)",
            (content, category or '想法', '新', now))
        return cur.lastrowid


def list_wishes(status=None, limit=100):
    sql = "SELECT * FROM wishes"
    params = []
    if status and status != '全部':
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def update_wish(wid, status=None, note=None):
    now = datetime.now().isoformat(timespec='seconds')
    with get_conn() as conn:
        if status is not None and note is not None:
            conn.execute("UPDATE wishes SET status=?, note=?, updated_at=? WHERE id=?",
                         (status, note, now, wid))
        elif status is not None:
            conn.execute("UPDATE wishes SET status=?, updated_at=? WHERE id=?", (status, now, wid))
        elif note is not None:
            conn.execute("UPDATE wishes SET note=?, updated_at=? WHERE id=?", (note, now, wid))


def delete_wish(wid):
    with get_conn() as conn:
        conn.execute("DELETE FROM wishes WHERE id=?", (wid,))


# ============== 提示词工坊 (孩子自造追问模板) ==============
def add_custom_prompt(name: str, prompt_text: str) -> int:
    name = (name or '').strip()
    prompt_text = (prompt_text or '').strip()
    if not name or not prompt_text:
        return None
    now = datetime.now().isoformat(timespec='seconds')
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO custom_prompts (name, prompt_text, use_count, created_at) VALUES (?,?,0,?)",
            (name, prompt_text, now))
        return cur.lastrowid


def list_custom_prompts():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM custom_prompts ORDER BY use_count DESC, id DESC").fetchall()]


def get_custom_prompt(pid):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM custom_prompts WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None


def bump_prompt_use(pid):
    """使用次数 +1, 更新最近使用时间"""
    now = datetime.now().isoformat(timespec='seconds')
    with get_conn() as conn:
        conn.execute("UPDATE custom_prompts SET use_count=use_count+1, last_used=? WHERE id=?",
                     (now, pid))


def delete_custom_prompt(pid):
    with get_conn() as conn:
        conn.execute("DELETE FROM custom_prompts WHERE id=?", (pid,))
