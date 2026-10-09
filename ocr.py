"""
OCR 模块: 用 RapidOCR (ONNX Runtime) 识别题目图片
- 输入: 文件路径 / numpy 数组
- 输出: (text, confidence, lines)
- 自动去重/合并相邻文字行
"""
import os
from typing import Optional

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


def ocr_image(image_path: str) -> dict:
    """
    识别图片中的文字
    返回: {
        'success': bool,
        'text': str,            # 全文(合并后)
        'lines': [str, ...],    # 逐行
        'avg_confidence': float,
        'error': str or None
    }
    """
    engine = _get_engine()
    if engine is None:
        return {
            'success': False,
            'text': '',
            'lines': [],
            'avg_confidence': 0.0,
            'error': _engine_error or 'OCR 引擎未就绪',
        }
    try:
        result, elapse = engine(image_path)
        if not result:
            return {
                'success': True, 'text': '', 'lines': [], 'avg_confidence': 0.0, 'error': None
            }
        lines = []
        confs = []
        for box, text, conf in result:
            text = (text or '').strip()
            if text:
                lines.append(text)
                confs.append(float(conf))
        return {
            'success': True,
            'text': '\n'.join(lines),
            'lines': lines,
            'avg_confidence': sum(confs) / len(confs) if confs else 0.0,
            'error': None,
        }
    except Exception as e:
        return {
            'success': False, 'text': '', 'lines': [], 'avg_confidence': 0.0,
            'error': f"OCR 识别异常: {type(e).__name__}: {e}",
        }


def ocr_to_text(image_path: str) -> str:
    """简单封装: 仅返回文字"""
    res = ocr_image(image_path)
    return res['text'] if res['success'] else ''
