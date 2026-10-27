# -*- coding: utf-8 -*-
"""
验证「错点定位」: 图上要标出孩子错在哪一步 + AI 解释原因, 且三科(数学/英语/语文)都能出图。
用临时库, 不污染真实数据。
"""
import os
import sys
import json
import tempfile
import xml.etree.ElementTree as ET

_tmp = os.path.join(tempfile.gettempdir(), 'study_mistake_mark.db')
if os.path.exists(_tmp):
    os.remove(_tmp)
os.environ['STUDY_DB'] = _tmp

import db           # noqa: E402
db.DB_PATH = _tmp
db.init_db()

import diagram      # noqa: E402
import quiz         # noqa: E402

PASS, FAIL = [], []


def ck(cond, name, extra=''):
    (PASS if cond else FAIL).append(name + ((' | ' + str(extra)) if extra else ''))
    print(('  OK  ' if cond else ' FAIL ') + name + ((' | ' + str(extra)) if extra else ''))


print('=== 1. 三科 prompt 分科 ===')
for s in ('数学', '英语', '语文', '物理'):
    p = diagram._spec_system(s)
    ck(len(p) > 200 and 'mistake' in p, f'{s} 有专属规则',
       ('专属' if ('数轴' if s == '数学' else ('英语' if s == '英语' else ('文言' if s == '语文' else '其他')) in p) else '通用'))
ck('数轴' in diagram._spec_system('数学'), '数学规则含数轴要求')
ck('绝不填 points' in diagram._spec_system('英语'), '英语规则禁止数轴')
ck('绝不填 points' in diagram._spec_system('语文'), '语文规则禁止数轴')

print('=== 2. sanitize_spec 处理错点字段 ===')
sp = diagram.sanitize_spec({
    'layout': 'steps', 'title': '去括号',
    'steps': ['去括号', '合并同类项', '代入求值'],
    'answer': 'x = 3',
    'mistake': {'my': '5', 'at': '第2步：合并同类项', 'step': 2,
                'why': '把 -3x 和 +2x 合并成了 -5x，符号弄错了', 'fix': '应得 -x，再代入 x=3'},
})
ck(sp.get('mistake', {}).get('step') == 2, 'step 合法保留')
ck(sp['mistake']['why'].startswith('把 -3x'), 'why 保留')
ck(sp['v'] == diagram.SPEC_VER, 'spec 带版本号', sp.get('v'))

sp2 = diagram.sanitize_spec({
    'layout': 'steps', 'steps': ['a', 'b'],
    'mistake': {'my': 'x', 'step': 9},     # 越界
})
ck(sp2.get('mistake', {}).get('step') is None, 'step 越界被丢弃', sp2.get('mistake'))

sp3 = diagram.sanitize_spec({'layout': 'steps', 'steps': ['a'], 'mistake': '我就是一段字符串'})
ck(sp3.get('mistake') == {}, 'mistake 是字符串时不影响出图')

sp4 = diagram.sanitize_spec({'layout': 'steps', 'steps': ['a'], 'mistake': {}})
ck(sp4.get('mistake') == {}, 'mistake 为空不画卡片')

print('=== 3. 渲染: 步骤红标 + 底部错点卡片 ===')
svg = diagram.render_svg(sp)
try:
    ET.fromstring(svg)
    ck(True, 'SVG 是合法 XML')
except Exception as e:
    ck(False, 'SVG 是合法 XML', e)
ck('错在这一步' in svg, '步骤行有红标 ← 错在这一步')
ck('你错在这里' in svg, '底部有错点卡片')
ck('为什么错' in svg, '卡片含错因')
ck('应该这样想' in svg, '卡片含正确思路')
ck('你答的是' in svg, '卡片含孩子原答案')
vh = svg.split('viewBox="0 0 ')[1].split('"')[0].split()
ck(int(float(vh[1])) > 200, '画布高度合理', vh[1])

svg_none = diagram.render_svg(sp3)
ck('你错在这里' not in svg_none, '无错点时不画红卡片')

print('=== 4. 端到端: 三科真实出图(联网) ===')
CASES = [
    ('数学', '已知数轴上 A(a)、B(b)、C(c) 三点，且 a>b>c。下列结论：① a-b>0；② b-c>0；'
             '③ a-c>0；④ (a-b)(b-c)>0。其中正确的有几个？',
     'C', '由 a>b>c 得 a-b>0、b-c>0、a-c>0，且 (a-b)(b-c) 两个正数相乘大于 0，四个全对，共 4 个 → 选 C。',
     'A'),
    ('英语', 'My mother often ____ (go) to work by bus. 用所给词的适当形式填空。',
     'goes', '主语 My mother 是第三人称单数，often 表示一般现在时，动词 go 要加 -es 变 goes。',
     'go'),
    ('语文', '解释下列句中加点的词：兔走触株，折颈而死。（《守株待兔》）',
     '走：跑', '"走"在文言文中是"跑"的意思，不是今天的"走路"。兔子跑得快撞上树桩，折断脖子死了。',
     '走：走路'),
]

for subj, q, ans, exp, my in CASES:
    mid = db.add_mistake(subj, q, answer=my, correct_answer=ans, explanation=exp, source='typing')
    r = quiz.gen_diagram_for_mistake(mid)
    ok = r.get('ok')
    ck(bool(ok), f'[{subj}] 出图成功', r.get('error') if not ok else '')
    if not ok:
        continue
    svg = r['svg']
    try:
        ET.fromstring(svg)
        ck(True, f'[{subj}] SVG 合法')
    except Exception as e:
        ck(False, f'[{subj}] SVG 合法', e)
    ck('你错在这里' in svg, f'[{subj}] 图上标出错点', '' if '你错在这里' in svg else svg[:120])
    ck('为什么错' in svg, f'[{subj}] 图上有错因解释')
    spec = r.get('spec') or {}
    ck(bool((spec.get('mistake') or {}).get('why')), f'[{subj}] AI 给了错因',
       (spec.get('mistake') or {}).get('why'))
    ck((spec.get('mistake') or {}).get('my') == my, f'[{subj}] 卡片里是孩子原答案',
       (spec.get('mistake') or {}).get('my'))
    ck(not quiz.diagram_is_old({'diagram_json': json.dumps({'spec': spec, 'v': diagram.SPEC_VER})}),
       f'[{subj}] 新图不算旧版')
    if subj == '数学':
        print('     数学 layout =', spec.get('layout'), '| cases =', len(spec.get('cases') or []),
              '| verdicts =', len(spec.get('verdicts') or []))
    else:
        ck(not (spec.get('cases') and any(c.get('points') for c in spec.get('cases'))),
           f'[{subj}] 没有乱画数轴')
        print(f'     {subj} layout =', spec.get('layout'),
              '| verdicts =', len(spec.get('verdicts') or []),
              '| steps =', len(spec.get('steps') or []))

print('=== 5. 旧图会被判为过期 ===')
ck(quiz.diagram_is_old({'diagram_json': json.dumps({'spec': {'layout': 'steps'}, 'v': 1})}),
   'v=1 的图判为旧版')
ck(quiz.diagram_is_old({}), '没有 diagram_json 判为旧版')
ck(not quiz.diagram_is_old({'diagram_json': json.dumps({'spec': {'layout': 'steps'},
                                                       'v': diagram.SPEC_VER})}),
   '当前版本不算旧版')

print('=== 6. 降级路径也带错点 ===')
fb = diagram.build_fallback_spec('题目', '正确答案', '第一步这么做。第二步那么做。', 'topic', '我的答案')
ck(bool((fb.get('mistake') or {}).get('my')), '降级图也标出孩子答案')
svg_fb = diagram.render_svg(fb)
ck('你错在这里' in svg_fb, '降级图有错点卡片')

print('\n===== 汇总: 通过 %d / 失败 %d =====' % (len(PASS), len(FAIL)))
for f in FAIL:
    print('  ✗', f)
sys.exit(1 if FAIL else 0)
