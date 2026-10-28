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

裁剪图不要再跑本地 OCR(实测反而更差: 小块被放大后长宽比扭曲, 检测器失去整页上下文)。
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from PIL import Image, ImageOps

import ocr
import llm

MODULE_VER = 1

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


# ============== 带坐标的 OCR ==============
def ocr_boxes(abs_path: str) -> list:
    """
    跑一次本地 OCR 并**保留文本框坐标**(ocr.py 里的版本把 box 丢了)。
    坐标换算回原图尺寸, 界面拿到的框坐标可以直接比对。
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


def save_crop(img: "Image.Image", orig_name: str, idx: int, upload_dir: str) -> tuple:
    """裁剪图存到 uploads/, 返回 (相对名, 绝对路径)"""
    os.makedirs(upload_dir, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    name = f"crop{idx}_{ts}_{(orig_name or 'sheet')[:24]}.png"
    abs_path = os.path.join(upload_dir, name)
    img.convert('RGB').save(abs_path, format='PNG')
    return name, abs_path


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
        return llm.read_image(_to_jpeg_bytes(path), mime='image/jpeg', subject=subject)
    except Exception as e:
        return {'success': False, 'error': f"{type(e).__name__}: {str(e)[:80]}", 'text': ''}


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
