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

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'study.db')

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

CREATE INDEX IF NOT EXISTS idx_mistakes_subject ON mistakes(subject);
CREATE INDEX IF NOT EXISTS idx_mistakes_knowledge ON mistakes(knowledge_id);
CREATE INDEX IF NOT EXISTS idx_reviews_mistake ON reviews(mistake_id);
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
                                     'knowledge_id', 'image_path', 'source'):
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


def list_mistakes(subject=None, mastered=None, limit=200, offset=0):
    sql = "SELECT * FROM mistakes WHERE 1=1"
    params = []
    if subject and subject != '全部':
        sql += " AND subject = ?"
        params.append(subject)
    if mastered is not None:
        sql += " AND mastered = ?"
        params.append(int(mastered))
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_mistake(mid):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM mistakes WHERE id=?", (mid,)).fetchone()
        return dict(r) if r else None


def update_mistake(mid, **kwargs):
    if not kwargs:
        return
    sets, params = [], []
    for k, v in kwargs.items():
        sets.append(f"{k}=?")
        params.append(v)
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
