"""
本地冒烟测试(不进 Git 部署包也能跑, 只用于开发自检)
验证: 多方案生成 / 择优 / 文本清洗 / 无文字图不崩
用法: python test_ocr_local.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image, ImageDraw, ImageFont
import ocr


def make_test_image(path='_test_img.png'):
    font_path = 'C:/Windows/Fonts/msyh.ttc'
    font = ImageFont.truetype(font_path, 30)
    img = Image.new('RGB', (900, 320), 'white')
    d = ImageDraw.Draw(img)
    lines = ['计算: (-81)÷2¼×(-4/9)÷(-8)', '12+6+5-10 = 13', '解方程: 2x - 5 = 9']
    y = 30
    for l in lines:
        d.text((40, y), l, fill=(10, 10, 10), font=font)
        y += 60
    img.save(path)
    return path


def main():
    print('引擎可用:', ocr.is_available(), ocr.engine_error())

    # 1) 文本清洗
    print('清洗:', repr(ocr.clean_line('１２＋６＝１８　（-test）')))

    # 2) 正常图
    p = make_test_image()
    res = ocr.ocr_image_strong(p)
    print('\n=== 识别结果 ===')
    print('success:', res['success'], '| 方案:', res['variant'], '| 试了', res['tried'], '种')
    print('置信度:', round(res['avg_confidence'], 3))
    print('文本:\n' + res['text'])
    print('各方案:', [(v['name'], round(v['score'], 3)) for v in res.get('variants', [])])

    # 3) 空白图(不能崩, 要给得出"没认出来"的结论)
    blank = Image.new('RGB', (600, 200), 'white')
    blank.save('_blank.png')
    res2 = ocr.ocr_image_strong('_blank.png')
    print('\n空白图 -> success:', res2['success'], '| lines:', len(res2['lines']),
          '| error:', res2['error'])

    for f in (p, '_blank.png'):
        try:
            os.remove(f)
        except OSError:
            pass


if __name__ == '__main__':
    main()
