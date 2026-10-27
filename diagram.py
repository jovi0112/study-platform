# -*- coding: utf-8 -*-
"""
错题「思维导图」生成器。

分工:
  AI 只负责产出结构化数据 (解题主线 / 已知条件 / 关键判断 / 分情况 / 逐条结论);
  图由本地固定模板渲染成 SVG, 不让 AI 写任意代码。

这样做的原因:
  1. 渲染可预期 —— 模板固定, 不会出现 AI 画的框叠在一起、字跑出框
  2. 可离线降级 —— AI 不可用时, 用题干+解析本地拼一张「解题步骤图」, 而不是没有图
  3. 无渲染风险 —— 不让 AI 输出 HTML/SVG 直插页面
"""
import json
import re
from html import escape

# ============== 视觉常量 (对齐家长提供的样图风格) ==============
C_TEXT = '#2F3B48'
C_TEXT_DIM = '#5A6876'
C_BOX_FILL = '#F0F3F7'
C_BOX_STROKE = '#C9D4E0'
C_KEY_FILL = '#DCEAF7'
C_KEY_STROKE = '#7FAFD9'
C_LINE = '#9AAFC2'
C_AXIS = '#3C4B5A'
C_AB = '#2E6BA8'
C_BC = '#2A9D9A'
C_OK_FILL = '#E7F5EC'
C_OK_STROKE = '#8CC9A3'
C_BAD_FILL = '#FBEAE7'
C_BAD_STROKE = '#DFA398'
C_SUM_FILL = '#DCEAF7'
FONT = "'Microsoft YaHei','PingFang SC','Noto Sans SC',sans-serif"

W = 800
PAD = 30

# 结构版本: 老版本的图会被判为过期并重画, 免得孩子看到一张缺东西的旧图
# v1 无错点定位 / v2 有错点定位 / v3 增加"评分要点逐条命中"清单(阅读、简答靠它看清漏了哪条)
SPEC_VER = 3


# ============== 文字宽度估算与折行 ==============
def _w(ch: str) -> float:
    """单字符显示宽度(以中文字宽为 1)"""
    if '\u4e00' <= ch <= '\u9fff' or ch in '，。；：（）、“”‘’！？':
        return 1.0
    if ch in 'il|.,:;!()[]\' ':
        return 0.32
    if ch.isupper() or ch in 'mwMW@':
        return 0.72
    return 0.56


def text_width(s: str, size: float) -> float:
    return sum(_w(c) for c in str(s)) * size


def wrap(text: str, max_px: float, size: float, max_lines: int = 4) -> list:
    """按像素宽度折行, 超出 max_lines 的行末加省略号"""
    text = str(text or '').strip()
    if not text:
        return []
    limit = max_px / size          # 折成多少个"中文字宽"
    lines, cur, acc = [], '', 0.0
    for ch in text:
        cw = _w(ch)
        if acc + cw > limit and cur:
            lines.append(cur)
            cur, acc = ch, cw
        else:
            cur += ch
            acc += cw
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:-1] + '…'
    return lines


def fit(text: str, max_px: float, size: float) -> str:
    """单行, 超长截断"""
    text = str(text or '')
    if text_width(text, size) <= max_px:
        return text
    out = ''
    for ch in text:
        if text_width(out + ch + '…', size) > max_px:
            return out + '…'
        out += ch
    return out


# ============== SVG 基元 ==============
def _rect(x, y, w, h, fill, stroke, rx=8, sw=1.2, op=1.0):
    return (f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" '
            f'fill="{fill}" fill-opacity="{op}" stroke="{stroke}" stroke-width="{sw}"/>')


def _text(x, y, s, size=13, color=C_TEXT, anchor='start', weight='normal'):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" '
            f'fill="{color}" text-anchor="{anchor}" font-weight="{weight}">{escape(str(s))}</text>')


def _arrow(x1, y1, x2, y2, color=C_LINE):
    """水平箭头"""
    head = 5.5
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2 - head:.1f}" y2="{y2:.1f}" '
            f'stroke="{color}" stroke-width="1.4"/>'
            f'<path d="M{x2 - head:.1f},{y1 - 3.6:.1f} L{x2:.1f},{y2:.1f} L{x2 - head:.1f},{y1 + 3.6:.1f} Z" '
            f'fill="{color}"/>')


def _check(x, y, size=13, ok=True):
    color = '#2E8B57' if ok else '#C0504D'
    if ok:
        return (f'<path d="M{x:.1f},{y + size * 0.52:.1f} l{size * 0.3:.1f},{size * 0.34:.1f} '
                f'l{size * 0.54:.1f},{-size * 0.72:.1f}" fill="none" stroke="{color}" '
                f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>')
    return (f'<path d="M{x:.1f},{y + size * 0.2:.1f} l{size * 0.7:.1f},{size * 0.7:.1f} '
            f'M{x + size * 0.7:.1f},{y + size * 0.2:.1f} l{-size * 0.7:.1f},{size * 0.7:.1f}" '
            f'fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round"/>')


def _num(v, default=None):
    try:
        f = float(v)
        if f != f or f in (float('inf'), float('-inf')):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _fmt_num(x: float) -> str:
    return str(int(x)) if abs(x - round(x)) < 1e-9 else f'{x:g}'


# ============== spec 清洗: AI 输出不可信, 一律过一遍 ==============
def sanitize_spec(raw, fallback_title='') -> dict:
    if not isinstance(raw, dict):
        return {}
    layout = str(raw.get('layout') or '').strip()
    if layout not in ('case_split', 'steps', 'verdict_list'):
        layout = 'verdict_list' if raw.get('verdicts') else ('case_split' if raw.get('cases') else 'steps')

    def _strs(v, n, maxpx):
        out = []
        for x in (v or [])[:n]:
            s = str(x or '').strip()
            if s:
                out.append(fit(s, maxpx, 11.5))
        return out

    cases = []
    for c in (raw.get('cases') or [])[:3]:
        if not isinstance(c, dict):
            continue
        pts = []
        for p in (c.get('points') or [])[:4]:
            if not isinstance(p, dict):
                continue
            x = _num(p.get('x'))
            if x is None:
                continue
            name = str(p.get('name') or '').strip()[:6]
            if name:
                pts.append({'name': name, 'x': x})
        # 数轴至少要 2 个点才有意义
        if len(pts) < 2:
            pts = []
        segs = []
        names = {p['name'] for p in pts}
        for s in (c.get('segments') or [])[:3]:
            if not isinstance(s, dict):
                continue
            f, t = str(s.get('from') or '').strip(), str(s.get('to') or '').strip()
            if f in names and t in names and f != t:
                segs.append({'label': str(s.get('label') or f'{f}{t}').strip()[:10],
                             'from': f, 'to': t,
                             'style': 'ab' if str(s.get('color') or '').lower() in ('blue', 'ab') else 'bc'})
        cases.append({
            'label': str(c.get('label') or '').strip()[:24],
            'note': fit(str(c.get('note') or '').strip(), 700, 11),
            'points': pts,
            'segments': segs,
            'conclusion': fit(str(c.get('conclusion') or '').strip(), 700, 11.5),
        })

    verdicts = []
    for v in (raw.get('verdicts') or [])[:6]:
        if not isinstance(v, dict):
            continue
        txt = str(v.get('text') or '').strip()
        if not txt:
            continue
        verdicts.append({'no': str(v.get('no') or '').strip()[:3],
                         'text': fit(txt, 108, 11.5),
                         'ok': bool(v.get('ok'))})

    steps = []
    for s in (raw.get('steps') or [])[:5]:
        s = str(s or '').strip()
        # 模板左侧已经有序号, AI 又爱写"第2步：", 剥掉免得重复
        s = re.sub(r'^第\s*[0-9一二三四五六七八九]\s*步\s*[:：.、,，]?\s*', '', s).strip()
        if s:
            steps.append(fit(s, 240, 11.5))

    # --- 评分要点清单: 主观题(阅读/简答)逐条显示"这条你写了 / 这条漏了" ---
    # 命中与否是判卷时算出来的权威结果, 不让 AI 自己编, 所以这里只做清洗。
    checks = []
    for c in (raw.get('checks') or [])[:8]:
        if not isinstance(c, dict):
            continue
        t = str(c.get('text') or '').strip()
        if not t:
            continue
        checks.append({'no': str(c.get('no') or '').strip()[:3],
                       'text': fit(t, 620, 11.5),
                       'hit': bool(c.get('hit'))})

    # --- 错点定位: 孩子答错了才画红框; AI 有时塞字符串进来, 一律按 dict 处理 ---
    m_raw = raw.get('mistake')
    if not isinstance(m_raw, dict):
        m_raw = {}
    # 长度上限按"卡片实际可用宽度"给: 卡片宽 740-28, why/fix 可折到 3 行, 不要提前截成一句话
    my = fit(str(m_raw.get('my') or '').strip(), 640, 12)
    at = fit(str(m_raw.get('at') or m_raw.get('where') or '').strip(), 200, 12)
    why = fit(str(m_raw.get('why') or '').strip(), 660, 11.5)
    fix = fit(str(m_raw.get('fix') or '').strip(), 660, 11.5)
    st_i = _num(m_raw.get('step'))
    if st_i is not None:
        st_i = int(st_i)
        if not (1 <= st_i <= max(1, len(steps))):
            st_i = None
    mistake = {}
    if my or at or why:
        mistake = {'my': my, 'at': at, 'why': why, 'fix': fix, 'step': st_i}

    return {
        'layout': layout,
        'title': fit(str(raw.get('title') or fallback_title or '解题思路').strip(), 740, 15),
        'known': _strs(raw.get('known'), 3, 200),
        'keys': _strs(raw.get('keys'), 3, 200),
        'cases': cases,
        'verdicts': verdicts,
        'steps': steps,
        'checks': checks,
        'answer': fit(str(raw.get('answer') or '').strip(), 620, 13),
        'mistake': mistake,
        'v': SPEC_VER,
    }


def make_checks(points: list, hits: list) -> list:
    """评分点 + 判卷命中结果 -> 图上要点清单。判卷的 hits 是权威数据, 不让 AI 再猜一遍。"""
    out = []
    pts = points or []
    for i, p in enumerate(pts[:8]):
        t = str((p or {}).get('text') or '').strip()
        if not t:
            continue
        h = bool(hits[i]) if hits and i < len(hits) else False
        out.append({'no': str((p or {}).get('no') or '①②③④⑤⑥⑦⑧'[i] if i < 8 else str(i + 1)),
                    'text': fit(t, 620, 11.5), 'hit': h})
    return out


def missed_text(checks: list) -> str:
    miss = [c['no'] for c in (checks or []) if not c['hit']]
    return ('漏了要点 ' + ''.join(miss)) if miss else ''


# ============== 数轴渲染 ==============
def _render_axis(pts, segs, x0, y, width) -> str:
    """
    垂直布局(参考样图):
        y-11 / y-25  点名(坐标) —— 两点过近时上下错层, 避免叠字
        y            数轴
        y+9 / y+31   两条长度带
        y+23 / y+45  长度标签
    """
    xs = [p['x'] for p in pts]
    lo, hi = min(xs), max(xs)
    if hi - lo < 1e-9:
        return ''
    # 两端各留 40px: 端点标签(如 "C(-5)") 才不会跑出画布
    edge = 40
    inner_l, inner_r = x0 + edge, x0 + width - edge
    span = hi - lo
    def px(v):
        return inner_l + (v - lo) / span * (inner_r - inner_l)

    out = [f'<line x1="{x0:.1f}" y1="{y:.1f}" x2="{x0 + width:.1f}" y2="{y:.1f}" '
           f'stroke="{C_AXIS}" stroke-width="1.1"/>']
    rx_ = x0 + width
    out.append(f'<path d="M{rx_ - 5:.1f},{y - 3:.1f} L{rx_:.1f},{y:.1f} '
               f'L{rx_ - 5:.1f},{y + 3:.1f} Z" fill="{C_AXIS}"/>')

    # 零点: 只画淡竖线做参照, 不写文字(点的坐标已标出, 避免与标签抢位置)
    if lo < 0 < hi:
        zx = px(0)
        out.append(f'<line x1="{zx:.1f}" y1="{y - 8:.1f}" x2="{zx:.1f}" y2="{y + 8:.1f}" '
                   f'stroke="#C6D0DA" stroke-width="1.4" stroke-dasharray="3 3"/>')

    by_name = {p['name']: p for p in pts}
    # 两条长度带错开在轴下方, 标签各在带下方
    bands = [('ab', y + 9), ('bc', y + 31)]
    for style, yy in bands:
        for s in segs:
            if s['style'] != style:
                continue
            a, b = by_name[s['from']], by_name[s['to']]
            xa, xb = px(a['x']), px(b['x'])
            color = C_AB if style == 'ab' else C_BC
            l, r = min(xa, xb), max(xa, xb)
            out.append(f'<line x1="{l:.1f}" y1="{yy:.1f}" x2="{r:.1f}" y2="{yy:.1f}" '
                       f'stroke="{color}" stroke-width="2.4" stroke-linecap="round" opacity="0.9"/>')
            for xx in (l, r):
                out.append(f'<line x1="{xx:.1f}" y1="{yy - 4.5:.1f}" x2="{xx:.1f}" y2="{yy + 4.5:.1f}" '
                           f'stroke="{color}" stroke-width="1.6"/>')
            out.append(_text((l + r) / 2, yy + 14, s['label'], 10.5, color, 'middle', '600'))

    # 点 + 标签(坐标与名字合并, 过近则错层)
    last_x = -999.0
    layer = 0
    for p in sorted(pts, key=lambda z: z['x']):
        x = px(p['x'])
        if x - last_x < 52:
            layer = 1 - layer
        else:
            layer = 0
        last_x = x
        ly = y - 11 - layer * 14
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.4" fill="{C_AXIS}"/>')
        label = f"{p['name']}({_fmt_num(p['x'])})"
        out.append(_text(x, ly, label, 11, C_TEXT, 'middle', '600'))
    return ''.join(out)


# ============== 自动补"对照数轴" ==============
_POINTS_RE = re.compile(
    r'\(\s*a\s*,\s*b\s*,\s*c\s*\)\s*=\s*\(\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*\)')


def fill_compare_axis(spec: dict, question: str = '') -> dict:
    """
    AI 常只给"满足条件"的那种情形画数轴, 另一种只给一组数 —— 家长要的是能对照的图。
    若题目明确提到了数轴上的 A(a)/B(b)/C(c), 且这一种情形已写明 (a,b,c)=(x,y,z),
    就按这三个数补一条数轴和 AB/BC 长度, 让学生自己看出长短反过来了。
    只在至少已有一种情形画了数轴时才补, 避免把纯代数题硬画成数轴。
    """
    cases = (spec or {}).get('cases') or []
    q = question or ''
    if not cases or not any(c['points'] for c in cases):
        return spec
    if not (('数轴' in q) or ('A(a)' in q.replace(' ', '')) or ('AB' in q) or ('BC' in q)):
        return spec
    for c in cases:
        if c['points']:
            continue
        m = _POINTS_RE.search(c.get('note') or '')
        if not m:
            continue
        a, b, cc = (float(x) for x in m.groups())
        c['points'] = [{'name': 'C', 'x': cc}, {'name': 'B', 'x': b}, {'name': 'A', 'x': a}]
        c['segments'] = [
            {'label': f'AB = {_fmt_num(abs(a - b))}', 'from': 'B', 'to': 'A', 'style': 'ab'},
            {'label': f'BC = {_fmt_num(abs(b - cc))}', 'from': 'C', 'to': 'B', 'style': 'bc'},
        ]
        ab, bc = abs(a - b), abs(b - cc)
        rel = 'AB > BC' if ab > bc else ('AB < BC' if ab < bc else 'AB = BC')
        old = (c.get('conclusion') or '').strip()
        if not old:
            c['conclusion'] = f'{rel}（与另一种情形对照）'
        elif ('AB' not in old) and ('BC' not in old):
            # 原来只写了别的结论, 补上这条对照, 否则补出来的数轴没有落点
            c['conclusion'] = fit(f'{old}；对照 {rel}', 700, 11.5)
    return spec


# ============== 错点卡片(红色, 固定画在图最下方) ==============
def _render_mistake_card(mis: dict, x: float, y: float, width: float):
    """返回 (svg片段列表, 卡片高度)。没有内容则返回 ([], 0)。"""
    if not mis:
        return [], 0
    my = str(mis.get('my') or '').strip()
    at = str(mis.get('at') or '').strip()
    why = str(mis.get('why') or '').strip()
    fix = str(mis.get('fix') or '').strip()
    if not (my or at or why or fix):
        return [], 0

    inner = width - 28
    line_h = 16.5
    parts = []
    cy = y + 21                  # 当前行的基线(统一用基线推进, 别混用"高度累加", 否则必然叠字)

    # 第 1 行: 左边红字标题, 右边"你答的是 X"(放不下就另起一行)
    parts.append(_text(x + 14, cy, '❌ 你错在这里', 12.5, '#C0504D', 'start', '700'))
    if my:
        my_s = '你答的是：' + my
        room = inner - text_width('❌ 你错在这里', 12.5) - 20
        if text_width(my_s, 12) <= room:
            parts.append(_text(x + width - 14, cy, my_s, 12, '#8A5A4A', 'end', '600'))
        else:
            # 孩子的答案往往很长, 截成一句"…"等于没说 —— 折两行放完整
            for ln in wrap(my_s, inner, 12, 2):
                cy += 18
                parts.append(_text(x + 14, cy, ln, 12, '#8A5A4A', 'start', '600'))
    cy += 20

    if at:
        parts.append(_text(x + 14, cy, '📍 错在：' + at, 12, '#C0504D', 'start', '700'))
        cy += 18
    if why:
        for ln in wrap('为什么错：' + why, inner, 11.5, 3):
            parts.append(_text(x + 14, cy, ln, 11.5, C_TEXT))
            cy += line_h
    if fix:
        for ln in wrap('应该这样想：' + fix, inner, 11.5, 3):
            parts.append(_text(x + 14, cy, ln, 11.5, '#2E8B57', 'start', '600'))
            cy += line_h
    # 最后一行的基线已在循环里推进过, 退回一行再算卡片底
    h = (cy - line_h + 11) - y
    return parts, h


# ============== 主渲染 ==============
def render_svg(spec: dict, foot_note: str = '') -> str:
    """把 spec 渲染成 SVG 字符串。spec 需已过 sanitize_spec。"""
    if not spec:
        return ''
    parts, y = [], 0

    # --- 标题 ---
    y += 8
    if spec.get('title'):
        parts.append(_text(W / 2, y + 15, spec['title'], 15, C_TEXT, 'middle', '700'))
        y += 28

    # --- 顶部三框 ---
    known, keys = spec.get('known') or [], spec.get('keys') or []
    cases = spec.get('cases') or []
    verdicts = spec.get('verdicts') or []

    if known or keys:
        box_w, gap = 220, 40
        heads = ['已知条件', '先锁死关键', '']
        bodies = [known, keys, []]
        if cases:
            heads[2] = '只需分这几种情形'
            shorts = []
            for c in cases:
                lb = c['label']
                for sep in ('：', ':'):
                    if sep in lb:
                        lb = lb.split(sep, 1)[1]
                        break
                shorts.append(fit(lb.strip(), 190, 11.5))
            bodies[2] = shorts or [f'共 {len(cases)} 种']
        elif verdicts:
            heads[2] = '逐条判定'
            bodies[2] = [f'共 {len(verdicts)} 条结论']
        else:
            heads[2] = '按步骤推'
            bodies[2] = [f'共 {len(spec.get("steps") or [])} 步'] if spec.get('steps') else []

        # 估高
        line_h, head_h, pad_v = 15.5, 20, 11
        n_lines = max(len(b) for b in bodies) if any(bodies) else 1
        n_lines = max(n_lines, 1)
        box_h = head_h + n_lines * line_h + pad_v * 2

        xs = [PAD + i * (box_w + gap) for i in range(3)]
        for i in range(3):
            fill, stroke = (C_KEY_FILL, C_KEY_STROKE) if i == 1 else (C_BOX_FILL, C_BOX_STROKE)
            parts.append(_rect(xs[i], y, box_w, box_h, fill, stroke))
            parts.append(_text(xs[i] + box_w / 2, y + pad_v + 13, heads[i], 12.5,
                               C_TEXT_DIM if i != 1 else '#2A5D8F', 'middle', '600'))
            ty = y + pad_v + 13 + 17
            for ln in (bodies[i] or [])[:4]:
                for wl in (wrap(ln, box_w - 24, 11.5, 2) if text_width(ln, 11.5) > box_w - 24 else [ln])[:2]:
                    parts.append(_text(xs[i] + box_w / 2, ty, wl, 11.5, C_TEXT, 'middle'))
                    ty += line_h
            # 箭头
            if i < 2:
                ax1, ax2 = xs[i] + box_w + 6, xs[i + 1] - 6
                parts.append(_arrow(ax1, y + box_h / 2, ax2, y + box_h / 2))
        y += box_h + 18

    # --- 分情况: 每情况一条数轴 ---
    _mis_at = (spec.get('mistake') or {}).get('at') or ''

    def _case_is_bad(c):
        """错点在这一情形吗? (label 全文命中或"情形二"这类标记命中)"""
        lb = c.get('label') or ''
        if not lb or not _mis_at:
            return False
        if lb in _mis_at or _mis_at in lb:
            return True
        m = re.search(r'情形\s*[一二三四五1-5]', lb)
        return bool(m and m.group(0) in _mis_at)

    for idx, c in enumerate(cases):
        # 情形标题; 右侧说明按剩余宽度折行, 不够宽就另起一行(避免和标题叠字)
        _bad = _case_is_bad(c)
        parts.append(_text(PAD, y + 13, ('✗ ' if _bad else '') + c['label'], 12.5,
                           '#C0504D' if _bad else '#2A5D8F', 'start', '700'))
        if c['note']:
            room = W - PAD * 2 - text_width(c['label'], 12.5) - 16 - (28 if _bad else 0)
            if room >= text_width('(a,b,c)=(7,-2,-5)', 11):
                parts.append(_text(W - PAD, y + 13, c['note'], 11, C_TEXT_DIM, 'end'))
            else:
                y += 16
                parts.append(_text(PAD + 2, y + 13, c['note'], 11, C_TEXT_DIM, 'start'))
        y += 22
        # 数轴
        if c['points']:
            axis_y = y + 26
            parts.append(_render_axis(c['points'], c['segments'], PAD + 4, axis_y, W - PAD * 2 - 8))
            y = axis_y + (50 if c['segments'] else 14)
        else:
            # 没有可画的数轴(如"两点重合"只有一个点): 也要让出 note 那一行, 否则结论会压上去
            y += 6
        # 结论
        if c['conclusion']:
            parts.append(_text(W - PAD, y + 6, '→ ' + c['conclusion'], 11.5, C_AB, 'end', '600'))
            y += 22
        # 分隔线
        if idx < len(cases) - 1:
            parts.append(f'<line x1="{PAD}" y1="{y + 4:.1f}" x2="{W - PAD}" y2="{y + 4:.1f}" '
                         f'stroke="#E2E8EF" stroke-width="1" stroke-dasharray="4 4"/>')
            y += 16

    # --- 逐条结论 ---
    if verdicts:
        y += 6
        n = len(verdicts)
        per_row = n if n <= 5 else 3
        gap = 12
        vw = (W - PAD * 2 - gap * (per_row - 1)) / per_row
        vh = 48
        # 错点定位到某一条结论时(如 at="结论③"), 把那条标红: 只看底部卡片还不够直观
        _at = (spec.get('mistake') or {}).get('at') or ''
        _hits = [v['no'] for v in verdicts if v['no'] and v['no'] in _at]
        bad_vno = _hits[0] if len(_hits) == 1 else None
        for i, v in enumerate(verdicts):
            row, col = divmod(i, per_row)
            x = PAD + col * (vw + gap)
            yy = y + row * (vh + 10)
            is_bad = bool(bad_vno) and v['no'] == bad_vno
            fill, stroke = (C_OK_FILL, C_OK_STROKE) if v['ok'] else (C_BAD_FILL, C_BAD_STROKE)
            parts.append(_rect(x, yy, vw, vh, fill, stroke, rx=7))
            parts.append(_text(x + 10, yy + 17, (v['no'] + ' ' if v['no'] else '') + v['text'],
                               11.5, C_TEXT, 'start'))
            mark = '✓ 正确' if v['ok'] else '✗ 错误'
            if is_bad:
                # 再描一圈红边, 表示"孩子就是错在这一条"
                parts.append(f'<rect x="{x - 1.5:.1f}" y="{yy - 1.5:.1f}" width="{vw + 3:.1f}" '
                             f'height="{vh + 3:.1f}" rx="8" fill="none" stroke="#C0504D" '
                             f'stroke-width="2.2"/>')
                mark = '✗ 你错在这条'
            parts.append(_text(x + vw - 10, yy + 35, mark, 11.5,
                               '#2E8B57' if v['ok'] and not is_bad else '#C0504D', 'end', '600'))
        rows = (len(verdicts) + per_row - 1) // per_row
        y += rows * (vh + 10)

    # --- 步骤(无 cases/verdicts 时) ---
    if not cases and not verdicts and spec.get('steps'):
        parts.append(_text(PAD, y + 12, '解题步骤', 12, C_TEXT_DIM, 'start', '600'))
        y += 22
        bad_step = (spec.get('mistake') or {}).get('step')
        for i, s in enumerate(spec['steps']):
            is_bad = (bad_step == i + 1)
            bx, bw = PAD + 34, W - PAD * 2 - 34
            bh = 30
            fill, stroke = (C_BAD_FILL, C_BAD_STROKE) if is_bad else (C_BOX_FILL, C_BOX_STROKE)
            parts.append(_rect(bx, y, bw, bh, fill, stroke, rx=6, sw=1.6 if is_bad else 1.2))
            parts.append(f'<circle cx="{PAD + 16}" cy="{y + bh / 2:.1f}" r="11" '
                         f'fill="{"#F6DAD4" if is_bad else C_KEY_FILL}" '
                         f'stroke="{"#C0504D" if is_bad else C_KEY_STROKE}" stroke-width="1.2"/>')
            if is_bad:
                parts.append(_text(PAD + 16, y + bh / 2 + 4, '✗', 13, '#C0504D', 'middle', '700'))
            else:
                parts.append(_text(PAD + 16, y + bh / 2 + 4, str(i + 1), 11.5, '#2A5D8F', 'middle', '700'))
            parts.append(_text(bx + 12, y + bh / 2 + 4, s, 11.5, C_TEXT))
            if is_bad:
                # 步骤右侧打红标, 让孩子一眼看到错在第几步
                parts.append(_text(bx + bw - 12, y + bh / 2 + 4, '← 错在这一步', 11.5,
                                   '#C0504D', 'end', '700'))
            y += bh + 8
            if i < len(spec['steps']) - 1:
                parts.append(f'<line x1="{PAD + 16}" y1="{y - 8:.1f}" x2="{PAD + 16}" y2="{y + 1:.1f}" '
                             f'stroke="{C_KEY_STROKE}" stroke-width="1.4"/>')
        y += 4

    # --- 评分要点清单: 主观题逐条标"你写了 / 漏了" ---
    checks = spec.get('checks') or []
    if checks:
        y += 10
        got = sum(1 for c in checks if c['hit'])
        parts.append(_text(PAD, y + 13,
                           f'📋 评分要点（答到 {got}/{len(checks)} 条 · ✓ 你写到了 · ✗ 漏掉了）',
                           12.5, '#1F4B75', 'start', '700'))
        y += 26
        for c in checks:
            lines = wrap((c['no'] + ' ' + c['text']).strip(), W - PAD * 2 - 130, 11.5, 2)
            bh = 12 + 17 * len(lines)
            fill, stroke = (C_OK_FILL, C_OK_STROKE) if c['hit'] else (C_BAD_FILL, C_BAD_STROKE)
            parts.append(_rect(PAD, y, W - PAD * 2, bh, fill, stroke, rx=7,
                               sw=1.6 if not c['hit'] else 1.2))
            parts.append(_text(PAD + 14, y + 18, '✓' if c['hit'] else '✗', 13,
                               '#2E8B57' if c['hit'] else '#C0504D', 'start', '700'))
            for li, ln in enumerate(lines):
                parts.append(_text(PAD + 34, y + 18 + li * 17, ln, 11.5, C_TEXT))
            parts.append(_text(W - PAD - 14, y + 18, '你写到了' if c['hit'] else '这条漏了', 11.5,
                               '#2E8B57' if c['hit'] else '#C0504D', 'end', '700'))
            y += bh + 8

    # --- 汇总结论 ---
    if spec.get('answer'):
        y += 10
        label = '结论: ' + spec['answer']
        bw = min(W - PAD * 2, text_width(label, 13) + 56)
        bx = (W - bw) / 2
        parts.append(_rect(bx, y, bw, 38, C_SUM_FILL, C_KEY_STROKE, rx=9, sw=1.4))
        parts.append(_text(W / 2, y + 24, label, 13, '#1F4B75', 'middle', '700'))
        y += 38

    # --- 错点定位(红色卡片, 画在最下方, 让孩子一眼看到自己错在哪一步) ---
    _mp, _mh = _render_mistake_card(spec.get('mistake') or {}, PAD, y + 8, W - PAD * 2)
    if _mp:
        parts.append(_rect(PAD, y + 8, W - PAD * 2, _mh, C_BAD_FILL, C_BAD_STROKE, rx=9, sw=1.4))
        parts.extend(_mp)
        y += 8 + _mh

    if foot_note:
        y += 14
        parts.append(_text(W / 2, y, foot_note, 10.5, C_TEXT_DIM, 'middle'))
        y += 6

    H = y + PAD
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H:.0f}" '
            f'width="100%" style="max-width:{W}px;height:auto">'
            f'<rect x="0" y="0" width="{W}" height="{H:.0f}" fill="#FFFFFF"/>')
    return head + ''.join(parts) + '</svg>'


# ============== AI 产出结构 ==============
_SPEC_COMMON = """你是初中老师的板书设计师。给你一道题、孩子的答案、正确答案和解析，你要输出一张「讲解思维导图」的结构化数据。
你不画图，只输出 JSON；程序会用固定模板把 JSON 画成图。字段必须严格遵守。

输出格式：
{
  "layout": "case_split" | "steps" | "verdict_list",
  "title": "一句话点出这题的解法主线，不超过 18 字",
  "known": ["已知条件1", "已知条件2"],
  "keys": ["由已知推出的关键判断1"],
  "cases": [{"label":"情形一：b<0","note":"取哪组数","points":[],"segments":[],"conclusion":"这一情形的结论"}],
  "verdicts": [{"no":"①","text":"结论文本","ok":true}],
  "steps": ["第1步的文字", "第2步的文字"],
  "answer": "最终结论",
  "mistake": {
    "my": "孩子实际写下的答案原文",
    "at": "错在哪一步，如 第2步 / 情形二 / 结论③ / 审题",
    "step": 2,
    "why": "为什么会错，一句话不超过 30 字",
    "fix": "正确的想法应该是什么，一句话不超过 30 字"
  }
}

「mistake」是这张图最重要的部分 —— 图上会把孩子的错点单独标红，一定要认真填：
- my：原样抄孩子写的答案（没作答就写"没作答"）。
- at：定位到具体位置。layout 是 steps 时，必须同时给 step（整数，第几步，从 1 开始数）和 at（如"第2步：去括号"）。
  其他版式给 at 就行，如"结论③""情形二""选项 B"。
- why：说他这一步到底错在哪，是符号丢了、公式记错、还是概念没分清。要具体到这一步，不许写"粗心""没掌握"这种空话。
- fix：告诉他这一步应该怎么想，或正确结果是什么。
- 如果孩子的答案其实是对的（只是格式不同），why 写"答案正确，只是写法不同"，fix 写建议的规范写法。

通用硬性要求：
- known 必须是题干原意的直述，不要掺入推导结果。
- 每个文字字段尽量短：known/keys 每条不超过 18 字，conclusion 不超过 30 字，verdicts.text 不超过 12 字，steps 每步不超过 22 字。
- 全中文，用初中生能看懂的话，不要出现"显然""易得"这类词。
- 只输出 JSON，不要任何解释。"""

_SUBJ_RULE = {
    '数学': """本题科目：数学。按下面顺序判断 layout：
1. 题目里有 ① ② ③ 这类需要逐条判断真假的结论 —— verdicts 必须全部填上，ok 按真实对错填，不许全 true。
   如果这些结论里还需要分情况讨论（符号未定 / 位置未定 / 绝对值 / 动点），layout 用 "case_split"，
   并把 cases 也填上（这正是最有价值的部分）；否则 layout 用 "verdict_list"，cases 留空数组。
2. 只有一条解法路径的计算题、化简题、解方程题 —— layout 用 "steps"，steps 填 3~5 步，
   每步写清"做了什么 + 算出什么"。cases 和 verdicts 留空数组。
3. 需要按不同情况分别讨论，但不需要逐条判断结论 —— layout 用 "case_split"，verdicts 留空数组。

数轴题的硬性要求：
- 题目只要涉及数轴上的点（A、B、C 或 P、Q）的距离、线段长短比较，则**每一个情形都必须给出 points 和 segments**。
  不合格的反例：只给"满足条件"的情形画数轴，"不满足"的情形留空 —— 那样就看不出这个条件为什么必要。
  正确做法：两种情形都画出数轴，让学生自己看出长短关系反过来了。
- points 的 x 必须是你举的那组例子的真实数值，且与 note 里写的完全一致，不许编。
- segments 的 from/to 必须是 points 里出现过的 name。
- 数轴只在该情形需要比较线段长短（AB、BC 这类）时才填；纯代数计算、跟数轴无关的情形，points 和 segments 都留空数组。
- note 只写"这一情形取哪组数"，不要写结论判断；结论一律写进 conclusion。
- 数学题一律不要填数轴以外的图形；cases 最多 3 个，verdicts 最多 6 条，steps 最多 5 步。""",

    '英语': """本题科目：英语（七年级）。按下面顺序判断 layout：
1. 单选题 / 完形填空 / 词汇辨析 / 选词填空 —— layout 用 "verdict_list"，verdicts 逐条列出要判断的候选
   （四个选项就列四条：A.xxx / B.xxx / C.xxx / D.xxx，no 填 "A""B""C""D"），
   ok 只有正确答案那条为 true，其余为 false；每条 text 里写清楚为什么对/为什么错（如"缺谓语""时态不符"）。
   known 填题干的关键信息（如"主语是第三人称单数""yesterday 表过去"），keys 填判定规则（如"一般现在时，动词加 -s"）。
2. 句型转换 / 单句翻译 / 用所给词的适当形式填空 / 改写句子 —— layout 用 "steps"，steps 填 3~5 步：
   判时态语态 → 定句子结构 → 选词或变形 → 检查形式（单复数/大小写/标点）。每步写"做什么 + 得到什么"。
3. 阅读理解 / 任务型阅读 —— layout 用 "steps"，steps 按"读题干找关键词 → 回原文定位句子 → 比对选项 → 排除干扰"写，
   known 填题干问的是什么，keys 填原文依据（英文原文原句，可带中文翻译）。
4. 作文 / 表达类 —— layout 用 "steps"，steps 按"审题定人称时态 → 列要点 → 连词成句 → 检查语法"写。

硬性要求：
- 英语题**绝不填 points / segments**（那是数轴专用的），一律留空数组。
- 英文单词、短语、句子照原文写，不要翻译成中文；中文解释写在括号里。
- mistake.at 要定位到具体环节（如"时态判断""选项 B""第2步：定结构"），why 要说出具体语法点错在哪
  （如"把一般现在时当成一般过去时，动词多加了 -ed"），不许只写"语法没掌握"。""",

    '语文': """本题科目：语文（七年级，统编版）。按下面顺序判断 layout：
1. 文言文实词 / 虚词 / 句子翻译 / 断句 —— layout 用 "steps"，steps 按"看清句式 → 定重点词意思 → 补出省略成分 → 调整语序成现代汉语"写，
   known 填原句和出处，keys 填重点词解释（如"走：跑"）。
2. 病句修改 / 词语（成语）运用 / 标点 / 修辞判断 —— layout 用 "verdict_list"，
   verdicts 逐条列出可能的错误类型或选项（如"成分残缺""搭配不当""语序不当"或 A/B/C/D），ok 按真实对错填。
3. 古诗词鉴赏 / 默写 —— layout 用 "steps"，steps 按"看题目和作者 → 抓意象 → 体会情感 → 组织答题语言"写。
4. 现代文阅读（概括 / 含义理解 / 作用分析 / 人物形象）—— layout 用 "steps"，
   steps 按"审题明考点 → 回原文定位段落 → 摘关键词 → 按答题格式组织语言"写，
   known 填题目要求，keys 填这类题的答题格式（如"手法 + 内容 + 情感"）。
5. 文学常识 / 名著 —— layout 用 "verdict_list"，逐条判定。

硬性要求：
- 语文题**绝不填 points / segments**（那是数轴专用的），一律留空数组。
- steps 每步不超过 22 字，verdicts.text 不超过 12 字（比数学略宽，但别写成长句）。
- mistake.at 定位到具体环节（如"第2步：释词""选项 C""没答情感这一层"），
  why 要说出具体错在哪（如"把'走'按今义理解成走路，古义是跑"），不许只写"没读懂""基础不牢"。""",
}

_DEFAULT_RULE = """本题科目：其他。按下面顺序判断 layout：
1. 需要逐条判断真假/对错（选项、结论、说法）—— layout 用 "verdict_list"，verdicts 逐条列出，ok 按真实对错填。
2. 需要分情况讨论 —— layout 用 "case_split"，cases 最多 3 个。
3. 其余按步骤推进 —— layout 用 "steps"，steps 填 3~5 步，每步写"做什么 + 得到什么"。
不要填 points / segments（那是数学数轴专用的），一律留空数组。"""


def _spec_system(subject: str) -> str:
    rule = _SUBJ_RULE.get((subject or '').strip())
    return _SPEC_COMMON + '\n\n' + (rule or _DEFAULT_RULE)


def build_spec(question: str, answer: str, explain: str, subject: str = '数学',
               user_answer: str = '', points: list = None, hits: list = None,
               miss_reason: str = '') -> dict:
    """
    调用 AI 产出图解结构。返回 {'ok':bool, 'spec':dict, 'error':str}
    这是唯一需要联网的环节; 失败时调用方应回退到 build_fallback_spec()。
    user_answer = 孩子实际写的答案, 用于让 AI 定位错点(图上的红色错因框); 没有就传空串。
    points/hits = 主观题评分点和判卷命中情况: 命中结果来自判卷, 权威且不让 AI 编,
      图上会逐条列出"这条你写了 / 这条漏了", 这是孩子最需要看的部分。
    miss_reason = 'timeout' 表示这题是时间到没做完, 图上要照实说。
    """
    import llm
    if not llm.is_llm_enabled():
        return {'ok': False, 'spec': {}, 'error': 'AI 老师未配置，无法生成图解'}
    my = (user_answer or '').strip()
    checks = make_checks(points, hits)
    extra = ''
    if checks:
        miss = missed_text(checks)
        extra = ('\n\n评分要点（判卷已逐条核对，不要改判定结果）：\n'
                 + '\n'.join(('✓ 已答到 ' if c['hit'] else '✗ 漏掉 ') + c['no'] + ' ' + c['text']
                              for c in checks)
                 + ('\nmistake.why 要针对' + miss + '说：孩子为什么没答到这一层（角度没换 / 没回原文找依据 / 只答了内容没答情感），'
                    'fix 要给出这一条应该怎么答。' if miss else
                    '\n所有要点都答到了 —— mistake.why 写"要点都答到了"，fix 写还能怎么答得更完整。'))
    if miss_reason == 'timeout':
        extra += ('\n\n特别注意：这题是**考试时间到了没做完**，不是孩子不会。'
                  'mistake.at 写"时间到未作答"，why 写"时间不够，这题没写完"，'
                  'fix 写这道题最快的切入思路（先答哪一点），帮孩子下次抢时间。')
    user = ('科目：%s\n题目：%s\n\n孩子写的答案：%s\n正确答案：%s\n\n解析：%s%s\n\n'
            '请输出图解 JSON（重点填好 mistake 错点定位）。'
            % (subject, question, my or '（没作答）', answer,
               explain or '（无解析，请你自己推导一遍）', extra))
    raw = llm._call_chat(_spec_system(subject), user, json_mode=True)
    if not isinstance(raw, dict) or raw.get('error'):
        return {'ok': False, 'spec': {}, 'error': str((raw or {}).get('error') or 'AI 没有返回内容')}
    spec = sanitize_spec(raw, fallback_title='解题思路')
    if not spec or not (spec.get('cases') or spec.get('verdicts')
                        or spec.get('steps') or spec.get('answer')):
        return {'ok': False, 'spec': {}, 'error': 'AI 返回的内容不完整，没法画图'}
    if checks:
        spec['checks'] = checks
    if miss_reason == 'timeout':
        spec['mistake'] = dict(spec.get('mistake') or {},
                               my=my or '（没作答）', at='时间到未作答')
        if not (spec['mistake'].get('why')):
            spec['mistake']['why'] = '考试时间到了，这题没来得及做完'
    if my:
        spec.setdefault('mistake', {})
        if not spec['mistake'].get('my'):
            spec['mistake'] = dict(spec['mistake'], my=my)
    # AI 没说清错在哪、但我们判出漏了要点 -> 直接把漏的要点标上去, 不留空
    if checks and spec.get('mistake') and not spec['mistake'].get('at'):
        spec['mistake']['at'] = missed_text(checks) or spec['mistake'].get('at') or ''
    spec = fill_compare_axis(spec, question)
    return {'ok': True, 'spec': spec, 'error': ''}


def build_fallback_spec(question: str, answer: str, explain: str, topic: str = '',
                        user_answer: str = '', points: list = None, hits: list = None,
                        miss_reason: str = '') -> dict:
    """
    AI 不可用时的本地降级: 不联网, 把解析按句拆成步骤图。
    宁可给一张朴素的步骤图, 也不让孩子看到一个空白区域。
    降级图没有 AI 分析, 但仍要把孩子写的答案标出来(至少让他知道"我答的和正确答案不一样")。
    """
    steps = []
    for seg in re.split(r'(?<=[。；;！!])', (explain or '').strip()):
        seg = seg.strip(' 。；;')
        if len(seg) >= 6:
            steps.append(seg)
    steps = steps[:5]
    if not steps:
        return {}
    spec = sanitize_spec({
        'layout': 'steps',
        'title': (topic or '解题步骤'),
        'known': [fit(question, 200, 11.5)] if question else [],
        'keys': [],
        'steps': steps,
        'answer': str(answer or ''),
    })
    # 有评分点时, 哪怕 AI 不在也要把"哪条写了、哪条漏了"画出来 —— 这是孩子最需要的部分
    checks = make_checks(points, hits)
    if checks:
        spec['checks'] = checks
    my = (user_answer or '').strip()
    if miss_reason == 'timeout':
        spec['mistake'] = {'my': my or '（没作答）', 'at': '时间到未作答',
                           'step': None, 'why': '考试时间到了，这题没来得及做完',
                           'fix': '先看清题目问什么，挑最有把握的一点先写'}
    elif my:
        at = missed_text(checks) if checks else 'AI 未接入，无法定位具体哪一步'
        spec['mistake'] = {'my': my, 'at': at, 'step': None,
                           'why': ('对照上面的要点清单，找出自己漏的是哪一条'
                                   if checks else '对照下面的步骤，找出自己卡住的那一步'),
                           'fix': ''}
    return spec
