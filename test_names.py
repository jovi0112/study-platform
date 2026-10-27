"""
静态自检: 找出「用了但没导入」的全局名字。

来由 —— app.py 里 15 处 `json.loads(...)` 却从来没写 `import json`,
每处都被 `except Exception: _opts = None` 悄悄吞掉, 表现是选择题的选项
全部退化成输入框, 界面上一点报错都没有。这种"静默降级"比直接崩掉更难查。

这里用 AST 把每个模块的全局名字引用扫一遍, 发现未定义的立刻报错。
用法: python test_names.py
"""
import ast
import builtins
import os
import sys

FILES = ['app.py', 'db.py', 'quiz.py', 'diagram.py', 'llm.py', 'ocr.py',
         'backup.py', 'web_search.py']

# 运行时由 Streamlit / 宿主注入的名字, 静态看不出来
ALLOW = {'st', '__file__', '__name__', '__doc__', 'self', 'cls'}


def defined_names(tree):
    """模块级 + 函数级所有被赋值的名字 / 导入名 / 形参名"""
    out = set(dir(builtins)) | set(ALLOW)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.asname or a.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                out.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            out.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
        elif isinstance(node, ast.Global):
            out.update(node.names)
    return out


def used_names(tree):
    """所有被读取的名字, 带行号"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            out.append((node.id, node.lineno))
    return out


def check(path):
    src = open(path, encoding='utf-8').read()
    tree = ast.parse(src, filename=path)
    have = defined_names(tree)
    bad = []
    for name, ln in used_names(tree):
        if name not in have:
            bad.append((name, ln))
    return bad


def main():
    fails = 0
    for f in FILES:
        if not os.path.exists(f):
            continue
        bad = check(f)
        if bad:
            fails += len(bad)
            print(f'[FAIL] {f}')
            for name, ln in bad:
                print(f'        第 {ln} 行用了 `{name}`, 但这个模块里没定义/没导入')
        else:
            print(f'[OK]   {f}')
    print()
    if fails:
        print(f'✗ 共 {fails} 处「用了没导入」—— 运行时会 NameError, 如果被 except 吞掉就是静默降级')
        return 1
    print('✓ 全部模块没有未定义名字')
    return 0


if __name__ == '__main__':
    sys.exit(main())
