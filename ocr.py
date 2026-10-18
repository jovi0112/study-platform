"""
OCR 模块(图片读取加强版)

设计原则: 一次拍照 → 多种预处理方案并行识别 → 按置信度择优 → 文本清洗
- 方案 1 原图(EXIF 方向已纠正)
- 方案 2 灰度 + 自动对比度 + 放大(小图 2x)
- 方案 3 二值化(手写/打印混合时更稳)
- 方案 4 反色(白字黑底/阴影重的照片)
- 方案 5 轻度锐化 + 中值去噪

对外接口保持兼容:
- ocr_image(path) -> {success, text, lines, avg_confidence, error}
- 额外返回: variant(胜出方案名), variants(各方案得分明细)
- ocr_image_strong(path) 与 ocr_image 等价, 只是语义更明确

AI 视觉识图(更准, 需 Vision Key)在 llm.py 的 read_image() 里, 本模块不联网.
"""
import os
from typing import Optional, List, Tuple

try:
    import numpy as np
    from PIL import Image, ImageOps, ImageFilter, ImageEnhance
    _IMAGING_OK = True
    _IMAGING_ERROR = None
except Exception as e:  # 缺依赖时降级为"原图直出", 不让整个页面崩
    np = None
    _IMAGING_OK = False
    _IMAGING_ERROR = f"{type(e).__name__}: {e}"

_engine = None
_engine_error: Optional[str] = None


def _get_engine():
    """懒加载 OCR 引擎, 失败时记录原因(不抛)"""
    global _engine, _engine_error
    if _engine is not None:
        return _engine
    if _engine_error is not None:
        return None
    try:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
        return _engine
    except Exception as e:
        _engine_error = f"OCR 引擎加载失败: {type(e).__name__}: {e}"
        return None


def is_available() -> bool:
    return _get_engine() is not None


def engine_error() -> Optional[str]:
    return _engine_error


# ============== 图像预处理: 生成多种"看得更清"的版本 ==============
def _open_fixed(path: str) -> "Image.Image":
    """读图 + 修正手机拍照的旋转(EXIF) + 统一 RGB"""
    img = Image.open(path)
    try:
        img = ImageOps.exif_transpose(img)   # 手机竖拍/横拍常常是"躺着"存的
    except Exception:
        pass
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    return img


def _maybe_upscale(img: "Image.Image", min_width: int = 1200) -> "Image.Image":
    """小图放大: OCR 对小于 ~1200px 宽的图识别率明显下降"""
    if img.width < min_width:
        scale = min_width / max(img.width, 1)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    return img


def _to_bgr_array(img: "Image.Image") -> "np.ndarray":
    """PIL -> numpy(BGR 三通道), RapidOCR 吃这个格式最稳"""
    arr = np.array(img.convert('RGB'))
    return arr[:, :, ::-1].copy()


def _binarize(img: "Image.Image") -> "Image.Image":
    """自适应阈值二值化(不用 cv2, 纯 numpy, 少一个依赖)"""
    gray = np.array(img.convert('L')).astype(np.float32)
    # 用块均值做局部阈值, 抵消拍照时的明暗不均
    h, w = gray.shape
    bh, bw = max(h // 20, 8), max(w // 20, 8)
    pad_h = (bh - h % bh) % bh
    pad_w = (bw - w % bw) % bw
    gp = np.pad(gray, ((0, pad_h), (0, pad_w)), mode='edge')
    blocks = gp.reshape(h // bh + (1 if pad_h else 0), bh, w // bw + (1 if pad_w else 0), bw)
    means = blocks.mean(axis=(1, 3))
    local = np.repeat(np.repeat(means, bh, axis=0), bw, axis=1)[:h, :w]
    binary = (gray > (local - 5.0)).astype(np.uint8) * 255
    return Image.fromarray(binary).convert('RGB')


def build_variants(path: str) -> List[Tuple[str, "np.ndarray"]]:
    """
    生成多种预处理版本的图片数组.
    顺序无所谓, 后面会按识别得分重新排序.
    """
    base = _open_fixed(path)
    base = _maybe_upscale(base)
    variants = [('原图', base)]

    gray = base.convert('L')
    gray_enh = ImageEnhance.Contrast(gray).enhance(1.6)
    gray_enh = ImageOps.autocontrast(gray_enh)
    variants.append(('灰度增强', gray_enh.convert('RGB')))

    sharp = base.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    variants.append(('锐化', sharp))

    try:
        variants.append(('二值化', _binarize(gray_enh)))
    except Exception:
        pass

    try:
        inv = ImageOps.invert(gray_enh.convert('L'))
        variants.append(('反色', inv.convert('RGB')))
    except Exception:
        pass

    try:
        denoise = gray_enh.filter(ImageFilter.MedianFilter(size=3))
        variants.append(('去噪', denoise.convert('RGB')))
    except Exception:
        pass

    return [(name, _to_bgr_array(im)) for name, im in variants]


# ============== 文本清洗: 专治数学题里的常见误读 ==============
_FULL2HALF = {chr(0xFF01 + i): chr(0x21 + i) for i in range(94)}  # 全角 -> 半角
_MATH_FIX = {
    '＝': '=', '﹦': '=', '＋': '+', '－': '-', '−': '-', '–': '-', '—': '-',
    '×': '×', '✕': '×', '✖': '×', '＊': '×', '÷': '÷', '∕': '÷', '⁄': '÷',
    '（': '(', '）': ')', '［': '[', '］': ']', '｛': '{', '｝': '}',
    '∶': ':', '：': ':', '｜': '|', '·': '·', '．': '.', '。': '。',
}


def clean_line(line: str) -> str:
    """
    单行清洗. 只做"几乎不会改错"的替换:
    - 全角数字/字母 -> 半角(题目里的数字几乎都是半角)
    - 数学运算符归一(＝ -> = 等)
    - 去首尾空白 / 压缩连续空格
    """
    if not line:
        return ''
    out = []
    for ch in line:
        if ch in _MATH_FIX:
            out.append(_MATH_FIX[ch])
        elif ch in _FULL2HALF and (ch.isdigit() or ('＀' <= ch <= 'ﾟ') or ('Ａ' <= ch <= 'Ｚ') or ('ａ' <= ch <= 'ｚ')):
            out.append(_FULL2HALF[ch])
        else:
            out.append(ch)
    text = ''.join(out)
    text = ' '.join(text.split())
    return text.strip()


def clean_lines(lines: List[str]) -> List[str]:
    """清洗 + 去掉重复行 + 丢掉明显是噪声的孤立字符行"""
    seen = set()
    result = []
    for raw in lines or []:
        line = clean_line(raw)
        if not line:
            continue
        # 单个非文字字符(如孤立的 "-"、"~")多半是污渍/折痕
        if len(line) == 1 and not (line.isalnum() or '\u4e00' <= line <= '\u9fff'):
            continue
        if line in seen:
            continue
        seen.add(line)
        result.append(line)
    return result


# ============== 单次识别 ==============
def _run_engine(engine, img_arr) -> dict:
    """跑一次 RapidOCR, 返回 {lines, avg_confidence, ok}"""
    try:
        result, _elapse = engine(img_arr)
    except Exception as e:
        return {'ok': False, 'lines': [], 'avg_confidence': 0.0,
                'error': f"{type(e).__name__}: {e}"}
    if not result:
        return {'ok': True, 'lines': [], 'avg_confidence': 0.0, 'error': None}
    lines, confs = [], []
    for _box, text, conf in result:
        text = (text or '').strip()
        if text:
            lines.append(text)
            confs.append(float(conf))
    return {
        'ok': True,
        'lines': lines,
        'avg_confidence': (sum(confs) / len(confs)) if confs else 0.0,
        'error': None,
    }


def _score(lines: List[str], conf: float) -> float:
    """
    给方案打分: 置信度为主, 识别到的有效内容量为辅.
    防止出现"只认出 1 个字但置信度 99%"被选中的情况.
    """
    chars = sum(len(l) for l in lines)
    content_bonus = min(chars / 60.0, 1.0) * 0.15   # 最多加 0.15
    line_bonus = min(len(lines) / 8.0, 1.0) * 0.05  # 最多加 0.05
    return conf * 0.8 + content_bonus + line_bonus


# ============== 主入口: 多方案择优 ==============
def ocr_image_strong(image_path: str, max_variants: int = 6) -> dict:
    """
    加强版识别: 多套预处理方案都跑一遍, 选最好的那套返回.

    返回:
    {
      'success': bool, 'text': str, 'lines': [...], 'avg_confidence': float,
      'error': str|None,
      'variant': '胜出方案名',
      'variants': [{'name','lines','avg_confidence','score'}, ...],   # 按分数降序
      'tried': int, 'degraded': bool   # degraded=True 表示图像处理库不可用, 只跑了原图
    }
    """
    engine = _get_engine()
    if engine is None:
        return {'success': False, 'text': '', 'lines': [], 'avg_confidence': 0.0,
                'error': _engine_error or 'OCR 引擎未就绪',
                'variant': None, 'variants': [], 'tried': 0, 'degraded': True}

    degraded = False
    if _IMAGING_OK:
        try:
            variants = build_variants(image_path)[:max_variants]
        except Exception as e:
            variants = []
            degraded = True
            _soft_error = f"图像预处理失败: {type(e).__name__}: {e}"
    else:
        variants = []
        degraded = True
        _soft_error = f"缺少图像库: {_IMAGING_ERROR}"

    if not variants:
        # 降级: 直接把原路径丢给引擎(老行为)
        res = _run_engine(engine, image_path)
        lines = clean_lines(res['lines'])
        return {
            'success': res['ok'],
            'text': '\n'.join(lines),
            'lines': lines,
            'avg_confidence': res['avg_confidence'],
            'error': res['error'],
            'variant': '原图(直出)',
            'variants': [],
            'tried': 1,
            'degraded': True,
        }

    scored = []
    last_error = None
    for name, arr in variants:
        res = _run_engine(engine, arr)
        if not res['ok']:
            last_error = res['error']
            continue
        lines = clean_lines(res['lines'])
        if not lines:
            continue
        s = _score(lines, res['avg_confidence'])
        scored.append({'name': name, 'lines': lines,
                       'avg_confidence': res['avg_confidence'], 'score': s})

    if not scored:
        return {'success': True, 'text': '', 'lines': [], 'avg_confidence': 0.0,
                'error': last_error or '这张图没识别出文字(可能太模糊/太暗, 可试试重新拍)',
                'variant': None, 'variants': [], 'tried': len(variants), 'degraded': degraded}

    scored.sort(key=lambda x: x['score'], reverse=True)
    best = scored[0]
    return {
        'success': True,
        'text': '\n'.join(best['lines']),
        'lines': best['lines'],
        'avg_confidence': best['avg_confidence'],
        'error': None,
        'variant': best['name'],
        'variants': scored,
        'tried': len(variants),
        'degraded': degraded,
    }


def ocr_image(image_path: str) -> dict:
    """兼容旧接口: 内部走加强版"""
    return ocr_image_strong(image_path)


def ocr_to_text(image_path: str) -> str:
    """简单封装: 仅返回文字"""
    res = ocr_image(image_path)
    return res['text'] if res['success'] else ''
