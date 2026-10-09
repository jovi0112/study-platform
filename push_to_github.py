"""一键把 study-platform 推送到 GitHub (供 Streamlit Cloud 部署)

用法:
  1) 在 GitHub 网页上新建一个空仓库, 名字叫 study-platform (选 Public, 不要勾 README)
  2) 跑这个脚本:
     python push_to_github.py
  3) 按提示输入 GitHub 用户名 (没装 git 会自动跳到下载提示)

如果不想用脚本, 参考 README 手动跑 git 命令也行
"""
import os
import sys
import subprocess

APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)


def run(cmd: str, check: bool = True) -> int:
    print(f'  > {cmd}')
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.stdout.strip():
        print(r.stdout.strip())
    if r.returncode != 0 and check:
        if r.stderr.strip():
            print(r.stderr.strip())
        print(f'  [失败] 命令返回码 {r.returncode}')
        sys.exit(1)
    return r.returncode


def check_git():
    if subprocess.run('git --version', shell=True, capture_output=True).returncode != 0:
        print('  [错误] 未检测到 git')
        print('  下载: https://git-scm.com/download/win')
        print('  安装完重启命令行, 再跑这个脚本')
        sys.exit(1)


def main():
    print('=' * 60)
    print('  学习平台 - 一键推送到 GitHub')
    print('=' * 60)
    print()

    # 1. 确认 git 已装
    check_git()

    # 2. 问 GitHub 用户名
    username = input('  你的 GitHub 用户名 (回车确认): ').strip()
    if not username:
        print('  [取消] 用户名不能为空')
        return
    repo = 'study-platform'

    # 3. 检查是否已是 git 仓库
    is_repo = os.path.exists('.git')
    if not is_repo:
        print()
        print('[1/5] 初始化 git 仓库...')
        run('git init')
        run('git config user.email "study-platform@local"')
        run('git config user.name "Study Platform User"')
    else:
        print('[1/5] 已是 git 仓库, 跳过 init')

    # 4. 添加文件
    print()
    print('[2/5] 添加文件...')
    run('git add .')

    # 5. 提交
    print()
    print('[3/5] 提交 (commit)...')
    run('git commit -m "deploy: learning platform v0.1" || echo "  无新文件改动"')

    # 6. 设置远程
    print()
    print('[4/5] 设置远程仓库...')
    remote_url = f'https://github.com/{username}/{repo}.git'
    # 删除已有 origin, 重新加
    subprocess.run('git remote remove origin', shell=True, capture_output=True)
    run(f'git remote add origin {remote_url}')
    print(f'  remote: {remote_url}')

    # 7. 推送
    print()
    print('[5/5] 推送到 GitHub...')
    print('  (弹出窗口时输入 GitHub 用户名 + Personal Access Token)')
    print('  (没 PAT 的话, GitHub 右上头像 -> Settings -> Developer settings ->')
    print('   Personal access tokens -> Tokens (classic) -> Generate new token,')
    print('   勾选 repo 权限, 生成后复制 token 作为密码用)')
    print()
    r = subprocess.run('git branch -M main && git push -u origin main',
                       shell=True, capture_output=False)
    if r.returncode != 0:
        print()
        print('  [推送失败] 常见原因:')
        print('  1) GitHub 上还没建空仓库, 或仓库名不一致')
        print('  2) 仓库不是 Public (Streamlit Cloud 免费版需要 Public)')
        print('  3) 没用 Personal Access Token 登录 (2021 年起 GitHub 禁用密码登录)')
        return

    print()
    print('=' * 60)
    print('  推送成功!')
    print()
    print(f'  你的 GitHub 仓库: https://github.com/{username}/{repo}')
    print()
    print('  下一步: 打开 https://share.streamlit.io')
    print('  1. 用 GitHub 登录')
    print('  2. 点 "New app"')
    print(f'  3. Repository 选 "{username}/{repo}"')
    print('  4. Branch: main, Main file path: app.py')
    print('  5. 点 Deploy!, 2-3 分钟拿到公网 URL')
    print('=' * 60)


if __name__ == '__main__':
    main()
