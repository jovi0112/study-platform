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


def phases_of(subject: str) -> list:
    cal = load_calendar()
    return (cal.get('subjects', {}).get(subject) or {}).get('phases', [])


def current_phase(subject: str, on: date = None) -> dict:
    """按日期定位进度段. 返回 {label, detail, topics, index, total, from, to} 或 None"""
    on = on or date.today()
    on = on + timedelta(days=get_offset(subject))
    phs = phases_of(subject)
    if not phs:
        return None
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
  "type":"choice" | "fill" | "answer",
  "stem":"题面",
  "options":{"A":"...","B":"...","C":"...","D":"..."},   // 仅 choice 需要
  "answer":"正确答案(choice 填 A/B/C/D; fill 填简洁结果)",
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
                          difficulty: int = 3) -> dict:
    """
    AI 出变式题, 含自动验算。
    返回 {'ok':bool, 'questions':[...], 'msg':str, 'rejected':int}
    """
    import llm
    if not llm.is_llm_enabled():
        return {'ok': False, 'questions': [], 'msg': 'AI 老师未配置, 无法生成变式题(可在家长端配置 Key)', 'rejected': 0}

    ver = {'数学': '人教版七年级上册', '英语': '北师大版七年级上册', '语文': '统编版七年级上册'}.get(subject, '')
    user = (f"科目: {subject}（教材 {ver}）\n章节: {chapter}\n知识点: {topic}\n"
            f"难度: {difficulty}/5\n数量: {n}\n"
            f"要求: 出 {n} 道考查「{topic}」的新题, 题型尽量多样(选择/填空), "
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
    ua = normalize(user_answer)
    if not ua:
        return False
    ans = normalize(q.get('answer'))
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
def pick_questions(subject: str, n: int = 10, on: date = None, use_ai: bool = True) -> dict:
    """
    按当前进度 + 掌握度组卷。
    返回 {'ok':bool, 'questions':[...], 'phase':..., 'msg':..., 'ai_added':int}
    """
    ph = current_phase(subject, on)
    if not ph:
        return {'ok': False, 'questions': [], 'msg': f'{subject} 的教学进度未配置', 'phase': None, 'ai_added': 0}
    topics = ph.get('topics', [])
    bank = all_bank(subject)

    # 1) 当前进度范围内的题
    in_scope = [q for q in bank if q['topic'] in topics or q['chapter'] in topics]
    # 2) 难度贴近掌握度
    def target_of(q):
        return topic_level(subject, q['topic'])
    scored = []
    for q in in_scope:
        tgt = target_of(q)
        scored.append((abs(q['difficulty'] - tgt), random.random(), q))
    scored.sort(key=lambda x: (x[0], x[1]))
    picked = [s[2] for s in scored[:n]]

    # 3) 复习: 上次做错的知识点优先插一道
    # 上次做错的知识点优先插一道(哪怕已不在当前进度里, 复习是应该的)
    for t in recent_wrong_topics(subject, limit=3):
        cand = [q for q in bank if q['topic'] == t and q['uid'] not in {p['uid'] for p in picked}]
        if cand:
            picked.insert(0, random.choice(cand))
            break
    picked = picked[:n]

    ai_added = 0
    msg = f"从题库选出 {len(picked)} 道"
    # 4) 不够就用 AI 补, 补的题要过验算
    if len(picked) < n:
        if not use_ai:
            msg += f"（题库只有 {len(picked)} 道, 未开启 AI 补充）"
        else:
            need = n - len(picked)
            import llm
            if not llm.is_llm_enabled():
                msg += f"（题库不足, 还差 {need} 道, 但 AI 未配置）"
            else:
                gen = {'msg': '当前进度没有可用知识点'}
                cand_topics = topics or [ph.get('label', '') or subject]
                # 多试几个知识点、每次多要几道, 提高凑齐概率(A出题常被验算淘汰)
                for t in random.sample(cand_topics, min(len(cand_topics), 5)):
                    if ai_added >= need:
                        break
                    gen = ai_generate_questions(subject, t, ph.get('label', ''), n=3,
                                                difficulty=topic_level(subject, t))
                    for q in gen.get('questions', []):
                        if ai_added >= need:
                            break
                        if all(normalize(q['stem']) != normalize(p['stem']) for p in picked):
                            save_ai_question(q)
                            picked.append(q)
                            ai_added += 1
                if ai_added:
                    msg += f"，AI 补充 {ai_added} 道（已过验算）"
                else:
                    msg += f"，AI 补充失败: {gen.get('msg', '未知原因')}"

    if not picked:
        return {'ok': False, 'questions': [], 'msg': f'没选出题来。{msg}', 'phase': ph, 'ai_added': 0}
    random.shuffle(picked)
    return {'ok': True, 'questions': picked, 'phase': ph, 'msg': msg, 'ai_added': ai_added}


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
    push_wrong_to_mistakes(pid, subject)
    return {'ok': True, 'right': right, 'total': total, 'score': score, 'pending_ai': pending_ai}


def push_wrong_to_mistakes(pid: int, subject: str) -> int:
    """判完把错题流进现有错题本, 复用讲解链路"""
    data = get_paper(pid)
    n = 0
    for it in data.get('items', []):
        if it['correct'] == 1:
            continue
        try:
            db.add_mistake(
                subject=subject,
                question=it['stem'],
                grade='初一',
                title=f"[{it['topic'] or it['chapter']}] {it['stem'][:28]}",
                answer=it['user_answer'] or '(未作答)',
                correct_answer=it['answer'],
                explanation=(it['explain'] or '') + (('\n\nAI 评语: ' + it['ai_comment']) if it.get('ai_comment') else ''),
                wrong_reason='每日测试',
                source='quiz',
            )
            n += 1
        except Exception:
            continue
    return n


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
