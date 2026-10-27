# -*- coding: utf-8 -*-
"""
每日测试引擎: 出题 / 判卷 / 讲解 / 进度 / 掌握度

设计原则(沿用平台风格):
- 失败要给人话, 不静默返回空, 不假装成功
- 客观题本地硬判(答案比对), 主观题 AI 参考评分并明示"待家长确认"
- AI 生成的题必须过验算才入卷: 数学用代码复算, 其他用第二遍 AI 校对
"""
import json
import os
import re
import random
import hashlib
import sqlite3
from datetime import datetime, date, timedelta
from contextlib import contextmanager
from fractions import Fraction

import db

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, 'data')
CALENDAR_PATH = os.path.join(DATA_DIR, 'quiz_calendar.json')

SUBJECTS = ['数学', '英语', '语文']
BANK_FILES = {
    '数学': 'question_bank_math.json',
    '英语': 'question_bank_english.json',
    '语文': 'question_bank_chinese.json',
}

QUIZ_SCHEMA = """
CREATE TABLE IF NOT EXISTS quiz_papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    created_at TEXT NOT NULL,
    phase_label TEXT,
    target_level INTEGER DEFAULT 3,
    total INTEGER DEFAULT 0,
    source TEXT,                -- seed / mixed / ai
    status TEXT DEFAULT 'doing', -- doing / done
    right_cnt INTEGER,
    score REAL,
    submitted_at TEXT
);

CREATE TABLE IF NOT EXISTS quiz_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL,
    q_index INTEGER NOT NULL,
    uid TEXT,
    source TEXT,                -- seed / ai
    subject TEXT,
    chapter TEXT,
    topic TEXT,
    qtype TEXT,
    difficulty INTEGER,
    stem TEXT,
    options_json TEXT,
    answer TEXT,
    alt_json TEXT,
    explain TEXT,
    verified INTEGER DEFAULT 1,
    user_answer TEXT,
    correct INTEGER,
    ai_score REAL,
    ai_comment TEXT,
    graded_by TEXT,             -- exact / ai / parent
    FOREIGN KEY(paper_id) REFERENCES quiz_papers(id)
);

CREATE TABLE IF NOT EXISTS mastery (
    subject TEXT NOT NULL,
    topic TEXT NOT NULL,
    level INTEGER DEFAULT 3,     -- 1~5, 出题目标难度
    right_cnt INTEGER DEFAULT 0,
    wrong_cnt INTEGER DEFAULT 0,
    last_at TEXT,
    PRIMARY KEY(subject, topic)
);

-- 章节手动指定的变更记录(学校跳讲时家长手动改, 每条改动都留痕, 可回溯)
CREATE TABLE IF NOT EXISTS phase_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    action TEXT NOT NULL,          -- set_manual / clear_manual
    before_text TEXT,              -- 改之前是什么
    after_text TEXT,               -- 改成什么
    note TEXT,                     -- 家长填的原因
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_bank (
    uid TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    chapter TEXT,
    topic TEXT,
    qtype TEXT,
    difficulty INTEGER,
    stem TEXT,
    options_json TEXT,
    answer TEXT,
    alt_json TEXT,
    explain TEXT,
    verified INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


@contextmanager
def _conn():
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_quiz_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with _conn() as conn:
        conn.executescript(QUIZ_SCHEMA)
        # 兼容旧库: 讲解图的结构存在这里, 已生成过的不再重复调 AI
        cols = {r[1] for r in conn.execute('PRAGMA table_info(quiz_items)')}
        if 'diagram_json' not in cols:
            conn.execute('ALTER TABLE quiz_items ADD COLUMN diagram_json TEXT')


# ============== 教学日历 ==============
def load_calendar() -> dict:
    try:
        with open(CALENDAR_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        return {'_error': f'教学日历读取失败: {e}', 'subjects': {}}


def get_offset(subject: str) -> int:
    """家长端可以整体前移/后移进度(天数). 正数=往后推, 负数=往前提"""
    try:
        return int(db.get_setting(f'quiz_offset_{subject}', '0') or 0)
    except (TypeError, ValueError):
        return 0


def set_offset(subject: str, days: int):
    db.set_setting(f'quiz_offset_{subject}', str(int(days)))


# ============== 手动指定章节 ==============
# 为什么需要: 学校常常跳着讲(先讲第三章再回头补第二章), 纯按日期推算必然对不上.
# 家长指定后写库长期生效, 每次改动都进 phase_log 留痕, 可一键恢复"按日历自动".
MANUAL_KEY = 'quiz_manual_phase_{}'

# 一个科目最多允许同时选几段(太多了会变成"什么都练", 等于没有进度)
MAX_MANUAL_PHASES = 3


def get_manual(subject: str) -> dict:
    """
    读家长的手动指定. 返回 {'indices':[...], 'topics':[...], 'set_at':..., 'note':...}
    没指定返回 {}.
    """
    raw = db.get_setting(MANUAL_KEY.format(subject), '') or ''
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            return {}
        idx = [int(i) for i in (d.get('indices') or [])]
        if not idx:
            return {}
        return {'indices': idx, 'topics': list(d.get('topics') or []),
                'set_at': (d.get('set_at') or '').replace('T', ' '),   # 老数据带 T, 统一成空格
                'note': d.get('note') or ''}
    except Exception:
        # 存坏了就当没指定, 走日历自动, 不能因为一条坏配置把整页搞崩
        return {}


def phase_options(subject: str) -> list:
    """给选择器用的选项: 每个进度段的序号/标题/日期区间/知识点"""
    out = []
    for i, ph in enumerate(phases_of(subject)):
        out.append({'index': i, 'label': ph.get('label', ''),
                    'detail': ph.get('detail', ''),
                    'from': ph.get('from', ''), 'to': ph.get('to', ''),
                    'topics': list(ph.get('topics') or [])})
    return out


def _log_phase(subject: str, action: str, before: str, after: str, note: str = ''):
    with _conn() as conn:
        conn.execute(
            'INSERT INTO phase_log(subject, action, before_text, after_text, note, at) '
            'VALUES(?,?,?,?,?,?)',
            (subject, action, before, after, note or '',
             datetime.now().isoformat(timespec='seconds')))


def set_manual(subject: str, indices: list, topics: list = None, note: str = '') -> dict:
    """
    手动指定某科当前在学哪些进度段(可多段), 可再限定只练其中部分知识点.
    indices: 进度段序号(0 开始); topics: 留空=整个段全练
    返回 {'ok':bool, 'msg':str, 'manual':{...}}
    """
    phs = phases_of(subject)
    if not phs:
        return {'ok': False, 'msg': f'{subject} 的教学日历没读到, 没法指定', 'manual': {}}
    raw_idx = [int(i) for i in (indices or [])]
    idx = sorted({i for i in raw_idx if 0 <= i < len(phs)})
    if not idx:
        if raw_idx:
            return {'ok': False,
                    'msg': f'选的章节不在日历里（{subject} 一共只有 {len(phs)} 段，序号 0~{len(phs) - 1}）',
                    'manual': {}}
        return {'ok': False, 'msg': '至少要选一个章节', 'manual': {}}
    if len(idx) > MAX_MANUAL_PHASES:
        return {'ok': False,
                'msg': f'最多同时指定 {MAX_MANUAL_PHASES} 段 —— 选太多就变成"什么都练", 等于没有进度',
                'manual': {}}

    # topics 只能落在所选段的范围内, 防止配置和段对不上
    allowed = []
    for i in idx:
        allowed += list(phs[i].get('topics') or [])
    tps = [t for t in (topics or []) if t in allowed]
    if tps and len(tps) == len(allowed):
        tps = []          # 全选了就等于整段, 去掉限定, 免得日志里啰嗦

    before = _manual_text(get_manual(subject), subject)
    payload = {'indices': idx, 'topics': tps,
               'set_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
               'note': (note or '').strip()[:120]}
    db.set_setting(MANUAL_KEY.format(subject), json.dumps(payload, ensure_ascii=False))

    after = _manual_text(payload, subject)
    _log_phase(subject, 'set_manual', before, after, payload['note'])
    return {'ok': True, 'msg': f'已记录: {after}', 'manual': payload}


def clear_manual(subject: str, note: str = '') -> dict:
    """恢复成按教学日历自动推算"""
    m = get_manual(subject)
    if not m:
        return {'ok': True, 'msg': f'{subject} 本来就是按日历自动的, 不需要恢复', 'manual': {}}
    before = _manual_text(m, subject)
    db.set_setting(MANUAL_KEY.format(subject), '')
    _log_phase(subject, 'clear_manual', before, '按教学日历自动', note)
    return {'ok': True, 'msg': f'已改回按日历自动（原为: {before}）', 'manual': {}}


def _manual_text(m: dict, subject: str) -> str:
    """把手动指定翻译成一句人话, 给日志和界面用"""
    if not m:
        return '按教学日历自动'
    phs = phases_of(subject)
    labels = [phs[i].get('label', '') for i in (m.get('indices') or []) if 0 <= i < len(phs)]
    txt = ' + '.join(labels) or '（无效指定）'
    if m.get('topics'):
        txt += f"（只练 {'、'.join(m['topics'])}）"
    return txt


def manual_summary(subject: str) -> str:
    m = get_manual(subject)
    if not m:
        return ''
    when = m.get('set_at', '')
    return _manual_text(m, subject) + (f' · 指定于 {when}' if when else '')


def phase_log(subject: str = '', limit: int = 30) -> list:
    """进度变更记录, 最近的在前"""
    sql = 'SELECT * FROM phase_log'
    args = []
    if subject:
        sql += ' WHERE subject=?'
        args.append(subject)
    sql += ' ORDER BY id DESC LIMIT ?'
    args.append(int(limit))
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def phases_of(subject: str) -> list:
    cal = load_calendar()
    return (cal.get('subjects', {}).get(subject) or {}).get('phases', [])


def current_phase(subject: str, on: date = None) -> dict:
    """
    当前该练哪一段.
    优先级: 家长手动指定 > 按日期从教学日历推算.
    返回 {label, detail, topics, index, total, from, to, manual?}
    """
    phs = phases_of(subject)
    if not phs:
        return None

    # ---- 1) 家长手动指定优先 ----
    m = get_manual(subject)
    if m:
        idx = [i for i in m['indices'] if 0 <= i < len(phs)]
        if idx:
            picked = [phs[i] for i in idx]
            topics = []
            for p in picked:
                for t in (p.get('topics') or []):
                    if t not in topics:
                        topics.append(t)
            # 段内再限定: 只保留家长勾选的知识点
            if m.get('topics'):
                sel = [t for t in topics if t in m['topics']]
                if sel:
                    topics = sel
            if len(picked) == 1:
                label = picked[0].get('label', '')
                detail = picked[0].get('detail', '')
            else:
                label = ' + '.join(p.get('label', '') for p in picked)
                detail = ' ／ '.join(p.get('detail', '') for p in picked if p.get('detail'))
            return {'label': label, 'detail': detail, 'topics': topics,
                    'index': idx[0], 'total': len(phs),
                    'from': min(p['from'] for p in picked), 'to': max(p['to'] for p in picked),
                    'manual': True, 'manual_set_at': m.get('set_at', ''),
                    'manual_note': m.get('note', '')}

    # ---- 2) 按日期推算 ----
    on = on or date.today()
    on = on + timedelta(days=get_offset(subject))
    iso = on.isoformat()
    for i, ph in enumerate(phs):
        if ph['from'] <= iso <= ph['to']:
            return {**ph, 'index': i, 'total': len(phs)}
    # 超出日历范围: 明说, 不猜
    if iso < phs[0]['from']:
        return {**phs[0], 'index': 0, 'total': len(phs), 'out_of_range': 'before'}
    return {**phs[-1], 'index': len(phs) - 1, 'total': len(phs), 'out_of_range': 'after'}



def progress_hint(subject: str) -> str:
    """给家长/孩子看的一句进度说明"""
    ph = current_phase(subject)
    if not ph:
        return f'{subject}: 教学日历未配置, 请到家长端检查 data/quiz_calendar.json'
    base = f"{ph['label']}（第 {ph['index'] + 1}/{ph['total']} 段）"
    if ph.get('manual'):
        when = ph.get('manual_set_at') or ''
        return base + f" · 家长手动指定{f'（{when}）' if when else ''}"
    if ph.get('out_of_range') == 'before':
        return base + ' · 还没开学, 现在按第一段出题'
    if ph.get('out_of_range') == 'after':
        return base + ' · 已超出日历范围, 现在按最后一段出题'
    off = get_offset(subject)
    if off:
        return base + f' · 已手动调整 {off:+d} 天'
    return base


# ============== 题库 ==============
def _seed_bank(subject: str) -> list:
    fn = BANK_FILES.get(subject)
    if not fn:
        return []
    path = os.path.join(DATA_DIR, fn)
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    for q in data.get('questions', []):
        out.append({
            'uid': q.get('id') or hashlib.md5(q['stem'].encode('utf-8')).hexdigest()[:8],
            'subject': subject,
            'chapter': q.get('chapter', ''),
            'topic': q.get('topic', ''),
            'qtype': q.get('type', 'fill'),
            'difficulty': int(q.get('difficulty', 3)),
            'stem': q.get('stem', ''),
            'options': q.get('options'),
            'answer': str(q.get('answer', '')),
            'alt': q.get('alt', []) or [],
            'explain': q.get('explain', ''),
            'source': 'seed',
            'verified': True,
        })
    return out


def _ai_bank(subject: str, verified_only: bool = True) -> list:
    with _conn() as conn:
        sql = 'SELECT * FROM ai_bank WHERE subject = ?'
        if verified_only:
            sql += ' AND verified = 1'
        rows = conn.execute(sql, (subject,)).fetchall()
    out = []
    for r in rows:
        out.append({
            'uid': r['uid'], 'subject': r['subject'], 'chapter': r['chapter'] or '',
            'topic': r['topic'] or '', 'qtype': r['qtype'], 'difficulty': r['difficulty'],
            'stem': r['stem'], 'options': json.loads(r['options_json']) if r['options_json'] else None,
            'answer': r['answer'], 'alt': json.loads(r['alt_json']) if r['alt_json'] else [],
            'explain': r['explain'], 'source': 'ai', 'verified': bool(r['verified']),
        })
    return out


def all_bank(subject: str) -> list:
    return _seed_bank(subject) + _ai_bank(subject)


def bank_stats(subject: str) -> dict:
    seed = _seed_bank(subject)
    ai = _ai_bank(subject)
    topics = sorted({q['topic'] for q in seed + ai if q['topic']})
    return {'seed': len(seed), 'ai': len(ai), 'total': len(seed) + len(ai), 'topics': topics}


def scope_stats(subject: str, on: date = None) -> dict:
    """当前进度段能出多少题、题型怎么分布。页面用它解释「为什么只能出这么多」，
    而不是让用户对着一个填不上去的数字猜。"""
    ph = current_phase(subject, on)
    bank = all_bank(subject)
    if not ph:
        return {'in_scope': 0, 'bank': len(bank), 'types': {}, 'phase': None}
    topics = ph.get('topics', [])
    in_scope = [q for q in bank if q['topic'] in topics or q['chapter'] in topics]
    types = {}
    for q in in_scope:
        k = q.get('qtype') or 'fill'
        types[k] = types.get(k, 0) + 1
    return {'in_scope': len(in_scope), 'bank': len(bank), 'types': types, 'phase': ph}


# ============== 掌握度 ==============
def get_mastery(subject: str) -> dict:
    with _conn() as conn:
        rows = conn.execute('SELECT * FROM mastery WHERE subject = ?', (subject,)).fetchall()
    return {r['topic']: dict(r) for r in rows}


def topic_level(subject: str, topic: str) -> int:
    m = get_mastery(subject).get(topic)
    return int(m['level']) if m else 3


def update_mastery(subject: str, topic: str, correct: bool, conn=None):
    """对 +1 / 错 -1, 夹在 1~5. 传 conn 可在同一事务内完成(判卷时用, 避免多连接互相锁库)"""
    if not topic:
        return
    if conn is not None:
        _mastery_upsert(conn, subject, topic, correct)
        return
    with _conn() as c:
        _mastery_upsert(c, subject, topic, correct)


def _mastery_upsert(conn, subject: str, topic: str, correct: bool):
    now = datetime.now().isoformat(timespec='seconds')
    row = conn.execute('SELECT * FROM mastery WHERE subject=? AND topic=?', (subject, topic)).fetchone()
    if row:
        level = int(row['level'])
        level = min(5, level + 1) if correct else max(1, level - 1)
        conn.execute(
            'UPDATE mastery SET level=?, right_cnt=right_cnt+?, wrong_cnt=wrong_cnt+?, last_at=? '
            'WHERE subject=? AND topic=?',
            (level, 1 if correct else 0, 0 if correct else 1, now, subject, topic))
    else:
        level = 4 if correct else 2
        conn.execute(
            'INSERT OR REPLACE INTO mastery(subject,topic,level,right_cnt,wrong_cnt,last_at) VALUES(?,?,?,?,?,?)',
            (subject, topic, level, 1 if correct else 0, 0 if correct else 1, now))


# ============== AI 出题与验算 ==============
_GEN_SYSTEM = """你是一名北京市初中一线教师, 正在给初一学生出练习题。
要求:
1. 题目必须贴合给定的知识点, 难度符合初中一年级水平, 不超纲。
2. 答案必须唯一、确定。数学计算题必须能被复算验证。
3. 解析要写给刚学这个知识点的孩子看, 说清楚"为什么", 点出易错点, 不要只给步骤。
4. 输出严格 JSON, 不要有任何多余文字。

输出格式:
{"questions":[{
  "type":"choice" | "fill" | "calc" | "answer" | "judge",
  "stem":"题面",
  "options":{"A":"...","B":"...","C":"...","D":"..."},   // 仅 choice 需要
  "answer":"choice 填 A/B/C/D; fill/calc 填简洁结果; judge 填 对 或 错; answer 填解题要点与最终结论",
  "alt":["可接受的其它写法"],
  "explain":"给孩子的讲解",
  "difficulty":1-5,
  "verify":{"expr":"可直接被 Python eval 的算式, 结果须等于 answer", "value":"算式结果"}
}]}
数学题必须给 verify; 语文英语主观题可不给 verify, 但答案要点要写在 answer 里。"""


def _eval_expr(expr: str):
    """安全范围内验算: 只允许数字/运算符/Fraction"""
    if not expr:
        return None
    # 只允许数字运算符, 外加 Fraction(,) 这个白名单词(分数题要用)
    stripped = re.sub(r'Fraction', '', str(expr))
    allowed = set('0123456789+-*/().,% ')
    if not set(stripped) <= allowed:
        return None
    try:
        return eval(expr, {'__builtins__': {}, 'Fraction': Fraction}, {})
    except Exception:
        return None


def ai_generate_questions(subject: str, topic: str, chapter: str, n: int = 5,
                          difficulty: int = 3, want_types: list = None) -> dict:
    """
    AI 出变式题, 含自动验算。
    want_types: 指定题型偏好, 如 ['judge','answer'], 避免每次都只出选择题
    返回 {'ok':bool, 'questions':[...], 'msg':str, 'rejected':int}
    """
    import llm
    if not llm.is_llm_enabled():
        return {'ok': False, 'questions': [], 'msg': 'AI 老师未配置, 无法生成变式题(可在家长端配置 Key)', 'rejected': 0}

    ver = {'数学': '人教版七年级上册', '英语': '北师大版七年级上册', '语文': '统编版七年级上册'}.get(subject, '')
    if want_types:
        type_req = f"题型要求: 这 {n} 道题里必须覆盖这些题型 {', '.join(want_types)}(类型名照抄), 不要全出同一种。"
    elif subject == '数学':
        type_req = ("题型要求: 混合出题, 至少覆盖三种 —— choice(选择)、fill(填空)、"
                    "以及 judge(判断,答案是 对/错) 或 answer(要写过程的解答题, 应用题/几何推理题)。"
                    "严禁全是选择题。")
    else:
        type_req = "题型要求: 混合出 choice(选择)、fill(填空)、answer(简答) 三种, 不要全出同一种。"
    user = (f"科目: {subject}（教材 {ver}）\n章节: {chapter}\n知识点: {topic}\n"
            f"难度: {difficulty}/5\n数量: {n}\n"
            f"要求: 出 {n} 道考查「{topic}」的新题。{type_req}\n"
            f"不要和下面的样题重复:\n（无样题）")

    res = llm._call_chat(_GEN_SYSTEM, user, json_mode=True)
    if res.get('error'):
        return {'ok': False, 'questions': [], 'msg': f"出题失败: {res['error']}", 'rejected': 0}
    raw = res.get('questions')
    if not raw:
        return {'ok': False, 'questions': [], 'msg': 'AI 返回的题目为空, 请重试或换个知识点', 'rejected': 0}
    if not isinstance(raw, list):
        raw = [raw]

    out, rejected = [], 0
    for q in raw:
        if not q.get('stem') or not q.get('answer'):
            rejected += 1
            continue
        qtype = q.get('type') or ('choice' if q.get('options') else 'fill')
        if qtype == 'choice' and q.get('options'):
            if str(q['answer']).strip().upper()[:1] not in (q['options'].keys()):
                rejected += 1
                continue
        if qtype == 'judge':
            if str(q.get('answer', '')).strip() not in ('对', '错', '正确', '错误', '√', '×', 'T', 'F', 'true', 'false'):
                # AI 把判断题写成了别的形式, 统一成 对/错 再收, 认不出就丢
                _j = normalize(q.get('answer')).lower()
                if _j in ('对', '正确', '√', 't', 'true'):
                    q['answer'] = '对'
                elif _j in ('错', '错误', 'x', '*', 'f', 'false'):
                    q['answer'] = '错'
                else:
                    rejected += 1
                    continue
        # 自动验算
        verified = False
        v = q.get('verify') or {}
        val = _eval_expr(str(v.get('expr', '')))
        if val is not None:
            want = v.get('value', q.get('answer'))
            try:
                ok = abs(float(val) - float(Fraction(str(want)))) < 1e-9
            except Exception:
                try:
                    ok = abs(float(val) - float(str(want))) < 1e-9
                except Exception:
                    ok = False
            if not ok:
                rejected += 1
                continue
            verified = True
        else:
            # 没有验算表达式: 用第二遍 AI 校对
            chk = ai_double_check(subject, q.get('stem'), str(q.get('answer')))
            if chk.get('ok') and not chk.get('agree', True):
                rejected += 1
                continue
            verified = bool(chk.get('ok'))
        out.append({
            'uid': 'ai-' + hashlib.md5((subject + q['stem']).encode('utf-8')).hexdigest()[:10],
            'subject': subject, 'chapter': chapter, 'topic': topic,
            'qtype': qtype, 'difficulty': int(q.get('difficulty') or difficulty),
            'stem': q['stem'], 'options': q.get('options'),
            'answer': str(q['answer']), 'alt': q.get('alt') or [],
            'explain': q.get('explain') or '', 'source': 'ai', 'verified': verified,
        })
    if not out:
        return {'ok': False, 'questions': [], 'msg': f'AI 出了 {len(raw)} 道题但全部未通过验算, 已全部丢弃', 'rejected': rejected}
    return {'ok': True, 'questions': out, 'msg': f'通过验算 {len(out)} 道, 淘汰 {rejected} 道', 'rejected': rejected}


def ai_double_check(subject: str, stem: str, answer: str) -> dict:
    """第二遍 AI 校对: 让它独立作答, 看是否与给定答案一致"""
    import llm
    if not llm.is_llm_enabled():
        return {'ok': False, 'agree': None}
    system = '你是严格的阅卷老师。只回答最终答案, 不要解释。'
    user = f"科目: {subject}\n题目: {stem}\n请直接给出正确答案(简洁形式):"
    res = llm._call_chat(system, user, json_mode=False)
    text = (res.get('text') or '').strip()
    if not text:
        return {'ok': False, 'agree': None}
    return {'ok': True, 'agree': normalize(text).find(normalize(answer)) >= 0 or normalize(answer) in normalize(text),
            'raw': text}


# ============== 显示安全 ==============
_MD_SPECIAL = re.compile(r'([\\`*_$<>|])')


def md_safe(text) -> str:
    """
    题干/选项/答案在页面上是用 Markdown 渲染的, 里面这些字符会被当成格式标记吞掉:
      `*`  -> 斜体        (m049: 计算 x*y ... (-2)*3  变成  ( -2)3, 乘号消失)
      `_`  -> 斜体/粗体    (m041: ____°____  变成  °′, 填空线消失)
      `$`  -> 数学公式     (两处 $ 之间的内容会被 KaTeX 重排)
      `<>` -> HTML 标签
    题库和 AI 生成的题都可能带这些字符, 所以在渲染前统一转义, 保证屏幕上和题库一字不差。
    """
    if text is None:
        return ''
    s = str(text)
    s = s.replace('&', '&amp;')
    return _MD_SPECIAL.sub(r'\\\1', s)


# ============== 判卷 ==============
def normalize(s) -> str:
    """答案标准化: 全角转半角、去空白、中文标点转英文、去掉常见单位后缀"""
    s = str(s or '').strip()
    # 全角 -> 半角
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            out.append(' ')
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    s = ''.join(out)
    s = s.replace('×', '*').replace('÷', '/').replace('－', '-').replace('＋', '+')
    s = s.replace('。', '.').replace('，', ',').replace('、', ',').replace('；', ';')
    s = s.replace('：', ':').replace('？', '?').replace('（', '(').replace('）', ')')
    s = re.sub(r'\s+', '', s)
    s = s.rstrip('.')
    return s


def _as_number(s):
    try:
        return float(Fraction(str(s)))
    except Exception:
        try:
            return float(str(s))
        except Exception:
            return None


def is_correct(q: dict, user_answer) -> bool:
    qt = (q.get('qtype') or q.get('type') or '').lower()
    ans_raw = q.get('answer')

    # ---- 多选题: 选择集合完全一致才算对(少选/多选都算错) ----
    if qt == 'multi':
        if isinstance(user_answer, (list, tuple, set)):
            ua_set = {str(x).strip().upper()[:1] for x in user_answer if str(x).strip()}
        else:
            ua_set = {c.upper() for c in re.findall(r'[A-Da-d]', str(user_answer or ''))}
        ans_set = {c.upper() for c in re.findall(r'[A-Da-d]', str(ans_raw or ''))}
        return bool(ua_set) and ua_set == ans_set

    # ---- 判断题: 对/错的各种写法都认 ----
    if qt == 'judge':
        ua = normalize(user_answer).lower()
        if isinstance(user_answer, bool):
            ua = '对' if user_answer else '错'
        yes = {'对', '正确', '√', 't', 'true', 'yes', 'y', '是', '对的'}
        no = {'错', '错误', 'x', '×', '*', 'f', 'false', 'no', 'n', '否', '错的'}
        ans_yes = normalize(ans_raw).lower() in yes or str(ans_raw).strip() in ('对', '√', '正确', 'T', 'True')
        if ua in yes:
            return ans_yes
        if ua in no:
            return not ans_yes
        return False

    ua = normalize(user_answer)
    if not ua:
        return False
    ans = normalize(ans_raw)
    if ua == ans:
        return True
    # 大小写容错: 英语单词(Library/library)、选项字母(b/B)
    if ua.lower() == ans.lower() and re.search(r'[A-Za-z]', ans):
        return True
    for a in (q.get('alt') or []):
        if ua == normalize(a):
            return True
    nu, na = _as_number(ua), _as_number(ans)
    if nu is not None and na is not None and abs(nu - na) < 1e-9:
        return True
    # 答了选项内容(如 "3" 而不是 "B")
    if q.get('options'):
        for k, v in q['options'].items():
            if normalize(v) == ua and normalize(k) == ans:
                return True
    return False


def ai_grade(subject: str, stem: str, ref_answer: str, user_answer: str, full_score: int = 10) -> dict:
    """主观题 AI 参考评分. 明示这是参考分, 需家长确认"""
    import llm
    if not llm.is_llm_enabled():
        return {'ok': False, 'score': None, 'comment': 'AI 老师未配置, 这题需要家长手动批改'}
    if not str(user_answer or '').strip():
        return {'ok': True, 'score': 0, 'comment': '没有作答, 记 0 分。参考要点: ' + str(ref_answer)[:200]}
    system = ('你是北京市初中阅卷老师, 按要点给分。输出严格 JSON: '
              '{"score":0-10, "hit":["答对的要点"], "miss":["漏掉的要点"], "advice":"给孩子的改进建议"}')
    user = (f"科目: {subject}\n题目: {stem}\n参考答案要点: {ref_answer}\n"
            f"学生答案: {user_answer}\n满分 {full_score} 分, 按要点给分, 不要因为表述不同扣分。")
    res = llm._call_chat(system, user, json_mode=True)
    if res.get('error'):
        return {'ok': False, 'score': None, 'comment': f'评分失败: {res["error"]}'}
    try:
        score = float(res.get('score', 0))
    except (TypeError, ValueError):
        score = 0.0
    comment = []
    if res.get('hit'):
        comment.append('答到了: ' + '；'.join(map(str, res['hit'])))
    if res.get('miss'):
        comment.append('漏了: ' + '；'.join(map(str, res['miss'])))
    if res.get('advice'):
        comment.append('建议: ' + str(res['advice']))
    return {'ok': True, 'score': round(score, 1), 'comment': '　'.join(comment) or '暂无评语'}


# ============== 组卷 ==============
TYPE_NAMES = {'choice': '选择', 'fill': '填空', 'calc': '计算',
              'answer': '解答', 'judge': '判断', 'multi': '多选'}


def _recent_uids(subject: str, days: int = 10, limit: int = 300) -> set:
    """最近做过的题 uid。用来优先出没见过的题, 避免每次刷新都是同一批。"""
    try:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec='seconds')
        with _conn() as conn:
            rows = conn.execute(
                'SELECT qi.uid AS uid FROM quiz_items qi JOIN quiz_papers qp ON qi.paper_id = qp.id '
                'WHERE qp.subject = ? AND qp.created_at >= ? ORDER BY qp.id DESC LIMIT ?',
                (subject, since, limit)).fetchall()
        return {r['uid'] for r in rows if r['uid']}
    except Exception:
        return set()


def _pick_by_mix(subject: str, pool: list, n: int, seen: set) -> list:
    """
    题型轮转 + 难度阶梯 挑题。

    以前只按「难度贴近掌握度」排序, 结果一张卷子全是同类型的计算题;
    孩子看到的就是"怎么又是去括号"。现在改成:
    - 每轮每种题型各取 1 道 → 一张卷子里题型不会重样
    - 轮次之间难度上下拉开(0,+1,-1,+2,-2) → 自然形成由易到难
    - 最近做过的题降权 → 每次刷新都尽量给没见过的
    """
    by_type = {}
    for q in pool:
        by_type.setdefault(q.get('qtype') or 'fill', []).append(q)
    if not by_type:
        return []
    # 题多的题型先轮, 小众题型(判断/多选)也会轮到但排在后面
    order = sorted(by_type.keys(), key=lambda t: (-len(by_type[t]), t))
    picked, used = [], set()
    delta_seq = [0, 1, -1, 2, -2, 0, 1]
    rnd = 0
    while len(picked) < n and rnd < n * 3 + 3:
        delta = delta_seq[rnd % len(delta_seq)]
        progressed = False
        for t in order:
            if len(picked) >= n:
                break
            cand = [q for q in by_type[t] if q['uid'] not in used]
            if not cand:
                continue

            def _key(q, _delta=delta):
                lvl = topic_level(subject, q.get('topic') or '')
                want = max(1, min(5, lvl + _delta))
                return (1 if q['uid'] in seen else 0,
                        abs(int(q.get('difficulty') or 3) - want),
                        random.random())

            q = min(cand, key=_key)
            picked.append(q)
            used.add(q['uid'])
            progressed = True
        if not progressed:
            break
        rnd += 1
    return picked


def pick_questions(subject: str, n: int = 10, on: date = None, use_ai: bool = True) -> dict:
    """
    按当前进度 + 掌握度组卷。
    返回 {'ok':bool, 'questions':[...], 'phase':..., 'msg':..., 'ai_added':int, 'review_added':int}
    """
    ph = current_phase(subject, on)
    if not ph:
        return {'ok': False, 'questions': [], 'msg': f'{subject} 的教学进度未配置', 'phase': None, 'ai_added': 0}
    topics = ph.get('topics', [])
    bank = all_bank(subject)

    # 1) 当前进度范围内的题
    in_scope = [q for q in bank if q['topic'] in topics or q['chapter'] in topics]
    seen = _recent_uids(subject)

    # 2) 题型轮转 + 难度阶梯。优先只用"没做过"的题, 做完才回头重复
    fresh = [q for q in in_scope if q['uid'] not in seen]
    pool = fresh if len(fresh) >= n else in_scope
    picked = _pick_by_mix(subject, pool, n, seen)
    used = {q['uid'] for q in picked}

    # 3) 当前进度段的题不够 → 从已学过的其它章节补「复习题」(不超纲, 都是学过的)
    review_added = 0
    if len(picked) < n:
        rest = [q for q in bank if q['uid'] not in used]
        # 没做过的排前面, 难度别太高的排前面
        rest.sort(key=lambda q: (1 if q['uid'] in seen else 0,
                                 abs(int(q.get('difficulty') or 3) - 3),
                                 random.random()))
        for q in rest:
            if len(picked) >= n:
                break
            q = dict(q, review=True)
            picked.append(q)
            used.add(q['uid'])
            review_added += 1

    # 3b) 当前进度段已经练完(选出来的一大半都是做过的) → 用跨章节没做过的题换掉一部分,
    #     否则孩子第二次刷新会看到一模一样的卷子。上限 40%, 保证仍以当前进度为主。
    if seen and picked:
        stale = [q for q in picked if q['uid'] in seen]
        fresh_cnt = len(picked) - len(stale)
        if stale and fresh_cnt < n:
            cap = int(n * 0.4)
            rest_new = [q for q in bank if q['uid'] not in used and q['uid'] not in seen]
            random.shuffle(rest_new)
            for old in stale[:min(len(stale), max(0, min(n - fresh_cnt, cap)))]:
                if not rest_new:
                    break
                newq = dict(rest_new.pop(), review=True)
                picked[picked.index(old)] = newq
                used.add(newq['uid'])
                review_added += 1

    # 4) 还不够(题库总量不足) → AI 补, 要求题型多样, 且过验算
    ai_added = 0
    have_types = {q.get('qtype') for q in picked}
    # 缺什么题型就让 AI 补什么题型
    want = [t for t in ('judge', 'answer', 'calc', 'choice') if t not in have_types]
    parts = [f"题库出 {len(picked) - review_added} 道"]
    if review_added:
        parts.append(f"跨章节复习 {review_added} 道")

    if len(picked) < n:
        need = n - len(picked)
        if not use_ai:
            parts.append(f"还差 {need} 道（题库总量不够, 未开启 AI 补充）")
        else:
            import llm
            if not llm.is_llm_enabled():
                parts.append(f"还差 {need} 道（题库总量不够, 且 AI 未配置）")
            else:
                gen = {'msg': '当前进度没有可用知识点'}
                cand_topics = topics or [ph.get('label', '') or subject]
                tries = 0
                for t in random.sample(cand_topics, min(len(cand_topics), 6)):
                    if ai_added >= need or tries >= 8:
                        break
                    tries += 1
                    gen = ai_generate_questions(subject, t, ph.get('label', ''), n=min(4, need - ai_added),
                                                difficulty=topic_level(subject, t),
                                                want_types=want[:2] if want else None)
                    for q in gen.get('questions', []):
                        if ai_added >= need:
                            break
                        if all(normalize(q['stem']) != normalize(p['stem']) for p in picked):
                            save_ai_question(q)
                            picked.append(q)
                            ai_added += 1
                            if q.get('qtype') in want:
                                want.remove(q['qtype'])
                if ai_added:
                    parts.append(f"AI 现出 {ai_added} 道（已过验算）")
                else:
                    parts.append(f"还差 {need} 道, AI 补充没成（{gen.get('msg', '未知原因')}）")

    # 5) 上次做错的知识点, 塞一道进来复习
    for t in recent_wrong_topics(subject, limit=3):
        cand = [q for q in bank if q['topic'] == t and q['uid'] not in used]
        if cand:
            q = dict(random.choice(cand), review=True)
            picked.insert(0, q)
            used.add(q['uid'])
            break

    if not picked:
        return {'ok': False, 'questions': [], 'msg': '没选出题来：题库和 AI 都没能给出题目', 'phase': ph, 'ai_added': 0}
    random.shuffle(picked)
    types_in = [TYPE_NAMES.get(t, t) for t in sorted({q.get('qtype') for q in picked})]
    msg = '，'.join(parts) + f"；本卷题型：{'/'.join(types_in)}"
    return {'ok': True, 'questions': picked, 'phase': ph, 'msg': msg,
            'ai_added': ai_added, 'review_added': review_added}


def save_ai_question(q: dict):
    with _conn() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO ai_bank(uid,subject,chapter,topic,qtype,difficulty,stem,options_json,'
            'answer,alt_json,explain,verified,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (q['uid'], q['subject'], q.get('chapter'), q.get('topic'), q.get('qtype'),
             int(q.get('difficulty') or 3), q['stem'],
             json.dumps(q.get('options'), ensure_ascii=False) if q.get('options') else None,
             str(q.get('answer')), json.dumps(q.get('alt') or [], ensure_ascii=False),
             q.get('explain'), 1 if q.get('verified') else 0,
             datetime.now().isoformat(timespec='seconds')))


def recent_wrong_topics(subject: str, limit: int = 3) -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT topic, COUNT(*) c FROM quiz_items WHERE subject=? AND correct=0 AND topic<>'' "
            "GROUP BY topic ORDER BY MAX(id) DESC LIMIT ?", (subject, limit)).fetchall()
    return [r['topic'] for r in rows]


# ============== 试卷落库 / 交卷 / 报告 ==============
def create_paper(subject: str, questions: list, phase_label: str = '', target_level: int = 3) -> int:
    source = 'seed'
    if any(q['source'] == 'ai' for q in questions):
        source = 'mixed' if any(q['source'] == 'seed' for q in questions) else 'ai'
    with _conn() as conn:
        cur = conn.execute(
            'INSERT INTO quiz_papers(subject,created_at,phase_label,target_level,total,source,status) '
            'VALUES(?,?,?,?,?,?,?)',
            (subject, datetime.now().isoformat(timespec='seconds'), phase_label,
             target_level, len(questions), source, 'doing'))
        pid = cur.lastrowid
        for i, q in enumerate(questions):
            conn.execute(
                'INSERT INTO quiz_items(paper_id,q_index,uid,source,subject,chapter,topic,qtype,difficulty,'
                'stem,options_json,answer,alt_json,explain,verified) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (pid, i, q['uid'], q['source'], subject, q.get('chapter'), q.get('topic'), q.get('qtype'),
                 int(q.get('difficulty') or 3), q['stem'],
                 json.dumps(q.get('options'), ensure_ascii=False) if q.get('options') else None,
                 str(q.get('answer')), json.dumps(q.get('alt') or [], ensure_ascii=False),
                 q.get('explain'), 1 if q.get('verified') else 0))
    return pid


def get_paper(pid: int) -> dict:
    with _conn() as conn:
        p = conn.execute('SELECT * FROM quiz_papers WHERE id=?', (pid,)).fetchone()
        if not p:
            return {}
        items = conn.execute('SELECT * FROM quiz_items WHERE paper_id=? ORDER BY q_index', (pid,)).fetchall()
    return {'paper': dict(p), 'items': [dict(i) for i in items]}


def save_answer(item_id: int, user_answer: str):
    with _conn() as conn:
        conn.execute('UPDATE quiz_items SET user_answer=? WHERE id=?', (str(user_answer or ''), item_id))


def grade_paper(pid: int, subject: str) -> dict:
    """判卷: 客观题本地硬判, 主观题 AI 参考评分(标记待家长确认)"""
    data = get_paper(pid)
    if not data:
        return {'ok': False, 'msg': f'找不到试卷 #{pid}'}
    items = data['items']
    pending_ai = []
    results = []
    # 第一步: 先判(AI 评分要联网, 不要在写事务里做, 否则长锁)
    for it in items:
        if it['qtype'] == 'answer':
            g = ai_grade(subject, it['stem'], it['answer'], it['user_answer'])
            score = g.get('score')
            correct = 1 if (score is not None and score >= 6) else 0
            results.append((it['id'], correct, score, g.get('comment'),
                            'ai' if g.get('ok') else 'parent', it['topic']))
            if not g.get('ok'):
                pending_ai.append(it['id'])
        else:
            ok = is_correct({'answer': it['answer'], 'alt': json.loads(it['alt_json'] or '[]'),
                             'qtype': it['qtype'],
                             'options': json.loads(it['options_json']) if it['options_json'] else None},
                            it['user_answer'])
            results.append((it['id'], 1 if ok else 0, None, None, 'exact', it['topic']))
    # 第二步: 同一事务内写结果 + 更新掌握度
    with _conn() as conn:
        for (iid, correct, score, comment, by, topic) in results:
            conn.execute(
                'UPDATE quiz_items SET correct=?, ai_score=?, ai_comment=?, graded_by=? WHERE id=?',
                (correct, score, comment, by, iid))
            update_mastery(subject, topic or '', bool(correct), conn=conn)

    # 统计
    items2 = get_paper(pid)['items']
    right = sum(1 for i in items2 if i['correct'] == 1)
    total = len(items2)
    score = round(right / total * 100, 1) if total else 0
    with _conn() as conn:
        conn.execute('UPDATE quiz_papers SET status=?, right_cnt=?, score=?, submitted_at=? WHERE id=?',
                     ('done', right, score, datetime.now().isoformat(timespec='seconds'), pid))
    pushed = push_wrong_to_mistakes(pid, subject)
    return {'ok': True, 'right': right, 'total': total, 'score': score,
            'pending_ai': pending_ai, 'pushed': pushed['count'], 'pairs': pushed['pairs']}


def push_wrong_to_mistakes(pid: int, subject: str) -> dict:
    """
    判完把错题流进现有错题本, 复用讲解链路。
    返回 {'count': n, 'pairs': [(mistake_id, item_id), ...]} —— pairs 供后续把讲解图挂上去。
    """
    data = get_paper(pid)
    n = 0
    pairs = []
    for it in data.get('items', []):
        if it['correct'] == 1:
            continue
        try:
            mid = db.add_mistake(
                subject=subject,
                question=it['stem'],
                grade='初一',
                title=f"[{it['topic'] or it['chapter']}] {it['stem'][:28]}",
                answer=it['user_answer'] or '(未作答)',
                correct_answer=it['answer'],
                explanation=(it['explain'] or '') + (('\n\nAI 评语: ' + it['ai_comment']) if it.get('ai_comment') else ''),
                wrong_reason='每日测试',
                source='quiz',
                diagram_json=it.get('diagram_json') or None,
            )
            n += 1
            if mid:
                pairs.append((mid, it['id']))
        except Exception:
            continue
    return {'count': n, 'pairs': pairs}


# ============== 讲解思维导图 ==============
MAX_DIAGRAMS_PER_PAPER = 15     # 一次判卷最多画几张(实测 4 并发下 15 张约 10 秒)


def get_item(item_id: int) -> dict:
    with _conn() as conn:
        r = conn.execute('SELECT * FROM quiz_items WHERE id=?', (item_id,)).fetchone()
    return dict(r) if r else {}


def _cached_spec(row: dict):
    raw = row.get('diagram_json')
    if not raw:
        return None
    try:
        import diagram
        d = json.loads(raw)
        if not isinstance(d, dict):
            return None
        # 老版本(没有错点定位)的图一律当没画过, 重新画一张带红标的
        if int(d.get('v') or 0) < diagram.SPEC_VER:
            return None
        spec = d.get('spec')
        return (spec, bool(d.get('fallback'))) if spec else None
    except Exception:
        return None


def diagram_is_old(row: dict) -> bool:
    """这张图是不是老版本(还没有错点定位)? 用于提示用户重画。"""
    try:
        import diagram
        d = json.loads((row or {}).get('diagram_json') or '{}')
        return (not isinstance(d, dict)) or int(d.get('v') or 0) < diagram.SPEC_VER
    except Exception:
        return True


def gen_diagram(item_id: int, force: bool = False) -> dict:
    """
    给卷内某道题生成「讲解思维导图」。
    返回 {'ok','svg','spec','error','from_cache','fallback'}
    已生成过的直接复用, 不重复花 AI 调用。
    """
    import diagram
    row = get_item(item_id)
    if not row:
        return {'ok': False, 'svg': '', 'spec': {}, 'error': '题目不存在', 'from_cache': False}

    if not force:
        hit = _cached_spec(row)
        if hit:
            spec, fb = hit
            return {'ok': True, 'svg': diagram.render_svg(spec), 'spec': spec,
                    'error': '', 'from_cache': True, 'fallback': fb}

    res = diagram.build_spec(row.get('stem') or '', row.get('answer') or '',
                             row.get('explain') or '', row.get('subject') or '数学',
                             row.get('user_answer') or '')
    fallback, err = False, ''
    if res['ok']:
        spec = res['spec']
    else:
        # AI 不可用/输出不合格 -> 本地按解析拆步骤, 不能让孩子看到空白
        spec = diagram.build_fallback_spec(row.get('stem') or '', row.get('answer') or '',
                                          row.get('explain') or '', row.get('topic') or '',
                                          row.get('user_answer') or '')
        fallback, err = True, res.get('error', '')
        if not spec:
            return {'ok': False, 'svg': '', 'spec': {}, 'error': err or '没有可用的解析内容',
                    'from_cache': False}

    payload = json.dumps({'spec': spec, 'fallback': fallback, 'error': err,
                          'v': diagram.SPEC_VER}, ensure_ascii=False)
    with _conn() as conn:
        conn.execute('UPDATE quiz_items SET diagram_json=? WHERE id=?', (payload, item_id))
    return {'ok': True, 'svg': diagram.render_svg(spec), 'spec': spec,
            'error': err, 'from_cache': False, 'fallback': fallback}


def _gen_one_offline(item: dict) -> dict:
    """只调 AI 拿结构, 不碰数据库(并发安全)。失败则本地降级。"""
    import diagram
    res = diagram.build_spec(item.get('stem') or '', item.get('answer') or '',
                             item.get('explain') or '', item.get('subject') or '数学',
                             item.get('user_answer') or '')
    if res['ok']:
        return {'id': item['id'], 'spec': res['spec'], 'fallback': False, 'error': ''}
    spec = diagram.build_fallback_spec(item.get('stem') or '', item.get('answer') or '',
                                       item.get('explain') or '', item.get('topic') or '',
                                       item.get('user_answer') or '')
    return {'id': item['id'], 'spec': spec, 'fallback': True,
            'error': res.get('error', 'AI 未返回可用结构')}


def gen_diagrams_for_paper(pid: int, max_n: int = MAX_DIAGRAMS_PER_PAPER,
                           workers: int = 4, on_done=None) -> dict:
    """
    判卷后给错题批量画图。
    并发只发生在 AI 调用上, 写库统一回到当前线程 —— SQLite 一条连接不能跨线程写。
    返回 {'ok','failed','skipped','errors':[...]}
    """
    import diagram
    data = get_paper(pid)
    wrong = [it for it in data.get('items', []) if it.get('correct') != 1]
    todo, skipped = [], 0
    for it in wrong:
        if _cached_spec(it):
            skipped += 1
        else:
            todo.append(it)
    over = max(0, len(todo) - max_n)      # 超出一轮上限的, 留着让用户单独点
    todo = todo[:max_n]
    if not todo:
        return {'ok': 0, 'failed': 0, 'skipped': skipped + over, 'errors': []}

    results, errors = [], []
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(_gen_one_offline, it): it for it in todo}
            for fut in as_completed(futs):
                try:
                    results.append(fut.result())
                    if on_done:
                        on_done(len(results), len(todo))
                except Exception as e:
                    errors.append(f'{futs[fut].get("uid")}: {e}')
    except Exception as e:
        # 并发不可用时退化为串行, 不能因为一个线程池问题就完全不出图
        errors.append(f'并发出错, 改用串行: {e}')
        for it in todo:
            try:
                results.append(_gen_one_offline(it))
            except Exception as e2:
                errors.append(f'{it.get("uid")}: {e2}')

    with _conn() as conn:
        for r in results:
            if not r.get('spec'):
                continue
            payload = json.dumps({'spec': r['spec'], 'fallback': r['fallback'],
                                  'error': r.get('error', ''), 'v': diagram.SPEC_VER},
                                 ensure_ascii=False)
            conn.execute('UPDATE quiz_items SET diagram_json=? WHERE id=?', (payload, r['id']))

    ok = len([r for r in results if r.get('spec')])
    return {'ok': ok, 'failed': len(results) - ok + len(errors), 'skipped': skipped + over,
            'errors': errors[:5]}


def attach_diagram_to_mistake(mistake_id: int, item_id: int) -> bool:
    """把已画好的图挂到错题本记录上"""
    row = get_item(item_id)
    raw = row.get('diagram_json')
    if not raw:
        return False
    with db.get_conn() as conn:
        conn.execute('UPDATE mistakes SET diagram_json=? WHERE id=?', (raw, mistake_id))
    return True


def sync_diagrams_to_mistakes(pairs) -> int:
    """判卷后: 把刚画好的图同步到对应的错题本记录, 免得错题本里还是没图"""
    n = 0
    for mid, iid in (pairs or []):
        try:
            if attach_diagram_to_mistake(mid, iid):
                n += 1
        except Exception:
            continue
    return n


def render_mistake_diagram(mistake: dict) -> str:
    """错题本里的记录 -> SVG。没图就返回空串(调用方负责显示按钮)"""
    import diagram
    raw = (mistake or {}).get('diagram_json')
    if not raw:
        return ''
    try:
        d = json.loads(raw)
        spec = d.get('spec') if isinstance(d, dict) else None
        return diagram.render_svg(spec) if spec else ''
    except Exception:
        return ''


def gen_diagram_for_mistake(mistake_id: int, force: bool = False) -> dict:
    """
    给错题本里的一条记录画讲解图。
    拍照录入的错题没有 quiz_items 记录, 只能按 mistakes 自己的字段生成。
    """
    import diagram
    with db.get_conn() as conn:
        row = conn.execute('SELECT * FROM mistakes WHERE id=?', (mistake_id,)).fetchone()
    if not row:
        return {'ok': False, 'svg': '', 'spec': {}, 'error': '错题不存在'}
    m = dict(row)
    # 老版本图(没有错点定位)不算缓存, 直接重画
    if not force and m.get('diagram_json') and not diagram_is_old(m):
        svg = render_mistake_diagram(m)
        if svg:
            return {'ok': True, 'svg': svg, 'spec': {}, 'error': '', 'from_cache': True}

    res = diagram.build_spec(m.get('question') or '', m.get('correct_answer') or '',
                             m.get('explanation') or '', m.get('subject') or '数学',
                             m.get('answer') or '')
    fallback, err = False, ''
    if res['ok']:
        spec = res['spec']
    else:
        spec = diagram.build_fallback_spec(m.get('question') or '', m.get('correct_answer') or '',
                                           m.get('explanation') or '', m.get('title') or '',
                                           m.get('answer') or '')
        fallback, err = True, res.get('error', '')
        if not spec:
            return {'ok': False, 'svg': '', 'spec': {}, 'error': err or '这道题没有解析，画不了图'}
    payload = json.dumps({'spec': spec, 'fallback': fallback, 'error': err,
                          'v': diagram.SPEC_VER}, ensure_ascii=False)
    with db.get_conn() as conn:
        conn.execute('UPDATE mistakes SET diagram_json=? WHERE id=?', (payload, mistake_id))
    return {'ok': True, 'svg': diagram.render_svg(spec), 'spec': spec,
            'error': err, 'from_cache': False, 'fallback': fallback}


def list_papers(subject: str = None, limit: int = 20) -> list:
    with _conn() as conn:
        if subject:
            rows = conn.execute('SELECT * FROM quiz_papers WHERE subject=? ORDER BY id DESC LIMIT ?',
                                (subject, limit)).fetchall()
        else:
            rows = conn.execute('SELECT * FROM quiz_papers ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return [dict(r) for r in rows]


def wrong_summary(subject: str, days: int = 30) -> list:
    """错题归纳: 按知识点聚合"""
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec='seconds')
    with _conn() as conn:
        rows = conn.execute(
            "SELECT topic, chapter, COUNT(*) c FROM quiz_items i JOIN quiz_papers p ON p.id=i.paper_id "
            "WHERE i.subject=? AND i.correct=0 AND p.created_at>=? AND i.topic<>'' "
            "GROUP BY topic ORDER BY c DESC", (subject, since)).fetchall()
    return [dict(r) for r in rows]
