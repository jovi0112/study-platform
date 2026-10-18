"""
云端自动备份 —— 把整库同步到「你自己账号下的私有 Gist」

为什么需要这个模块
------------------
Streamlit Community Cloud 的文件系统是**临时存储**: 应用每次重启 / 每次重新部署
都会清空本地 SQLite 库, 官方不保留任何快照。也就是说孩子录的错题随时可能蒸发。

本模块做的事:
  启动时  —— 自动从 Gist 把「本地没有、备份里有」的数据找回来(合并, 不覆盖现有)
  录入后  —— 自动把整库推送到 Gist(节流 120 秒, 不会刷爆 API)

配置(Streamlit Cloud → 应用 → Settings → Secrets), 只需要一行:
  STUDY_BACKUP_TOKEN = "github 个人令牌(只需勾选 gist 权限)"
可选:
  STUDY_BACKUP_GIST_ID = ""   # 留空则自动查找/创建, 一般不用填

设计原则(踩过的坑都写在这)
--------------------------
1. **绝不拿空库覆盖有数据的备份**: 应用重启后本地库会被清空, 如果这时候自动 push,
   就会把备份也一起毁掉 —— 这是整个方案最致命的陷阱。所以 push 前会拿本地条数和
   远端条数对比, 本地为 0 而远端有货时直接拒绝推送并报警。
2. **不假成功**: 没配 token / 网络失败 / 权限不足, 全部如实报出原因, 不显示 ✅。
3. **Gist ID 自动管理**: 存三处(环境变量 → 数据库设置表 → 按 description 搜索),
   即使云端文件被清空也能自己找回来。
"""
import hashlib
import json
import math
import os
import time
from datetime import datetime

import requests

import db

# ============== 配置读取 ==============
_BACKUP_DESC_PREFIX = 'study-platform-backup'
_FILE_MAIN = 'study_backup.json'
_FILE_PART = 'study_backup_p{}.json'
_FILE_META = 'study_backup_meta.json'
_MAX_PART_BYTES = 700_000      # 单文件控制在 700KB 以内, 避免 Gist 截断(阈值 1MB)
# 不做时间节流。踩过的坑: 设成 30 秒 / 3 秒时, 孩子连着录的第 2、3 题会落在窗口内被
# 跳过, 之后没有别的操作就永远没进备份 —— 等于备份形同虚设。
# 防"无意义写入"完全交给下面的内容哈希比对: 数据没变就一个字节都不发给 GitHub,
# 数据变了就必须立刻写, 哪怕上一秒刚写过。
_MIN_PUSH_INTERVAL = 0


def _read_secret(key: str, default: str = '') -> str:
    """Secrets 优先, 环境变量兜底(本地调试用)"""
    val = default
    try:
        import streamlit as st
        if key in st.secrets:
            val = st.secrets[key]
    except Exception:
        pass
    if not val:
        val = os.environ.get(key, default)
    return (val or '').strip()


def _clean_secret(v: str) -> str:
    """清掉粘贴时常见的脏字符: 中英文引号 / 零宽字符 / 全角空格"""
    if not v:
        return ''
    s = v.strip()
    for ch in ('\u200b', '\ufeff', '\u3000', '\u00a0', '\u200c', '\u200d'):
        s = s.replace(ch, '')
    while len(s) >= 2 and ((s[0], s[-1]) in
                           (('"', '"'), ("'", "'"), ('\u201c', '\u201d'),
                            ('\u2018', '\u2019'), ('\u300c', '\u300d'),
                            ('\uff08', '\uff09'), ('\u300a', '\u300b'),
                            ('\u300e', '\u300f'))):
        s = s[1:-1].strip()
    return s.strip()


BACKUP_TOKEN = _clean_secret(_read_secret('STUDY_BACKUP_TOKEN', ''))
BACKUP_GIST_ID = _clean_secret(_read_secret('STUDY_BACKUP_GIST_ID', ''))

# ============== 进程内状态(只在本次运行有效, 不落盘) ==============
_state = {
    'gist_id': BACKUP_GIST_ID or None,
    'last_push_at': None,
    'last_push_ts': 0.0,
    'last_push_ok': None,
    'last_payload_fp': None,
    'last_push_error': None,
    'last_pull_at': None,
    'last_pull_restored': 0,
    'last_pull_error': None,
    'remote_counts': None,     # 上次从远端读到的条数, push 前的安全比对靠它
    'started': False,          # 启动时的拉回只做一次
}


def _now() -> str:
    return datetime.now().strftime('%m-%d %H:%M:%S')


def is_enabled() -> bool:
    return bool(BACKUP_TOKEN)


def status() -> dict:
    """给 UI 用的状态快照"""
    try:
        local_counts = db.count_all()
    except Exception as e:
        local_counts = {}
    return {
        'enabled': is_enabled(),
        'reason': '已启用' if is_enabled() else '未配置 STUDY_BACKUP_TOKEN',
        'gist_id': _state['gist_id'],
        'gist_url': (f"https://gist.github.com/{_state['gist_id']}"
                     if _state['gist_id'] else None),
        'last_push_at': _state['last_push_at'],
        'last_push_ok': _state['last_push_ok'],
        'last_push_error': _state['last_push_error'],
        'last_pull_at': _state['last_pull_at'],
        'last_pull_restored': _state['last_pull_restored'],
        'last_pull_error': _state['last_pull_error'],
        'remote_counts': _state['remote_counts'],
        'local_counts': local_counts,
    }


def _sess() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        'Authorization': f'Bearer {BACKUP_TOKEN}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'study-platform-backup',
    })
    return s


def _humanize_http(code: int, body: str) -> str:
    if code == 401:
        return '令牌无效或已过期(401) — 去 Secrets 换一个新的'
    if code == 403:
        if 'rate limit' in body.lower():
            return 'GitHub 接口限流(403) — 等一会儿会自动重试'
        return '令牌没有 gist 权限(403) — 生成令牌时记得勾选 gist'
    if code == 404:
        return '备份 Gist 找不到了(404) — 可能被删了, 下次备份会重新建一个'
    return f'GitHub 接口返回 {code}'


# ============== Gist 定位 ==============
def _remember_gist(gid: str):
    """把 gist id 记到数据库设置表(能留住就留住, 留不住还有 description 兜底)"""
    try:
        db.set_setting('backup_gist_id', gid)
    except Exception:
        pass


def _resolve_gist(sess) -> tuple:
    """确定用哪个 Gist: 环境变量 → 数据库设置 → 按描述搜索 → 新建。返回 (gist_id, error)"""
    if _state['gist_id']:
        return _state['gist_id'], None
    try:
        gid = db.get_setting('backup_gist_id')
    except Exception:
        gid = None
    if gid:
        r = sess.get(f'https://api.github.com/gists/{gid}', timeout=15)
        if r.ok:
            _state['gist_id'] = gid
            return gid, None
    # 搜索已有的备份 gist
    r = sess.get('https://api.github.com/gists', params={'per_page': 100}, timeout=20)
    if r.ok:
        for g in r.json():
            if str(g.get('description') or '').startswith(_BACKUP_DESC_PREFIX):
                gid = g['id']
                _state['gist_id'] = gid
                _remember_gist(gid)
                return gid, None
    # 没有就建一个
    r = sess.post('https://api.github.com/gists', timeout=20, json={
        'description': f'{_BACKUP_DESC_PREFIX} (自动创建, 请勿删除)',
        'public': False,
        'files': {_FILE_MAIN: {'content': '{"version":1,"tables":{}}'}},
    })
    if not r.ok:
        return None, _humanize_http(r.status_code, r.text[:200])
    gid = r.json()['id']
    _state['gist_id'] = gid
    _remember_gist(gid)
    return gid, None


# ============== 序列化 / 反序列化 ==============
def _split_payload(payload: dict) -> dict:
    """
    大备份切成分片文件, 避免单文件超过 Gist 截断阈值(1MB)。
    无论是否分片都写一个索引文件, 由它决定「这次备份到底有几个片」——
    这样从单片切到多片(或反过来)时, 不会误读上一次留下的旧文件。
    """
    text = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    raw_len = len(text.encode('utf-8'))
    # GitHub 要求每个文件是对象: {"content": "..."} —— 直接给字符串会 422
    if raw_len <= _MAX_PART_BYTES:
        return {
            _FILE_MAIN: {'content': text},
            _FILE_META: {'content': json.dumps(
                {'parts': 0, 'exported_at': payload.get('exported_at'),
                 'total_chars': len(text)}, ensure_ascii=False)},
        }
    n = math.ceil(raw_len / _MAX_PART_BYTES)
    per = math.ceil(len(text) / n)
    files = {}
    for i in range(n):
        files[_FILE_PART.format(i + 1)] = {'content': text[i * per:(i + 1) * per]}
    files[_FILE_META] = {'content': json.dumps({
        'parts': n,
        'exported_at': payload.get('exported_at'),
        'total_chars': len(text),
    }, ensure_ascii=False)}
    return files


def _fetch_files(sess, gid: str) -> tuple:
    """
    读取 Gist 内容。返回 ({文件名: 完整文本}, error)

    关键: 不能直接用 API 返回的 files[].content —— Gist 单次响应总内容超过约 1MB
    就会把后面的文件截断(truncated=true, 实测第 2 片只剩一半)。改用每个文件的
    raw_url 单独取, 拿到的永远是全量。
    """
    try:
        r = sess.get(f'https://api.github.com/gists/{gid}', timeout=20)
    except Exception as e:
        return None, f'网络异常: {e}'
    if not r.ok:
        return None, _humanize_http(r.status_code, r.text[:200])
    meta_files = r.json().get('files') or {}
    out = {}
    for name, info in meta_files.items():
        url = info.get('raw_url')
        if not url:
            continue
        try:
            rr = requests.get(url, timeout=30)
            if rr.ok:
                out[name] = rr.text
        except Exception:
            continue
    return out, None


def _join_files(files: dict) -> tuple:
    """把 {文件名: 文本} 拼回一个 payload。返回 (payload, error)"""
    if not files:
        return None, '备份里没有任何文件'
    # 索引文件说了算: parts=0 表示单片模式, >0 表示分片模式
    parts = 0
    if _FILE_META in files:
        try:
            parts = int(json.loads(files[_FILE_META]).get('parts', 0))
        except Exception:
            parts = 0
    if parts > 0:
        buf = []
        for i in range(1, parts + 1):
            name = _FILE_PART.format(i)
            if name not in files:
                return None, f'备份不完整(缺少第 {i}/{parts} 片)'
            buf.append(files[name])
        try:
            return json.loads(''.join(buf)), None
        except Exception as e:
            return None, f'分片拼接后解析失败: {e}'
    if _FILE_MAIN not in files:
        return None, '备份里没有数据文件'
    try:
        return json.loads(files[_FILE_MAIN]), None
    except Exception as e:
        return None, f'备份文件解析失败: {e}'


def _remote_counts(files: dict) -> dict:
    """只看备份里的条数, 不用完整入库"""
    try:
        payload, err = _join_files(files)
        if err:
            return {}
        return {t: len(v or []) for t, v in (payload.get('tables') or {}).items()}
    except Exception:
        return {}


# ============== 拉回(启动时找回丢失的数据) ==============
def pull(force: bool = False) -> dict:
    """
    从 Gist 把「本地没有」的数据找回来(合并导入, 不覆盖现有内容).
    返回 {'ok':bool, 'restored':int, 'error':str|None, 'skipped':bool}
    """
    if not is_enabled():
        return {'ok': False, 'restored': 0, 'error': '未配置 STUDY_BACKUP_TOKEN', 'skipped': True}
    sess = _sess()
    gid, err = _resolve_gist(sess)
    if err:
        _state['last_pull_error'] = err
        return {'ok': False, 'restored': 0, 'error': err}
    files, err = _fetch_files(sess, gid)
    if err:
        _state['last_pull_error'] = err
        return {'ok': False, 'restored': 0, 'error': err}

    remote = _remote_counts(files)
    _state['remote_counts'] = remote
    _state['last_pull_at'] = _now()

    try:
        local = db.count_all()
    except Exception:
        local = {}
    local_m = local.get('mistakes', 0)
    remote_m = remote.get('mistakes', 0)

    # 本地不比远端少 → 没什么要找回的, 别白跑一趟(也避免把删掉的题复活)
    if not force and local_m >= remote_m:
        _state['last_pull_restored'] = 0
        return {'ok': True, 'restored': 0, 'error': None, 'skipped': True}

    payload, err = _join_files(files)
    if err:
        _state['last_pull_error'] = err
        return {'ok': False, 'restored': 0, 'error': err}
    try:
        res = db.import_all(payload, merge=True)
    except Exception as e:
        _state['last_pull_error'] = f'写回本地失败: {e}'
        return {'ok': False, 'restored': 0, 'error': _state['last_pull_error']}
    if not res.get('ok'):
        _state['last_pull_error'] = res.get('error')
        return {'ok': False, 'restored': 0, 'error': res.get('error')}

    restored = sum(v for v in (res.get('imported') or {}).values())
    _state['last_pull_restored'] = restored
    _state['last_pull_error'] = None
    return {'ok': True, 'restored': restored, 'error': None, 'skipped': False}


def startup_sync() -> dict:
    """应用启动时调一次: 把丢失的数据找回来。整个进程只真正执行一次"""
    if not is_enabled():
        return {'ok': False, 'restored': 0, 'error': '未配置', 'skipped': True}
    if _state['started']:
        return {'ok': True, 'restored': 0, 'error': None, 'skipped': True}
    _state['started'] = True
    return pull()


def maybe_push_on_startup() -> dict:
    """
    启动后确保备份里有一份数据。
    刚配好令牌时靠它建立第一份备份 —— 否则得等孩子录入新题才会触发,
    中间这段时间数据还是裸奔的。整个进程只跑一次。
    """
    if not is_enabled() or _state.get('startup_pushed'):
        return {'ok': True, 'error': None, 'skipped': True}
    _state['startup_pushed'] = True
    try:
        if db.count_all().get('mistakes', 0) <= 0:
            return {'ok': True, 'error': None, 'skipped': True}
    except Exception:
        return {'ok': True, 'error': None, 'skipped': True}
    return push()


# ============== 推送 ==============
def push(force: bool = False) -> dict:
    """
    把本地整库推送到 Gist.
    返回 {'ok':bool,'error':str|None,'skipped':bool,'gist_id':str}
    """
    if not is_enabled():
        _state['last_push_error'] = '未配置 STUDY_BACKUP_TOKEN'
        return {'ok': False, 'error': _state['last_push_error'], 'skipped': True}

    if not force and _state['last_push_ts']:
        # 注意: 不能用字符串再 strptime 回来算间隔(默认会按 1900 年解析, 间隔恒为大数,
        # 节流形同虚设)。直接存时间戳最稳。
        if time.time() - _state['last_push_ts'] < _MIN_PUSH_INTERVAL:
            return {'ok': True, 'error': None, 'skipped': True}

    sess = _sess()
    gid, err = _resolve_gist(sess)
    if err:
        _state['last_push_ok'] = False
        _state['last_push_error'] = err
        return {'ok': False, 'error': err}

    try:
        payload = db.export_all()
        local = db.count_all()
    except Exception as e:
        _state['last_push_ok'] = False
        _state['last_push_error'] = f'读取本地数据失败: {e}'
        return {'ok': False, 'error': _state['last_push_error']}

    # ---- 致命陷阱防护: 本地被清空时绝不覆盖有数据的备份 ----
    remote = _state['remote_counts']
    if remote is None:
        _f, _e = _fetch_files(sess, gid)
        if _f is not None:
            remote = _remote_counts(_f)
            _state['remote_counts'] = remote
    if remote:
        lm = local.get('mistakes', 0)
        rm = remote.get('mistakes', 0)
        if lm == 0 and rm > 0:
            err = (f'已拒绝备份: 本地现在 0 条错题, 但备份里有 {rm} 条 '
                   f'(多半是刚重启把数据清空了). 先点「立即找回」恢复, 再备份.')
            _state['last_push_ok'] = False
            _state['last_push_error'] = err
            return {'ok': False, 'error': err}
        if lm > 0 and rm >= 5 and lm < rm * 0.5:
            err = (f'已拒绝备份: 本地 {lm} 条, 备份里 {rm} 条, 差得太多, '
                   f'怕是数据丢了. 先点「立即找回」恢复, 再备份.')
            _state['last_push_ok'] = False
            _state['last_push_error'] = err
            return {'ok': False, 'error': err}

    # 注意: 不要试图"删掉多余的分片文件" —— GitHub 不允许用 null 删文件(实测 422)。
    # 残留的旧分片无害: 索引文件只指向当前这次备份的片。
    files = _split_payload(payload)

    # 内容没变就别写 —— 每次写 Gist 都会产生一个版本, 没必要堆积无意义的 revision。
    # 只能用 tables 算指纹: 整份 payload 里带了 exported_at 时间戳, 每次导出都不同,
    # 拿它算哈希的话指纹永远不相等, 去重就失效了(实测踩过)。
    _fp = hashlib.md5(
        json.dumps(payload.get('tables'), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    if not force and _fp == _state.get('last_payload_fp'):
        return {'ok': True, 'error': None, 'skipped': True}

    try:
        r = sess.patch(f'https://api.github.com/gists/{gid}', timeout=30,
                       json={'files': files})
    except Exception as e:
        _state['last_push_ok'] = False
        _state['last_push_error'] = f'网络异常: {e}'
        return {'ok': False, 'error': _state['last_push_error']}
    if not r.ok:
        err = _humanize_http(r.status_code, r.text[:200])
        _state['last_push_ok'] = False
        _state['last_push_error'] = err
        return {'ok': False, 'error': err}

    _state['last_push_at'] = _now()
    _state['last_push_ts'] = time.time()
    _state['last_payload_fp'] = _fp
    _state['last_push_ok'] = True
    _state['last_push_error'] = None
    _state['remote_counts'] = {t: len(v or []) for t, v in (payload.get('tables') or {}).items()}
    return {'ok': True, 'error': None, 'skipped': False, 'gist_id': gid}


def push_after_change(force: bool = False) -> dict:
    """
    数据发生变化后调用(录入/复习/修改/删除).
    自动带上节流: 120 秒内不重复推。最新的改动会在下一次变更或 2 分钟内被推上去。
    """
    return push(force=force)
