"""
整张卷子 -> 多道错题: 画框 / 裁剪 / 单题识别 / 批量入库

为什么要有这个模块:
孩子的错题都在同一张卷子上, 一次拍照里面有好几道题。以前整张图只能存成"一道错题",
题干混在一起, 后面重做、讲解、复习全都没法按题拆开。

用法(界面侧):
    1) 上传卷子照片
    2) 在照片上画框 -> 「加为第 N 题」(可加多道)
    3) 「识别选中的 N 道」: 先用整卷 OCR 的行坐标**免费**拿到每题草稿(秒出),
       再对勾选题并发调 AI 看图拿准确题干(实测单张约 5 秒, 3 并发约 8.6 秒/3 张)
    4) 「全部存入错题本」: 一题一条, 配图是各自的裁剪图

实测数据(2026-10-28, 模拟卷子 1240x1754):
    - 整卷本地 OCR 2.9 秒拿到 14 行带坐标文本
    - 框内行归属(免费)立刻出草稿, 但会抄错上标和绝对值: `2¼`→`21/4`、 `|a|`→`[al`
    - 裁剪后送 AI 看图: `(-81) ÷ 2¼ × (-4/9) ÷ (-8)` 全对, 选项 A/B/C/D 顺序也对
    → 所以本地草稿只做预览, **入库用 AI 那版**; AI 没开就用草稿并明说是草稿

真卷实测(2026-10-28, 手机拍的数学卷 1279x1706, 内容是横着存的):
    - AI 抄题**三个方向全都抄对了**(连表格都还原了), 横着不转也认对 → 方向不影响 AI
    - 但本地 OCR 在横图上把每行认成"竖长条"(59x437), 草稿顺序整个倒乱(从最后一句往前)
      → 所以界面给了「左转/右转」按钮, 并用 guess_sideways() 主动提示
    - 三个方向的 OCR 字符数只有 269/290/263(差 8%), **别用"哪边认得多"猜方向, 会猜错**
    - AI 会把表格抄成 Markdown 表格, 进错题本被 md_safe 转义后会塌成一行竖线
      → 入库前统一过 plainify() 转纯文本

裁剪图不要再跑本地 OCR(实测反而更差: 小块被放大后长宽比扭曲, 检测器失去整页上下文)。
"""
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from PIL import Image, ImageOps

import ocr
import llm

MODULE_VER = 5      # v5: 加 edge_cut(框切到字要提醒) 2026-10-28 下午
#                      v4: 横放照片的草稿按横向重排(不再整段倒序挤成一行) 2026-10-28 下午
#                      v3: 加 box_ratio(框太大会不会圈了整页) 2026-10-28 下午
#                      v2: 加旋转 / crop_from / guess_sideways / plainify (2026-10-28 真卷)

# 一次最多框多少道(防止误操作框出几百个)
MAX_CROPS = 20


def module_ver_issue(mods: dict, want: dict) -> list:
    """
    哪些模块的 MODULE_VER 低于界面要求的版本. 返回 [(名字, 实际, 要求), ...]

    为什么需要这个(2026-10-28 线上实测踩到):
    Streamlit 每次交互只重新执行 app.py, **已经 import 过的模块不会重读磁盘**。
    于是会出现"app.py 是新的、scan 是旧的"这种组合 —— 线上就是这样:
    新 app.py 调 scan.box_ratio, 内存里的旧模块没这个函数, 整页抛 AttributeError,
    用户只看到一片红字, 完全不知道是"旧进程"而不是自己操作错了。
    属性清单(_SCAN_NEED)能兜住大部分, 但只要加函数时忘了往清单里补就漏 ——
    这次漏的正是 box_ratio。版本号是兜底层: 加函数就 bump, 界面写死要求值。
    """
    out = []
    for name, need in (want or {}).items():
        mod = (mods or {}).get(name)
        try:
            have = int(getattr(mod, 'MODULE_VER', 0) or 0)
        except Exception:
            have = 0
        try:
            need = int(need)
        except Exception:
            continue
        if have < need:
            out.append((name, have, need))
    return out


# ============== 组件可用性 ==============
def cropper_ready() -> tuple:
    """画框组件是否可用. 返回 (可用与否, 不可用的原因/人话)"""
    try:
        from streamlit_cropper import st_cropper  # noqa: F401
        return True, ''
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:60]}"


# ============== 方向 ==============
def guess_sideways(boxes: list) -> bool:
    """
    文字框是不是被识别成"竖长条"—— 那是整张照片横着存的典型症状。

    2026-10-28 真卷实测(1279x1706, 手机拍的股票涨跌题):
        - 原样(横):  行框 59x437, 高/宽 = 7.4, 一半以上都是这种竖条
        - 转正之后:  行框 1434x67, 高/宽 = 0.05
    差距 100 倍以上, 判据很硬。三方向的字符数只有 269/290/263(差 8%),
    **所以别用"哪个方向认得多"来猜方向, 会猜错**; 用长宽比。

    只做提示, 不自动转 —— 转错了更麻烦, 让用户自己按一下。

    判据放宽过(2026-10-28 下午): 原来是"竖条 ≥3 条且占一半以上", 真卷里带表格时
    表格里的小数字会被认成短框, 竖条占比掉到 50% 以下就不报警了 —— 明明整页是横的,
    界面却一声不吭。改成"竖条比横条多"就算, 宁可多提示一次也别漏。
    """
    if not boxes:
        return False
    tall = 0
    wide = 0
    for b in boxes:
        w = max(1.0, b['x1'] - b['x0'])
        h = max(1.0, b['y1'] - b['y0'])
        if h / w >= 3.0:
            tall += 1
        elif w / h >= 3.0:
            wide += 1
    return tall >= 3 and tall > wide


def _boxes_sideways(boxes: list) -> bool:
    """这批框里"竖长条"是不是主角(只看长度, 不看数量比例)"""
    if not boxes:
        return False
    tall = sum(1 for b in boxes
               if (b['y1'] - b['y0']) / max(1.0, b['x1'] - b['x0']) >= 3.0)
    return tall >= 3 and tall >= len(boxes) * 0.5


# ============== 带坐标的 OCR ==============
def ocr_boxes(abs_path: str, rotate_deg: int = 0) -> list:
    """
    跑一次本地 OCR 并**保留文本框坐标**(ocr.py 里的版本把 box 丢了)。
    坐标换算回原图尺寸, 界面拿到的框坐标可以直接比对。

    rotate_deg: 跟界面上「左转/右转」同一个角度。**界面转正了图, 这里必须一起转**,
    否则坐标还停在原方向, 框内文字归属全错(框住 A 题却取到 B 题的草稿)。
    """
    eng = ocr._get_engine()
    if eng is None:
        return []
    try:
        img = Image.open(abs_path)
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        if rotate_deg:
            img = img.rotate(-rotate_deg, expand=True)
        orig_w = img.size[0]
        big = ocr._maybe_upscale(img)
        scale = (big.size[0] / orig_w) if orig_w else 1.0
        res, _ = eng(ocr._to_bgr_array(big))
    except Exception:
        return []

    out = []
    for item in (res or []):
        try:
            box, text, conf = item[0], item[1], item[2]
            text = (text or '').strip()
            if not text:
                continue
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            out.append({
                'text': text,
                'conf': float(conf),
                'x0': min(xs) / scale, 'x1': max(xs) / scale,
                'y0': min(ys) / scale, 'y1': max(ys) / scale,
            })
        except Exception:
            continue
    return out


def _group_rows(boxes: list) -> list:
    """把散行按"是否同一行"聚类 —— 选项 A/B/C/D 并排时纵向有微小抖动,
    只按 y 排序会把顺序排成 A/B/D/C(实测踩到过)。"""
    if not boxes:
        return []
    hs = sorted(b['y1'] - b['y0'] for b in boxes)
    mh = hs[len(hs) // 2] or 1.0
    rows, cur = [], [boxes[0]]
    for b in boxes[1:]:
        cy = (b['y0'] + b['y1']) / 2
        prev = cur[-1]
        pcy = (prev['y0'] + prev['y1']) / 2
        if abs(cy - pcy) <= 0.6 * mh:
            cur.append(b)
        else:
            rows.append(cur)
            cur = [b]
    rows.append(cur)
    for r in rows:
        r.sort(key=lambda x: x['x0'])
    return rows


def _rows_to_lines(boxes: list) -> list:
    """按"行→列"把框拼成文本行(同一行内按 x 从左到右)"""
    if not boxes:
        return []
    bs = sorted(boxes, key=lambda x: x['y0'])
    rows = _group_rows(bs)
    return [' '.join(x['text'] for x in row) for row in rows]


# 像"题号开头"的样子: 23.  (1)  1)  第3题
_START_RE = re.compile(r'^\s*[（(]?\s*\d{1,3}\s*[）).．、]|^\s*第\s*\d{1,3}\s*题')
_MAIN_NO_RE = re.compile(r'^\s*\d{1,3}\s*[.．、]')      # 23. 这种"大题号"
_SUB_NO_RE = re.compile(r'^\s*[（(]\s*(\d{1,3})\s*[）)]')  # （1）这种"小问号"


def _order_score(lines: list) -> int:
    """
    两份候选草稿, 哪份"读起来"更像正常题面。

    只用于横放照片: 那种情况下按 x 排会得到正序或倒序两种结果, 而 OCR 本身的文字是对的,
    所以只能靠版式特征判断。三个判据(2026-10-28 真卷 + 合成用例都验过):
    1) 小问号是否递增 —— (1)(2)(3) 顺序念才对, 倒着念就是 3,2,1;
    2) 首行是不是"23."这种大题号 —— 页首最典型的特征;
    3) 末行是不是以问号结尾 —— 页尾常是最后一个小问。
    """
    if not lines:
        return -1
    s = 0
    nums = []
    for ln in lines[:8]:
        m = _SUB_NO_RE.match((ln or '').strip())
        if m:
            nums.append(int(m.group(1)))
    for a, b in zip(nums, nums[1:]):
        if b == a + 1:
            s += 2
        elif b < a:
            s -= 2
    first = (lines[0] or '').strip()
    last = (lines[-1] or '').strip()
    if _MAIN_NO_RE.match(first):
        s += 3
    elif _SUB_NO_RE.match(first):
        s += 1
    if last.endswith(('？', '?')):
        s += 1
    if len(first) <= 4:          # "元）""700" 这种碎片, 多半是从页尾开头的倒序
        s -= 1
    return s


def lines_in_rect(boxes: list, rect: tuple) -> str:
    """
    框内有哪些文字行。判据: 行的中心点落在框内。
    返回按"行→列"顺序拼好的文本(同一行内按 x 从左到右)。

    照片横着存时每个文字行会被认成"竖长条"(实测 59x437), 竖条之间 y 几乎一样,
    这时按 y 排等于没排 —— 结果整段被拼成一行, 而且是从页尾开头往回念。
    所以竖条占多数时把坐标先转 90° 再排, 正序倒序各拼一份, 取"首行像题目开头"的那份。
    """
    if not boxes or not rect:
        return ''
    l, t, r, b = rect
    hit = []
    for bx in boxes:
        cx = (bx['x0'] + bx['x1']) / 2
        cy = (bx['y0'] + bx['y1']) / 2
        if l <= cx <= r and t <= cy <= b:
            hit.append(bx)
    if not hit:
        return ''
    if not _boxes_sideways(hit):
        return '\n'.join(_rows_to_lines(hit))

    # 竖条: (x, y) -> (y, x) 让每根竖条变成一行; 再翻一个方向得到另一种顺序
    fwd = [dict(b, x0=b['y0'], x1=b['y1'], y0=b['x0'], y1=b['x1']) for b in hit]
    rev = [dict(b, x0=b['y0'], x1=b['y1'], y0=-b['x1'], y1=-b['x0']) for b in hit]
    a, c = _rows_to_lines(fwd), _rows_to_lines(rev)
    return '\n'.join(a if _order_score(a) >= _order_score(c) else c)


# ============== 裁剪 ==============
def normalize_rect(raw: dict, img_size: tuple) -> tuple:
    """
    把组件返回的 {left, top, width, height} 收成 (l, t, r, b) 并夹到图片范围内。
    组件可能给小数/负数/越界值, 这里统一收拾干净。
    """
    if not raw:
        return None
    try:
        lf = float(raw.get('left', 0))
        tf = float(raw.get('top', 0))
        rf = lf + float(raw.get('width', 0))
        bf = tf + float(raw.get('height', 0))
    except (TypeError, ValueError):
        return None
    W, H = img_size
    # 先按原始浮点算右下角再取整 —— 拿"已取整的左"加宽度会有几个像素的漂移
    l = max(0, min(W - 1, int(round(lf))))
    t = max(0, min(H - 1, int(round(tf))))
    r = max(l + 1, min(W, int(round(rf))))
    b = max(t + 1, min(H, int(round(bf))))
    if (r - l) < 8 or (b - t) < 8:
        return None      # 太小, 多半是误点
    return (l, t, r, b)


def box_ratio(rect: tuple, img_size: tuple) -> float:
    """
    框占整张图的比例(0~1)。用来判断"这个框是不是圈了整页"。

    实测: 单题裁剪块约占 3%~12%, 整页 85%~95%。中间空档很大, 阈值怎么取都不敏感。
    界面靠它提醒"框太大" —— 圈整页会慢 5 倍, 而且把孩子的手写答案一起抄进题面。
    """
    if not rect:
        return 0.0
    try:
        w, h = float(img_size[0]), float(img_size[1])
        if w <= 0 or h <= 0:
            return 0.0
        l, t, r, b = rect
        area = max(0.0, (r - l)) * max(0.0, (b - t))
        return max(0.0, min(1.0, area / (w * h)))
    except (TypeError, ValueError, IndexError):
        return 0.0


def edge_cut(boxes: list, rect: tuple, margin: int = 6) -> list:
    """
    框的哪几条边"从文字中间穿过去"了 —— 说明这一侧的字被裁掉了。

    2026-10-28 真卷实测踩到的坑: 用户框住的那道题，左边框压在第 3 个字上，
    「23.小王上周五买进…」和后面的「（1）」都被切掉半个，
    于是 AI 抄回来的题面天然缺字 —— 而这看起来像是"模型识别差"。
    这种损失是拍大头照那一步造成的，早点说一句比换三个模型都有用。

    判据: 文字框和某条边有交集，且文字框明显跨过这条边(两边都伸出去 margin 像素)。
    返回 ['左'], ['左','上'] 这样的列表; 没问题返回 []。
    """
    if not boxes or not rect:
        return []
    l, t, r, b = rect
    cut = set()
    for bx in boxes:
        x0, x1, y0, y1 = bx['x0'], bx['x1'], bx['y0'], bx['y1']
        if x1 <= l or x0 >= r or y1 <= t or y0 >= b:
            continue          # 完全在框外
        if x0 < l - margin and x1 > l + margin:
            cut.add('左')
        if x1 > r + margin and x0 < r - margin:
            cut.add('右')
        if y0 < t - margin and y1 > t + margin:
            cut.add('上')
        if y1 > b + margin and y0 < b - margin:
            cut.add('下')
    return [e for e in ('上', '下', '左', '右') if e in cut]


def crop_image(abs_path: str, rect: tuple) -> "Image.Image":
    l, t, r, b = rect
    img = Image.open(abs_path)
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img.crop((l, t, r, b))


def crop_from(img: "Image.Image", rect: tuple) -> "Image.Image":
    """
    从**已经拿在手上的图**裁剪。

    为什么要单独一个: crop_image 每次都重新读盘 + exif_transpose, 它不认界面上的旋转
    —— 用户把图转正了, 裁出来还是横的(框和内容错位)。界面那边必须把转正后的图
    传进来裁, 预览和识别用同一张, 才不会"看到的"和"存下来的"不是一个东西。
    """
    l, t, r, b = rect
    return img.crop((l, t, r, b))


def save_crop(img: "Image.Image", orig_name: str, idx: int, upload_dir: str) -> tuple:
    """裁剪图存到 uploads/, 返回 (相对名, 绝对路径)"""
    os.makedirs(upload_dir, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    name = f"crop{idx}_{ts}_{(orig_name or 'sheet')[:24]}.png"
    abs_path = os.path.join(upload_dir, name)
    img.convert('RGB').save(abs_path, format='PNG')
    return name, abs_path


# ============== 抄题结果去装饰 ==============
_TABLE_SEP = re.compile(r'^[\s:|\-]+$')


def plainify(text: str) -> str:
    """
    把 AI 抄题结果里的 Markdown 装饰去掉, 变成纯文本行。

    为什么必须做(2026-10-28 真卷实测):
    错题本题面走的是 `st.write(quiz.md_safe(m['question']))`, md_safe 会把 `|` 转义成 `\\|`。
    而 AI 一看到表格(股票涨跌、实验数据这类题特别多)就输出
        | 星期 | 一 | 二 | 三 |
        |------|----|----|----|
        | 每股涨跌 | +2 | +1.7 | -1.9 |
    转义之后表格彻底塌成一行竖线 `\\| 星期 \\| 一 \\| ...`, 分隔行变成一长串 `------`,
    孩子看到的是一堆符号, 表格信息全丢 —— 而这类题恰恰靠表格读题。

    所以入库前统一转成:
        星期：一 ｜ 二 ｜ 三
        每股涨跌：+2 ｜ +1.7 ｜ -1.9
    用全角 ｜ 分隔, 它不在 md_safe 的转义名单里, 怎么渲染都不会变形。
    顺手去掉 ** 加粗 / # 标题 / 反引号, 行首 - * + 列表符号换成 ·
    """
    if not text:
        return ''
    out, prev_blank = [], False
    for raw in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        s = raw.strip()

        # 表格的分隔行 |---|---| 直接丢掉(它没有内容)
        if s and _TABLE_SEP.match(s):
            continue

        # 表格数据行 -> "第一列：其他列 ｜ 其他列"
        if s.startswith('|') and s.endswith('|') and s.count('|') >= 2:
            cells = [c.strip() for c in s.strip('|').split('|') if c.strip()]
            if cells:
                s = cells[0] if len(cells) == 1 else f"{cells[0]}：" + ' ｜ '.join(cells[1:])

        s = re.sub(r'\*\*(.+?)\*\*', r'\1', s)      # **粗体** -> 粗体
        s = s.replace('**', '').replace('`', '')     # 落单的 ** 和反引号
        s = re.sub(r'^#{1,6}\s*', '', s)             # ### 标题 -> 标题
        s = re.sub(r'^[-*+]\s+', '· ', s)            # - 列表 -> · 列表

        if not s:
            if prev_blank:
                continue
            prev_blank = True
            out.append('')
        else:
            prev_blank = False
            out.append(s)
    return '\n'.join(out).strip()


# ============== AI 看图(单题) ==============
def _to_jpeg_bytes(abs_path: str, max_side: int = 2000, quality: int = 90) -> bytes:
    from io import BytesIO
    try:
        img = Image.open(abs_path).convert('RGB')
        longest = max(img.size)
        if longest > max_side:
            s = max_side / longest
            img = img.resize((int(img.width * s), int(img.height * s)), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format='JPEG', quality=quality)
        return buf.getvalue()
    except Exception:
        with open(abs_path, 'rb') as f:
            return f.read()


def ai_read_one(item: dict, subject: str = '通用', on_wait=None) -> dict:
    """对单张裁剪图调 AI 看图。失败不抛, 把原因放进 item。"""
    path = item.get('crop_abs')
    if not path or not os.path.exists(path):
        return {'success': False, 'error': '裁剪图不见了', 'code': 'missing', 'text': ''}
    try:
        r = llm.read_image(_to_jpeg_bytes(path), mime='image/jpeg', subject=subject,
                           on_wait=on_wait)
    except Exception as e:
        return {'success': False, 'code': 'other',
                'error': f"{type(e).__name__}: {str(e)[:80]}", 'text': ''}
    # 抄出来的 Markdown 表格/加粗, 进错题本会被 md_safe 转义成一堆竖线, 这里先转纯文本
    if isinstance(r, dict) and r.get('success') and (r.get('text') or '').strip():
        clean = plainify(r['text'])
        r['text'] = clean or r['text'].strip()
    return r


def run_ai_batch(items: list, subject: str = '通用', workers: int = 3,
                 on_progress=None, on_note=None) -> dict:
    """
    并发调 AI 看图(网络调用并发, 写结果回主线程 —— 和 diagram.py 一个规矩)。

    on_progress(done, total): 完成几道
    on_note(text):           当前状态一句话(排队等待中/恢复正常), 在主线程调用

    为什么不用 as_completed 阻塞: 那样主线程会一直卡到有结果为止, 期间界面完全静止。
    实测一次 429 重试链要 45 秒以上, 用户看到的是"点完什么都不动", 只能以为卡死。
    改成每 0.4 秒醒一次, 把 worker 写进 _wait_state 的等待信息读出来画到进度条上。

    返回 {'ok': n, 'fail': n, 'seconds': s}
    """
    todo = [it for it in items if it.get('crop_abs')]
    if not todo:
        return {'ok': 0, 'fail': 0, 'seconds': 0.0}
    t0 = time.time()
    workers = max(1, min(int(workers or 1), 4))
    total = len(todo)
    done = 0
    pairs = []          # [(item, result)] —— 结果回主线程再写回 item
    _wait_state = {}    # {idx: (第几次重试, 还要等几秒)} —— worker 写, 主线程读

    def _tick():
        nonlocal done
        done += 1
        if on_progress:
            try:
                on_progress(done, total)
            except Exception:
                pass        # 进度条画不出来不能影响识别本身

    def _make_wait(idx):
        def _w(attempt, sec):
            _wait_state[idx] = (attempt, sec)
        return _w

    # 探测一下当前的 ai_read_one 认不认 on_wait:
    # 只吃两个参数的旧实现/测试替身不能因为多了个可选参数就整批失败。
    import inspect
    try:
        _has_wait = 'on_wait' in inspect.signature(ai_read_one).parameters
    except (TypeError, ValueError):
        _has_wait = False

    def _call_read(it):
        if _has_wait:
            return ai_read_one(it, subject, on_wait=_make_wait(it.get('idx', 0)))
        return ai_read_one(it, subject)

    def _report_wait():
        if not on_note:
            return
        if not _wait_state:
            on_note(None)
            return
        # 有多道同时在等时, 报最晚的那个(用户关心还要多久才能全好)
        attempt, sec = max(_wait_state.values(), key=lambda x: x[1])
        left = len(_wait_state)
        on_note(f'服务商配额排队中，自动重试（第 {attempt} 次，等 {sec} 秒'
                f'{"，" + str(left) + " 道在等" if left > 1 else ""}）…')

    try:
        from concurrent.futures import FIRST_COMPLETED
        from concurrent.futures import wait as _fwait
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_call_read, it): it for it in todo}
            pending = set(futs)
            while pending:
                _done, pending = _fwait(pending, timeout=0.4, return_when=FIRST_COMPLETED)
                for f in _done:
                    _wait_state.pop(futs[f].get('idx', 0), None)
                    try:
                        pairs.append((futs[f], f.result()))
                    except Exception as e:
                        pairs.append((futs[f], {'success': False, 'code': 'other',
                                                'error': f'{type(e).__name__}: {str(e)[:60]}',
                                                'text': ''}))
                    _tick()
                _report_wait()
    except Exception:
        # 并发出任何问题就退化成串行, 不能因为线程池问题整批不出结果
        pairs = []
        done = 0
        _wait_state.clear()
        for it in todo:
            pairs.append((it, _call_read(it)))
            _tick()

    if on_note:
        try:
            on_note(None)
        except Exception:
            pass

    ok = 0
    for it, r in pairs:
        r = r or {}
        if r.get('success') and (r.get('text') or '').strip():
            it['ai_text'] = r['text'].strip()
            it['ai_error'] = None
            it['ai_code'] = 'ok'
            it['ai_verified'] = bool(r.get('verified'))
            ok += 1
        else:
            it['ai_error'] = r.get('error') or 'AI 没认出来'
            it['ai_code'] = r.get('code') or 'other'
    return {'ok': ok, 'fail': total - ok, 'seconds': round(time.time() - t0, 1)}


def ai_retry_one(item: dict, subject: str = '通用') -> dict:
    """只重认一道(失败后不用整批重跑, 省配额也省时间)。返回处理后的 item。"""
    r = ai_read_one(item, subject)
    if r.get('success') and (r.get('text') or '').strip():
        item['ai_text'] = r['text'].strip()
        item['ai_error'] = None
        item['ai_code'] = 'ok'
        item['ai_verified'] = bool(r.get('verified'))
    else:
        item['ai_error'] = r.get('error') or 'AI 没认出来'
        item['ai_code'] = r.get('code') or 'other'
    return item


def final_text(item: dict) -> str:
    """入库用哪一版: AI 优先, 没有就用本地草稿"""
    return (item.get('ai_text') or '').strip() or (item.get('draft_text') or '').strip()


# ============== 批量入库 ==============
def save_batch(items: list, subject: str, upload_dir: str, parent_photo: str = None) -> dict:
    """
    一题一条存进错题本。返回 {'ids': [...], 'skipped': n}
    存不进去的(题面为空)跳过并返回条数, 不静默假装成功。
    """
    import db
    ids, skipped = [], 0
    for it in items:
        text = final_text(it)
        if not text:
            skipped += 1
            continue
        mid = db.add_mistake(
            subject=subject,
            question=text,
            grade=it.get('grade'),
            image_path=it.get('crop_rel'),
            parent_photo=parent_photo,
            source='photo',
        )
        ids.append(mid)
    return {'ids': ids, 'skipped': skipped}
