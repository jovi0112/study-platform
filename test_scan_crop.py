"""
多题框选(整张卷子 -> 一题一条)的回归测试。

覆盖: 坐标清洗 / 同一行内左右排序 / 框内取行 / 裁剪尺寸 / 批量入库 / AI 并发降级。
不联网(AI 那部分用替身), 不碰真实数据库(指向临时库)。

用法: python test_scan_crop.py
"""
import os
import tempfile

# ---- 先换库, 再 import db/scan, 避免污染真实数据 ----
tmpdir = tempfile.mkdtemp()
import db

db.DB_PATH = os.path.join(tmpdir, 'test_scan.db')

import scan

PASS, FAIL = 0, 0


def check(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  ✓ {name}')
    else:
        FAIL += 1
        print(f'  ✗ {name}  {extra}')


print('== 1. normalize_rect 坐标清洗 ==')
img_size = (1000, 800)
r = scan.normalize_rect({'left': 10, 'top': 20, 'width': 300, 'height': 100}, img_size)
check('正常框原样保留', r == (10, 20, 310, 120), r)

r = scan.normalize_rect({'left': 10.6, 'top': 20.4, 'width': 99.6, 'height': 50.2}, img_size)
check('小数四舍五入', r == (11, 20, 110, 71), r)

r = scan.normalize_rect({'left': -50, 'top': -30, 'width': 200, 'height': 100}, img_size)
check('负数夹到 0', r == (0, 0, 150, 70), r)

r = scan.normalize_rect({'left': 900, 'top': 700, 'width': 500, 'height': 500}, img_size)
check('越界夹到图片边界', r == (900, 700, 1000, 800), r)

check('空框返回 None', scan.normalize_rect(None, img_size) is None)
check('太小的框丢弃', scan.normalize_rect({'left': 5, 'top': 5, 'width': 3, 'height': 2},
                                            img_size) is None)
check('脏数据返回 None', scan.normalize_rect({'left': 'abc'}, img_size) is None)

print('== 2. 同一行内按 x 从左到右(选项 A B C D 不会乱序) ==')
rows = [
    {'text': 'A. -2a²b', 'conf': .9, 'x0': 120, 'x1': 240, 'y0': 300, 'y1': 330},
    {'text': 'C. -2a²b²', 'conf': .9, 'x0': 520, 'x1': 660, 'y0': 304, 'y1': 334},
    {'text': 'B. 3ab²', 'conf': .9, 'x0': 330, 'x1': 430, 'y0': 302, 'y1': 332},
    {'text': 'D. 4ab', 'conf': .9, 'x0': 730, 'x1': 830, 'y0': 299, 'y1': 329},
]
grouped = scan._group_rows(sorted(rows, key=lambda x: x['y0']))
check('聚成 1 行', len(grouped) == 1, f'实际 {len(grouped)} 行')
order = [b['text'][0] for b in grouped[0]]
check('顺序 A B C D', order == ['A', 'B', 'C', 'D'], order)

lines = scan.lines_in_rect(sorted(rows, key=lambda x: x['y0']), (100, 290, 900, 340))
check('框内文本按 A B C D 拼接', lines.startswith('A. -2a²b B. 3ab²'), lines)

print('== 3. 框内取行 ==')
boxes = [
    {'text': '第一题题干', 'conf': .9, 'x0': 80, 'x1': 400, 'y0': 100, 'y1': 140},
    {'text': '第二题题干', 'conf': .9, 'x0': 80, 'x1': 400, 'y0': 300, 'y1': 340},
]
check('只取框内的行', scan.lines_in_rect(boxes, (50, 80, 900, 200)) == '第一题题干')
check('框在空白处返回空串', scan.lines_in_rect(boxes, (50, 600, 900, 700)) == '')
check('空输入不炸', scan.lines_in_rect([], (0, 0, 10, 10)) == '')

print('== 4. 真实图片: OCR 坐标 + 裁剪 ==')
sheet = 'mock_sheet.png'
if os.path.exists(sheet):
    from PIL import Image
    w, h = Image.open(sheet).size
    bx = scan.ocr_boxes(sheet)
    check('整卷 OCR 拿到带坐标的行', len(bx) >= 10, f'{len(bx)} 行')
    check('坐标落在图片范围内',
          all(0 <= b['x0'] <= w and 0 <= b['y1'] <= h for b in bx))
    rect3 = (70, 502, 1170, 634)          # 第3题: 解方程 2x-5=9
    txt3 = scan.lines_in_rect(bx, rect3)
    check('第3题框内文本含"解方程"', '解方程' in txt3.replace(' ', ''), txt3)
    crop = scan.crop_image(sheet, rect3)
    check('裁剪尺寸与框一致', crop.size == (1100, 132), crop.size)
    # 框外的题不该混进来
    check('第3题框内不含第4题内容', '化简' not in txt3, txt3)
else:
    print('  ! 缺少 mock_sheet.png, 跳过真实图片用例')

print('== 5. save_crop 落盘 ==')
from PIL import Image as _Image
tmpimg = _Image.new('RGB', (120, 60), 'white')
rel, absp = scan.save_crop(tmpimg, 'sheet.jpg', 3, os.path.join(tmpdir, 'uploads'))
check('返回相对名', rel.startswith('crop3_'), rel)
check('文件真的存在', os.path.exists(absp))
check('图片可读且尺寸对', _Image.open(absp).size == (120, 60))

print('== 6. final_text 取哪一版 ==')
check('AI 优先', scan.final_text({'ai_text': 'AI版', 'draft_text': '草稿版'}) == 'AI版')
check('AI 空则用草稿', scan.final_text({'ai_text': '', 'draft_text': '草稿版'}) == '草稿版')
check('两边都空返回空串', scan.final_text({}) == '')

print('== 7. run_ai_batch 并发 + 降级 ==')
real = scan.ai_read_one
scan.ai_read_one = lambda item, subject='通用': (
    {'success': True, 'text': f"AI:{item['idx']}"} if item['idx'] % 2
    else {'success': False, 'error': '限流'})
items = [{'idx': i, 'crop_abs': __file__} for i in range(1, 5)]
stat = scan.run_ai_batch(items, workers=3)
check('成功 2 道', stat['ok'] == 2, stat)
check('失败 2 道', stat['fail'] == 2, stat)
check('成功的写回 ai_text', items[0]['ai_text'] == 'AI:1')
check('失败的记 ai_error', items[1]['ai_error'] == '限流')


def boom(item, subject='通用'):
    raise RuntimeError('线程池炸了')


def serial_ok(item, subject='通用'):
    return {'success': True, 'text': 'S'}


scan.ai_read_one = serial_ok
scan.run_ai_batch(items[:2], workers=2)
check('异常后退化成串行仍出结果', items[0]['ai_text'] == 'S')
scan.ai_read_one = real

print('== 8. save_batch 批量入库 ==')
db.init_db()
sheet_rel = 'sheet_test.jpg'
payload = [
    {'idx': 1, 'crop_rel': 'crop1.png', 'crop_abs': '', 'ai_text': '第一题', 'draft_text': ''},
    {'idx': 2, 'crop_rel': 'crop2.png', 'crop_abs': '', 'ai_text': '', 'draft_text': '第二题草稿'},
    {'idx': 3, 'crop_rel': 'crop3.png', 'crop_abs': '', 'ai_text': '', 'draft_text': ''},
]
res = scan.save_batch(payload, subject='数学', upload_dir=os.path.join(tmpdir, 'uploads'),
                      parent_photo=sheet_rel)
check('存进 2 条(空题面跳过)', len(res['ids']) == 2, res)
check('跳过 1 条', res['skipped'] == 1, res)

rows = db.list_mistakes(subject='数学', limit=10)
check('库里条数对', len(rows) == 2, len(rows))
by_q = {r['question']: r for r in rows}
check('AI 版优先入库', '第一题' in by_q)
check('草稿也能入库', '第二题草稿' in by_q)
r1 = by_q['第一题']
check('配图是裁剪图', r1['image_path'] == 'crop1.png', r1['image_path'])
check('记了原卷路径', r1['parent_photo'] == sheet_rel, r1['parent_photo'])
check('来源标 photo', r1['source'] == 'photo', r1['source'])

print('== 9. 组件可用性 ==')
ok, why = scan.cropper_ready()
check('画框组件可用', ok is True, why)

print('== 10. 上限 ==')
check('最多 20 道', scan.MAX_CROPS == 20)

print('== 11. plainify: AI 抄出来的 Markdown 表格要转纯文本 ==')
# 真卷实测: 股票涨跌题被 AI 抄成 md 表格, 进错题本经 md_safe 转义后塌成一行竖线
md = ('23. 小王买进股票…\n'
      '| 星期 | 一 | 二 | 三 |\n'
      '|------|----|----|----|\n'
      '| 每股涨跌 | +2 | +1.7 | -1.9 |\n'
      '**注意**：下同\n'
      '### 小题\n'
      '- 第一条\n'
      '* 第二条\n')
p = scan.plainify(md)
check('表格分隔行被删掉', '---' not in p, p)
check('表格首行转成「列名：值 ｜ 值」', '星期：一 ｜ 二 ｜ 三' in p, p)
check('表格第二行同样处理', '每股涨跌：+2 ｜ +1.7 ｜ -1.9' in p, p)
check('不再残留半角竖线', '|' not in p, p)
check('** 加粗被去掉', '**' not in p and '注意：下同' in p, p)
check('# 标题符号被去掉', '###' not in p and '小题' in p, p)
check('列表符号换成 ·', '· 第一条' in p and '· 第二条' in p, p)
check('正文没被吃掉', '23. 小王买进股票' in p, p)
check('空串安全', scan.plainify('') == '')
check('单行表格也能转', scan.plainify('| a | b |') == 'a：b', scan.plainify('| a | b |'))
check('普通文字原样返回', scan.plainify('解方程 2x-5=9') == '解方程 2x-5=9')
check('数学符号不被误改', scan.plainify('(-81) ÷ 2¼ × (-4/9)') == '(-81) ÷ 2¼ × (-4/9)')

print('== 12. guess_sideways: 照片横着放要能识别 ==')
h_rows = [{'text': 'x', 'conf': .9, 'x0': 0, 'x1': 800, 'y0': i * 40, 'y1': i * 40 + 30}
          for i in range(5)]
v_rows = [{'text': 'x', 'conf': .9, 'x0': i * 60, 'x1': i * 60 + 40, 'y0': 0, 'y1': 300}
          for i in range(5)]
check('横排文字不报警', scan.guess_sideways(h_rows) is False)
check('竖长条报警', scan.guess_sideways(v_rows) is True)
check('空列表不报警', scan.guess_sideways([]) is False)
check('只有两行竖条不报(避免误判)', scan.guess_sideways(v_rows[:2]) is False)

print('== 13. crop_from: 从内存里的图裁(界面转正后必须用这个) ==')
base = _Image.new('RGB', (200, 100), 'white')
c = scan.crop_from(base, (10, 20, 110, 80))
check('裁剪尺寸对', c.size == (100, 60), c.size)
check('转正后画布换向', base.rotate(-90, expand=True).size == (100, 200))

print('== 14. 横图实测: ocr_boxes 的 rotate_deg 要跟着界面一起转 ==')
if os.path.exists(sheet):
    side_path = os.path.join(tmpdir, 'sideways.png')
    _Image.open(sheet).rotate(-90, expand=True).convert('RGB').save(side_path)
    bx_side = scan.ocr_boxes(side_path)
    check('横着存的图被认成竖条(会提示用户转正)', scan.guess_sideways(bx_side) is True,
          f'{len(bx_side)} 行')
    bx_fix = scan.ocr_boxes(side_path, 90)
    check('带 rotate_deg 转回来后不再是竖条', scan.guess_sideways(bx_fix) is False,
          f'{len(bx_fix)} 行')
    txt_all = scan.lines_in_rect(bx_fix, (0, 0, 1754, 1240))
    check('转正后全图能取到"解方程"', '解方程' in txt_all.replace(' ', ''), txt_all[:80])
else:
    print('  ! 缺少 mock_sheet.png, 跳过横图用例')

print('== 15. 大图不做第二遍校对(省一半配额) ==')
import llm
check('单题裁剪块(896x376)要校对', llm.auto_verify(47 * 1024, 896 * 376) is True)
check('整页图(1279x1706)只抄一遍', llm.auto_verify(226 * 1024, 1279 * 1706) is False)
check('A4 300dpi 整页也只抄一遍', llm.auto_verify(600 * 1024, 2480 * 3508) is False)
check('拿不到尺寸时按体积退化', llm.auto_verify(47 * 1024) is True)
check('大体积但无尺寸也要保守跳过校对', llm.auto_verify(400 * 1024) is False)
check('空图也当小图走正常流程', llm.auto_verify(0) is True)

print('== 16. 错误分类与文案: 不许把超时/鉴权混说成限流 ==')
check('429 归为配额', llm.error_code('Error code: 429 - rate limit') == 'rate_limit')
check('1302 归为配额', llm.error_code('{"code":1302}') == 'rate_limit')
check('超时单独一类', llm.error_code('Request timed out') == 'timeout')
check('401 归鉴权', llm.error_code('Error code: 401 - invalid_api_key') == 'auth')
_h429 = llm._humanize_error('Error code: 429 - rate limited')
_h_to = llm._humanize_error('Request timed out after 90s')
check('配额文案提到会自动重试', '重试' in _h429, _h429)
check('配额文案不再硬说"免费档"', '免费' not in _h429, _h429)
check('超时文案说超时', '超时' in _h_to, _h_to)
check('超时文案不冒充限流', '配额' not in _h_to and '限流' not in _h_to, _h_to)

print('== 17. 等配额时界面要能说话(不能 54 秒白屏) ==')
_ow = []


class _FakeClient:
    """每次调用都抛 429, 用来验证重试链"""

    def __init__(self):
        self.calls = 0
        self.chat = self
        self.completions = self

    def create(self, **kw):
        self.calls += 1
        raise RuntimeError('Error code: 429 - rate limit exceeded')


_fc = _FakeClient()
_old_waits = llm.VISION_WAITS
llm.VISION_WAITS = (0, 0.01, 0.01)   # 别真等 15+30 秒
try:
    try:
        llm._vision_call(_fc, on_wait=lambda a, s: _ow.append((a, s)), model='x')
        _raised = False
    except Exception:
        _raised = True
finally:
    llm.VISION_WAITS = _old_waits
check('429 三次都败后抛出', _raised)
check('重试了 3 次', _fc.calls == 3, _fc.calls)
check('每次等待前都回调界面', _ow == [(1, 0.01), (2, 0.01)], _ow)

_orig_read = scan.ai_read_one


def _slow_read(it, subject='通用', on_wait=None):
    if on_wait:
        on_wait(1, 0.6)
    import time as _t
    _t.sleep(0.9)          # 让主线程轮询(0.4s 一次)有机会读到等待状态
    return {'success': True, 'text': '第 1 题 抄写结果', 'verified': True, 'code': 'ok'}


_notes = []
_tmp_items = [{'idx': 1, 'crop_abs': os.path.abspath(__file__), 'ai_text': '', 'ai_error': None}]
scan.ai_read_one = _slow_read
try:
    _st = scan.run_ai_batch(_tmp_items, subject='数学', workers=1, on_note=_notes.append)
finally:
    scan.ai_read_one = _orig_read
check('等待时进度条有话说', any(n and '排队' in n for n in _notes), _notes)
check('跑完把提示收回(清空)', _notes and _notes[-1] is None, _notes[-1] if _notes else None)
check('等待不影响结果', _st['ok'] == 1 and _tmp_items[0]['ai_text'] == '第 1 题 抄写结果', _st)
check('记下"校对过"标记', _tmp_items[0].get('ai_verified') is True)

print('== 18. 单题重认(失败后不用整批重跑) ==')


def _ok_read(it, subject='通用', on_wait=None):
    return {'success': True, 'text': '重认后的题面', 'verified': False, 'code': 'ok'}


def _fail_read(it, subject='通用', on_wait=None):
    return {'success': False, 'text': '', 'code': 'rate_limit', 'error': '排队中'}


_it = {'idx': 2, 'crop_abs': os.path.abspath(__file__), 'ai_text': '', 'ai_error': '旧错误'}
scan.ai_read_one = _ok_read
try:
    scan.ai_retry_one(_it, '数学')
finally:
    scan.ai_read_one = _orig_read
check('重认成功写回题面', _it['ai_text'] == '重认后的题面')
check('重认成功清掉旧错误', _it['ai_error'] is None)
check('没校对也如实记下', _it['ai_verified'] is False)

_it2 = {'idx': 3, 'crop_abs': os.path.abspath(__file__), 'ai_text': '', 'ai_error': None}
scan.ai_read_one = _fail_read
try:
    scan.ai_retry_one(_it2, '数学')
finally:
    scan.ai_read_one = _orig_read
check('重认失败留下原因', _it2['ai_error'] == '排队中', _it2['ai_error'])
check('重认失败带错误码(界面据此给建议)', _it2['ai_code'] == 'rate_limit', _it2.get('ai_code'))

print('== 19. box_ratio: 判断"是不是圈了整页" ==')
_sz = (1279, 1706)
check('整页框约 83%(1160x1570 / 全图)', abs(scan.box_ratio((60, 80, 1220, 1650), _sz) - 0.835) < 0.005,
      scan.box_ratio((60, 80, 1220, 1650), _sz))
check('单题框约 5%', scan.box_ratio((100, 400, 700, 560), _sz) < 0.2,
      scan.box_ratio((100, 400, 700, 560), _sz))
check('整页框过 55% 线(会提示)', scan.box_ratio((60, 80, 1220, 1650), _sz) > 0.55)
check('单题框不过线(不打扰)', scan.box_ratio((100, 400, 700, 560), _sz) <= 0.55)
check('空框返回 0', scan.box_ratio(None, _sz) == 0.0)
check('尺寸为 0 不除零', scan.box_ratio((0, 0, 10, 10), (0, 0)) == 0.0)
check('脏数据返回 0', scan.box_ratio(('a', 'b', 'c', 'd'), _sz) == 0.0)
check('超出图片也夹在 1.0 以内', scan.box_ratio((-99, -99, 9999, 9999), _sz) <= 1.0)

print(f'\n结果: {PASS} 通过 / {FAIL} 失败')
raise SystemExit(1 if FAIL else 0)
