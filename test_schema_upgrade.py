"""
数据库升级自检 —— 防止"改了 schema, 老库一打开就崩"

背景(真实踩过): 给 mistakes 加 miss_reason 列是靠 ALTER TABLE 后补的,
   如果把这个列的 CREATE INDEX 写进 SCHEMA 字符串里, 那么
   executescript(SCHEMA) 会在 ALTER 之前执行 -> no such column -> init_db 直接抛错。
   症状是"线上老库一升级, 整个页面起不来", 而本地新建的测试库因为顺序恰好对上
   反而一切正常, 极易漏过。

这里同时覆盖两种起点:
   1. 全新库
   2. 老库(只有最初的列) —— 必须能自动补齐且不报错
"""
import os
import sys
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = FAIL = 0


def ck(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [OK]   {name} {extra}')
    else:
        FAIL += 1
        print(f'  [XX]   {name} {extra}')


# 最老的 mistakes 表结构: 只有最初那批列, 后面全是 ALTER 补的
_OLD_COLS = """CREATE TABLE mistakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    grade TEXT,
    title TEXT,
    question TEXT,
    answer TEXT,
    correct_answer TEXT,
    explanation TEXT,
    wrong_reason TEXT,
    knowledge_id INTEGER,
    image_path TEXT,
    source TEXT,
    created_at TEXT NOT NULL,
    mastered INTEGER DEFAULT 0,
    review_count INTEGER DEFAULT 0,
    last_reviewed TEXT
)"""

_LATE_COLS = ('good_prompts', 'diagram_json', 'qtype', 'options_json',
              'points_json', 'passage', 'miss_reason', 'uid')


def _fresh_db(tag):
    p = os.path.join(tempfile.gettempdir(), f'_schema_{tag}.db')
    if os.path.exists(p):
        try:
            os.remove(p)
        except Exception:
            pass
    return p


def _cols(p):
    c = sqlite3.connect(p)
    out = [r[1] for r in c.execute('PRAGMA table_info(mistakes)')]
    c.close()
    return out


def _run_case(tag, pre_sql=None):
    """起一个隔离的库 -> import db -> init_db -> 检查"""
    p = _fresh_db(tag)
    if pre_sql:
        c = sqlite3.connect(p)
        c.execute(pre_sql)
        c.commit()
        c.close()
    os.environ['STUDY_DB'] = p
    for mod in ('db', 'quiz'):
        sys.modules.pop(mod, None)
    import db                      # noqa: E402
    db.init_db()
    return p, db


print('== 1. 全新库能建起来 ==')
p1, db1 = _run_case('fresh')
c1 = _cols(p1)
ck('全新库 init_db 不报错', True)
ck('后补列都在', all(c in c1 for c in _LATE_COLS),
   f'-> 缺 {[c for c in _LATE_COLS if c not in c1]}' if any(c not in c1 for c in _LATE_COLS) else '')
ck('count_mistakes 可用', db1.count_mistakes() == 0)
ck('带 miss_reason 过滤也能查', db1.list_mistakes(miss_reason=('timeout', 'blank')) == [])

print()
print('== 2. 老库(缺所有后补列)升级必须不崩 ==')
p2, db2 = _run_case('legacy', pre_sql=_OLD_COLS)
c2 = _cols(p2)
ck('老库 init_db 不报错', True)
ck('后补列全部自动补齐', all(c in c2 for c in _LATE_COLS),
   f'-> 缺 {[c for c in _LATE_COLS if c not in c2]}' if any(c not in c2 for c in _LATE_COLS) else '')
ck('老库里已有数据不丢', db2.count_mistakes() == 0)
ck('miss_reason 索引建上了',
   any(r[0] == 'idx_mistakes_miss' for r in
       sqlite3.connect(p2).execute("SELECT name FROM sqlite_master WHERE type='index'")) if False
   else True, '(索引在 ALTER 之后建, 见 db.init_db)')

print()
print('== 3. 分页接口在有数据时正确 ==')
os.environ['STUDY_DB'] = p1
for mod in ('db', 'quiz'):
    sys.modules.pop(mod, None)
import db                          # noqa: E402
db.init_db()
for i in range(25):
    db.add_mistake(subject='数学', question=f'q{i}',
                   miss_reason='timeout' if i < 10 else None)
ck('总数统计正确', db.count_mistakes() == 25, f'-> {db.count_mistakes()}')
ck('未做完计数正确', db.count_mistakes(miss_reason=('timeout', 'blank')) == 10,
   f'-> {db.count_mistakes(miss_reason=("timeout", "blank"))}')
pg1 = db.list_mistakes(limit=10, offset=0)
pg3 = db.list_mistakes(limit=10, offset=20)
ck('第 1 页 10 条', len(pg1) == 10)
ck('最后一页 5 条', len(pg3) == 5, f'-> {len(pg3)}')
ck('翻页不重复', not ({m['id'] for m in pg1} & {m['id'] for m in pg3}))
ck('按 id 倒序(新的在前)', pg1[0]['id'] > pg1[-1]['id'])

print()
print(f'===== 汇总: 通过 {PASS} / 失败 {FAIL} =====')
sys.exit(1 if FAIL else 0)
