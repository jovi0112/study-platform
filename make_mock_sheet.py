"""生成一张模拟卷子(测试资产 mock_sheet.png)。
含题号、多行题干、选项、孩子手写痕迹、红笔批改 —— 尽量贴近真实卷子, 用来调框选+切题。
"""
from PIL import Image, ImageDraw, ImageFont
import random

W, H = 1240, 1754
FONT = 'C:/Windows/Fonts/msyh.ttc'
BOLD = 'C:/Windows/Fonts/msyhbd.ttc'

f_title = ImageFont.truetype(BOLD, 40)
f_q = ImageFont.truetype(FONT, 32)
f_opt = ImageFont.truetype(FONT, 28)

QUESTIONS = [
    ['1. 计算: (-81) ÷ 2¼ × (-4/9) ÷ (-8)'],
    ['2. 下列单项式中, 与 -2ab² 是同类项的是( )',
     '   A. -2a²b      B. 3ab²      C. -2a²b²      D. 4ab'],
    ['3. 解方程: 2x - 5 = 9'],
    ['4. 化简: 3(2x² - y²) - 2(3y² - 2x²)'],
    ['5. 已知 |a| = 3, |b| = 5, 且 a < b, 求 a - b 的值'],
    ['6. 某商店以每件 60 元购进一批商品, 售价 90 元; 卖出 80% 后',
     '   降价促销, 每件 70 元售完。求这批商品的总利润率。'],
]
WRONG = {1, 4, 5}   # 这几道被红笔打叉(模拟孩子做错的)

img = Image.new('RGB', (W, H), 'white')
d = ImageDraw.Draw(img)
random.seed(7)

d.text((80, 60), '七年级数学  第一章有理数  测试卷', font=f_title, fill='black')
d.line([(80, 118), (W - 80, 118)], fill='#333', width=2)

y = 170
regions = []
for i, lines in enumerate(QUESTIONS, start=1):
    top = y
    for j, ln in enumerate(lines):
        fnt = f_opt if ln.strip().startswith(('A.', 'B.', 'C.', 'D.')) or ln.startswith('   A.') else f_q
        d.text((90, y), ln, font=fnt, fill='black')
        y += 48 if fnt is f_q else 42
    # 孩子手写作答痕迹: 波浪线
    hy = y + 6
    for k in range(3):
        pts = [(110 + t * 9, hy + k * 16 + random.randint(-3, 3)) for t in range(46)]
        d.line(pts, fill='#2b4b8a', width=2)
    y = hy + 62
    # 红笔批改
    if i in WRONG:
        d.line([(W - 150, top + 6), (W - 100, top + 56)], fill='#d00', width=6)
        d.line([(W - 100, top + 6), (W - 150, top + 56)], fill='#d00', width=6)
    regions.append((i, top, y))
    y += 34

img.save('mock_sheet.png')
print('saved mock_sheet.png', img.size)
for i, t, b in regions:
    print(f'  第{i}题 纵向区间 {t}-{b}')
