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

MODULE_VER = 2      # v2: 加旋转 / crop_from / guess_sideways / plainify (2026-10-28 真卷)

# 一次最多框多少道(防止误操作框出几百个)
MAX_CROPS = 20


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
    """
    if not boxes:
        return False
    tall = 0
    for b in boxes:
        w = max(1.0, b['x1'] - b['x0'])
        h = b['y1'] - b['y0']
        if h / w >= 3.0:
            tall += 1
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


def lines_in_rect(boxes: list, rect: tuple) -> str:
    """
    框内有哪些文字行。判据: 行的中心点落在框内。
    返回按"行→列"顺序拼好的文本(同一行内按 x 从左到右)。
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
    hit.sort(key=lambda x: x['y0'])
    rows = _group_rows(hit)
    lines = []
    for row in rows:
        lines.append(' '.join(x['text'] for x in row))
    return '\n'.join(lines)


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


def ai_read_one(item: dict, subject: str = '通用') -> dict:
    """对单张裁剪图调 AI 看图。失败不抛, 把原因放进 item。"""
    path = item.get('crop_abs')
    if not path or not os.path.exists(path):
        return {'success': False, 'error': '裁剪图不见了', 'text': ''}
    try:
        r = llm.read_image(_to_jpeg_bytes(path), mime='image/jpeg', subject=subject)
    except Exception as e:
        return {'success': False, 'error': f"{type(e).__name__}: {str(e)[:80]}", 'text': ''}
    # 抄出来的 Markdown 表格/加粗, 进错题本会被 md_safe 转义成一堆竖线, 这里先转纯文本
    if isinstance(r, dict) and r.get('success') and (r.get('text') or '').strip():
        clean = plainify(r['text'])
        r['text'] = clean or r['text'].strip()
    return r


def run_ai_batch(items: list, subject: str = '通用', workers: int = 3, on_progress=None) -> dict:
    """
    并发调 AI 看图(网络调用并发, 写结果回主线程 —— 和 diagram.py 一个规矩)。
    on_progress(done, total) 在主线程回调, 界面可以据此推进度条 ——
    实测一张裁剪图要 5~15 秒(两遍校对), 大图或赶上限流能到 1 分钟,
    没有进度条的话用户会以为卡死。
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

    def _tick():
        nonlocal done
        done += 1
        if on_progress:
            try:
                on_progress(done, total)
            except Exception:
                pass        # 进度条画不出来不能影响识别本身

    try:
        from concurrent.futures import as_completed
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(ai_read_one, it, subject): it for it in todo}
            for f in as_completed(futs):
                pairs.append((futs[f], f.result()))
                _tick()
    except Exception:
        # 并发出任何问题就退化成串行, 不能因为线程池问题整批不出结果
        pairs = []
        done = 0
        for it in todo:
            pairs.append((it, ai_read_one(it, subject)))
            _tick()

    ok = 0
    for it, r in pairs:
        r = r or {}
        if r.get('success') and (r.get('text') or '').strip():
            it['ai_text'] = r['text'].strip()
            it['ai_error'] = None
            ok += 1
        else:
            it['ai_error'] = r.get('error') or 'AI 没认出来'
    return {'ok': ok, 'fail': total - ok, 'seconds': round(time.time() - t0, 1)}


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
