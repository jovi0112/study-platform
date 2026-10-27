"""
选择题判分回归测试。

来由(2026-10-27 真实浏览器实测发现的两个真 bug):
1. app.py 里用了 15 处 json.xxx 却没 import json —— 每处的 `except Exception`
   把 NameError 悄悄吞掉, 结果界面上所有选择题都没了选项, 全退化成输入框。
2. 界面渲染的是整条选项文字("C. -2ab"), 而标准答案存的是字母("C"),
   直接字符串比较 -> 孩子选对了也判错, 错题本里重做同样永远判错。

这里把「选项文字/字母/内容」各种写法都钉死, 免得以后改判分又退回去。
用法: python test_choice_grade.py
"""
import json
import os
import shutil
import sys
import tempfile

TMP = os.path.join(tempfile.gettempdir(), 'study_choice_test')
if os.path.isdir(TMP):
    shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ['STUDY_DB'] = os.path.join(TMP, 'test.db')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db          # noqa: E402
import quiz        # noqa: E402

PASS = FAIL = 0


def ck(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f'  OK  {name} | {got}')
    else:
        FAIL += 1
        print(f'  XX  {name} | 得到 {got!r}, 期望 {want!r}')


OPTS = {'A': 'x + 1', 'B': '1/x', 'C': '-2ab', 'D': 'a - b'}
Q = {'answer': 'C', 'qtype': 'choice', 'options': OPTS, 'alt': []}


def part1_forms():
    print('== 1. 选择题: 各种作答写法都要判对 ==')
    for a in ['C', 'c', 'C. -2ab', 'C.-2ab', 'C、-2ab', 'C) -2ab', '(C)', '-2ab', 'c. -2ab']:
        ck(f'答 {a!r} 判对', quiz.is_correct(Q, a), True)
    for a in ['B', 'B. 1/x', 'D. a - b', 'C. a - b', '', 'E']:
        ck(f'答 {a!r} 判错', quiz.is_correct(Q, a), False)


def part2_letter():
    print('== 2. choice_letter: 只收「字母+分隔符」, 不误伤填空 ==')
    ck('C. -2ab -> C', quiz.choice_letter('C. -2ab'), 'C')
    ck('C.-2ab -> C', quiz.choice_letter('C.-2ab'), 'C')
    ck('D -> D', quiz.choice_letter('D'), 'D')
    ck('x = 3 原样', quiz.choice_letter('x = 3'), 'x = 3')
    ck('17 原样', quiz.choice_letter('17'), '17')
    ck('AB. yy 原样(多字母不截)', quiz.choice_letter('AB. yy'), 'AB. yy')


def part3_retry():
    print('== 3. 错题本重做: 选对就归「已掌握」 ==')
    db.init_db()
    quiz.init_quiz_db()
    mid = db.add_mistake(
        subject='数学', question='下列各式中，是单项式的是（  ）', answer='(未作答)',
        correct_answer='C', wrong_reason='时间到未作答',
        explanation='单项式是数字与字母的乘积', source='quiz',
    )
    db.update_mistake(mid, qtype='choice', options_json=json.dumps(OPTS, ensure_ascii=False),
                      miss_reason='timeout', uid='t001')
    m = db.get_mistake(mid)
    ck('错题带上了题型', m.get('qtype'), 'choice')

    # 孩子点的是界面上那条 "C. -2ab"
    r = quiz.retry_mistake(mid, 'C. -2ab')
    ck('选 C. -2ab 判对', r.get('correct'), True)
    ck('归到已掌握', db.get_mistake(mid)['mastered'], 1)
    ck('留了重做记录', len(db.list_redos(mid, limit=5)), 1)
    ck('记录里存的是干净字母', db.list_redos(mid, limit=1)[0]['answer'], 'C')

    mid2 = db.add_mistake(
        subject='数学', question='下列各式中，是单项式的是（  ）', answer='(未作答)',
        correct_answer='C', wrong_reason='时间到未作答', explanation='', source='quiz',
    )
    db.update_mistake(mid2, qtype='choice', options_json=json.dumps(OPTS, ensure_ascii=False))
    r2 = quiz.retry_mistake(mid2, 'B. 1/x')
    ck('选错判错', r2.get('correct'), False)
    ck('仍留在错题本(未掌握)', db.get_mistake(mid2)['mastered'], 0)
    ck('错因改成重做仍错', db.get_mistake(mid2)['wrong_reason'], '重做仍错')
    ck('讲解图作废重画', db.get_mistake(mid2)['diagram_json'], None)


def part4_multi():
    print('== 4. 多选题不受影响 ==')
    qm = {'answer': 'BC', 'qtype': 'multi', 'options': OPTS, 'alt': []}
    ck('BC 判对', quiz.is_correct(qm, 'BC'), True)
    ck('CB 也判对', quiz.is_correct(qm, 'CB'), True)
    ck('只选 B 判错', quiz.is_correct(qm, 'B'), False)
    ck('多选 BCD 判错', quiz.is_correct(qm, 'BCD'), False)


def part5_modules():
    print('== 5. app.py 能正常 import(缺 import json 会在这里就炸) ==')
    import ast
    src = open('app.py', encoding='utf-8').read()
    tree = ast.parse(src)
    imports = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imports.update(a.name.split('.')[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            imports.update(a.name for a in n.names)
    ck('app.py 导入了 json', 'json' in imports, True)


if __name__ == '__main__':
    for fn in (part1_forms, part2_letter, part3_retry, part4_multi, part5_modules):
        fn()
    print()
    print(f'===== 通过 {PASS} / 失败 {FAIL} =====')
    sys.exit(1 if FAIL else 0)
