# ============================================================
# ocr_utils.py v1.0
#
# 新增：wait_for_answer_load() 智能检测回答加载
# 所有等待时间统一引用 config.py
# ============================================================

import pyautogui
import numpy as np
import re
import time
import logging

log = logging.getLogger(__name__)

# ============================================================
# OCR 引擎
# ============================================================

_ocr_engine = None

def _get_engine():
    global _ocr_engine
    if _ocr_engine is None:
        log.info("初始化 OCR 引擎...")
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
        log.info("OCR 引擎就绪。")
    return _ocr_engine

# ============================================================
# 标志检测
# ============================================================

_END_PATTERN = re.compile(
    r'(编辑于|发布于)\s*\d{2,4}[-/年.]\d{1,2}[-/月.]?\d{0,2}'
)

def _is_answer_end_marker(line):
    return bool(_END_PATTERN.search(line))

def _check_lines_for_end(lines):
    for i, line in enumerate(lines):
        if _is_answer_end_marker(line):
            return True, i
    return False, -1

# ============================================================
# 核心 OCR
# ============================================================

def ocr_region(left_x, top_y, right_x, bottom_y):
    w = right_x - left_x
    h = bottom_y - top_y
    if w <= 0 or h <= 0:
        return [], []
    engine = _get_engine()
    screenshot = pyautogui.screenshot(region=(left_x, top_y, w, h))
    result, _ = engine(np.array(screenshot))
    if not result:
        return [], []
    result.sort(key=lambda item: (
        sum(p[1] for p in item[0]) / 4,
        sum(p[0] for p in item[0]) / 4
    ))
    return _merge_to_lines(result), result

def ocr_region_raw(left_x, top_y, right_x, bottom_y):
    """返回每个文字块的 (text, abs_cx, abs_cy)，不做行合并"""
    w = right_x - left_x
    h = bottom_y - top_y
    if w <= 0 or h <= 0:
        return []
    engine = _get_engine()
    screenshot = pyautogui.screenshot(region=(left_x, top_y, w, h))
    result, _ = engine(np.array(screenshot))
    if not result:
        return []
    blocks = []
    for box, text, score in result:
        rel_cx = sum(p[0] for p in box) / 4
        rel_cy = sum(p[1] for p in box) / 4
        blocks.append((text, left_x + rel_cx, top_y + rel_cy))
    blocks.sort(key=lambda b: (b[2], b[1]))
    return blocks

def ocr_screenshot(region=None):
    if region:
        x, y, w, h = region
        return ocr_region(x, y, x + w, y + h)
    engine = _get_engine()
    screenshot = pyautogui.screenshot()
    result, _ = engine(np.array(screenshot))
    if not result:
        return [], []
    result.sort(key=lambda item: (
        sum(p[1] for p in item[0]) / 4,
        sum(p[0] for p in item[0]) / 4
    ))
    return _merge_to_lines(result), result

def _merge_to_lines(ocr_results, y_threshold=15):
    if not ocr_results:
        return []
    items = [(sum(p[1] for p in box)/4, sum(p[0] for p in box)/4, text)
             for box, text, score in ocr_results]
    lines = []
    cur_y = items[0][0]
    cur_parts = []
    for y, x, text in items:
        if abs(y - cur_y) <= y_threshold:
            cur_parts.append((x, text))
        else:
            cur_parts.sort(key=lambda p: p[0])
            lines.append(''.join(t for _, t in cur_parts))
            cur_y = y
            cur_parts = [(x, text)]
    if cur_parts:
        cur_parts.sort(key=lambda p: p[0])
        lines.append(''.join(t for _, t in cur_parts))
    return lines

# ============================================================
# OCR 查找 + 点击
# ============================================================

def find_text_on_screen(target_text, region=None):
    engine = _get_engine()
    screenshot = pyautogui.screenshot(region=region)
    result, _ = engine(np.array(screenshot))
    if not result:
        return None
    for box, text, score in result:
        if target_text in text:
            cx = sum(p[0] for p in box) / 4
            cy = sum(p[1] for p in box) / 4
            if region:
                cx += region[0]
                cy += region[1]
            return (int(cx), int(cy))
    return None

def click_by_text(target_text, region=None, retries=3, wait=1.0, log_name=None):
    from config import random_mouse_duration
    name = log_name or target_text
    for attempt in range(retries):
        pos = find_text_on_screen(target_text, region=region)
        if pos:
            log.info(f"OCR 定位「{name}」→ ({pos[0]}, {pos[1]})")
            pyautogui.click(pos[0], pos[1], duration=random_mouse_duration())
            time.sleep(0.3)
            return True
        if attempt < retries - 1:
            log.info(f"  未找到「{name}」，重试（{attempt+1}/{retries}）")
            time.sleep(wait)
    log.warning(f"OCR 未找到「{name}」")
    return False

# ============================================================
# 新功能：智能检测回答是否已加载
# ============================================================

def wait_for_answer_load(left_x, top_y, right_x, bottom_y,
                          poll_interval=1.0, max_wait=10.0):
    """
    进入问题页后，向下滚动触发回答加载，然后 OCR 轮询检测。

    判定回答已加载的条件（满足任一）：
    1. 屏幕上出现"人赞同了该回答"或"人赞同"
    2. 屏幕上出现"个回答"（回答列表标头）
    3. 相比第一次 OCR，文字行数显著增加（增加 5 行以上）

    返回 True 加载成功 / False 超时
    """
    log.info("触发回答加载...")
    pyautogui.scroll(-5)
    time.sleep(0.5)

    # 基准：刚进页面时的行数
    baseline_lines, _ = ocr_region(left_x, top_y, right_x, bottom_y)
    baseline_count = len(baseline_lines)

    start = time.time()
    while time.time() - start < max_wait:
        time.sleep(poll_interval)

        lines, _ = ocr_region(left_x, top_y, right_x, bottom_y)
        full_text = '\n'.join(lines)

        # 条件1：出现答主标记
        if "人赞同了该回答" in full_text or "人赞同" in full_text:
            log.info("  ✓ 检测到「人赞同」，回答已加载")
            return True

        # 条件2：出现回答列表标头
        if "个回答" in full_text:
            log.info("  ✓ 检测到「个回答」，回答已加载")
            return True

        # 条件3：文字行数显著增加
        if len(lines) > baseline_count + 5:
            log.info(f"  ✓ 行数从 {baseline_count} 增加到 {len(lines)}，回答已加载")
            return True

        elapsed = int(time.time() - start)
        log.info(f"  [{elapsed}s] 等待回答加载... 当前 {len(lines)} 行")

    log.warning(f"  回答加载超时（{max_wait}s），继续尝试")
    return False

# ============================================================
# 推荐页问题解析
# ============================================================

_TITLE_NOISE = {'飙升', '问题', '推荐理由', '操作', '稍后答', '写回答',
                '小说', '短篇小说', '古言', '现言', '言情', '爽文'}

_RECOMMEND_NOISE_RE = re.compile(
    r'(你在「|话题下获得|个赞同|你关注的|回答了该问题|试试帮|她解答)'
)

def _is_metrics_line(text):
    return '浏览' in text and ('回答' in text or '关注' in text)

def _is_valid_title(text):
    s = text.strip()
    if len(s) < 4:
        return False
    if s in _TITLE_NOISE:
        return False
    if _RECOMMEND_NOISE_RE.search(s):
        return False
    if s.replace(',', '').replace(' ', '').replace('·', '').isdigit():
        return False
    if _is_metrics_line(s):
        return False
    if re.search(r'\d+\s*(天|月|年)前的提问', s):
        return False
    return True

def _parse_chinese_number(s):
    s = s.strip().replace(',', '').replace(' ', '')
    mul = 1
    if '万' in s:
        s = s.replace('万', '')
        mul = 10000
    elif '亿' in s:
        s = s.replace('亿', '')
        mul = 100000000
    try:
        return float(s) * mul
    except ValueError:
        return 0

def _extract_metric(text, keyword):
    pattern = re.compile(r'([\d.]+)\s*万?\s*' + keyword)
    m = pattern.search(text)
    if not m:
        return 0
    num_str = m.group(1)
    segment = text[:m.end()]
    if '万' in segment[segment.rfind(num_str):]:
        return _parse_chinese_number(num_str + '万')
    return _parse_chinese_number(num_str)

def parse_recommend_questions(left_x, top_y, right_x, bottom_y):
    """
    OCR 推荐页左栏，解析每个问题卡片。
    从指标行往上搜索标题，跳过"飙升"标签。
    飙升优先：有飙升只返回飙升，无飙升返回全部。
    """
    left_col_right = left_x + int((right_x - left_x) * 0.55)
    blocks = ocr_region_raw(left_x, top_y, left_col_right, bottom_y)
    if not blocks:
        return []

    log.info(f"  左栏 OCR: {len(blocks)} 个文字块")
    questions = []

    for i, (text, cx, cy) in enumerate(blocks):
        if not _is_metrics_line(text):
            continue

        metrics_text = text
        metrics_y = cy
        is_hot = False
        title = ''
        title_y = cy
        title_x = cx

        for j in range(i - 1, max(i - 5, -1), -1):
            prev_text, prev_cx, prev_cy = blocks[j]
            if metrics_y - prev_cy > 100:
                break
            if '飙升' in prev_text:
                is_hot = True
                continue
            if not _is_valid_title(prev_text):
                continue
            title = prev_text.strip()
            title_y = prev_cy
            title_x = prev_cx
            break

        if not title:
            continue

        views = _extract_metric(metrics_text, '浏览')
        answers = _extract_metric(metrics_text, '回答')
        followers = _extract_metric(metrics_text, '关注')
        score = (views * (followers + 1)) / (answers + 1)

        questions.append({
            'title': title,
            'views': views,
            'answers': answers,
            'followers': followers,
            'is_hot': is_hot,
            'score': score,
            'click_x': int(title_x),
            'click_y': int(title_y),
        })

    hot = [q for q in questions if q['is_hot']]
    pool = hot if hot else questions
    pool.sort(key=lambda q: q['score'], reverse=True)
    return pool

# ============================================================
# DeepSeek 完成检测 + 复制
# ============================================================

def wait_for_deepseek_complete(left_x, top_y, right_x, bottom_y,
                                poll_interval=5, stable_count=2, max_wait=360):
    log.info(f"轮询检测（每{poll_interval}s，连续{stable_count}次稳定，上限{max_wait}s）...")
    prev_text = ""
    stable = 0
    start = time.time()
    while time.time() - start < max_wait:
        time.sleep(poll_interval)
        lines, _ = ocr_region(left_x, top_y, right_x, bottom_y)
        current_text = '\n'.join(lines[-10:])
        elapsed = int(time.time() - start)
        log.info(f"  [{elapsed}s] 行数={len(lines)}")
        if current_text and current_text == prev_text:
            stable += 1
            log.info(f"  稳定 ({stable}/{stable_count})")
            if stable >= stable_count:
                log.info("  ✓ 生成完成！")
                return True
        else:
            stable = 0
        prev_text = current_text
    log.warning(f"  超时（{max_wait}s）")
    return False

def copy_deepseek_by_position(copy_btn_x, copy_btn_y):
    """用校准坐标点击 DeepSeek 复制图标按钮"""
    from config import random_mouse_duration, WAIT_DS_SCROLL_END, WAIT_DS_COPY_CLICK
    import pyperclip

    log.info("滚到回复底部...")
    pyautogui.press('end')
    time.sleep(WAIT_DS_SCROLL_END)

    log.info(f"点击复制按钮 → ({copy_btn_x}, {copy_btn_y})")
    pyautogui.click(copy_btn_x, copy_btn_y, duration=random_mouse_duration())
    time.sleep(WAIT_DS_COPY_CLICK)

    content = pyperclip.paste()
    if content and len(content) > 100:
        log.info(f"  复制成功：{len(content)} 字符")
        return True
    else:
        log.warning(f"  剪贴板内容不足（{len(content or '')}字符）")
        return False

# ============================================================
# 知乎内容提取
# ============================================================

def split_first_page(lines):
    question_end = len(lines)
    answer_start = len(lines)
    for i, line in enumerate(lines):
        if "关注问题" in line:
            question_end = i
            break
        if "写回答" in line and i > 0:
            question_end = i
            break
    for i, line in enumerate(lines):
        if "人赞同了该回答" in line or re.search(r'等\s*\d+\s*人赞同', line):
            answer_start = i + 1
            break
        if "人赞同" in line and i > question_end:
            answer_start = i + 1
            break
    return lines[:question_end], lines[answer_start:]

def extract_question_title(q_lines):
    noise = {'关注', '回答', '邀请回答', '好问题', '写回答',
             '添加评论', '分享', '收藏', '举报', '登录',
             '知乎', '首页', '会员', '发现', '等待',
             '关注问题', '被浏览', '显示全部', '小说', '短篇小说',
             '古言', '现言', '知势榜', '故事'}
    candidates = [l.strip() for l in q_lines
                  if len(l.strip()) >= 4
                  and l.strip() not in noise
                  and not l.strip().replace(',','').replace(' ','').isdigit()]
    return max(candidates, key=len) if candidates else ''

def scroll_and_ocr_answer(left_x, right_x, top_y, bottom_y,
                           first_page_answer_lines=None, max_scrolls=20):
    from config import WAIT_PAGE_DOWN

    all_lines = list(first_page_answer_lines or [])
    prev = list(first_page_answer_lines or [])
    no_new = 0
    ended = False

    end_found, end_idx = _check_lines_for_end(all_lines)
    if end_found:
        all_lines = all_lines[:end_idx]
        ended = True

    if not ended:
        for idx in range(max_scrolls):
            pyautogui.press('pagedown')
            time.sleep(WAIT_PAGE_DOWN)
            log.info(f"  第 {idx+1}/{max_scrolls} 屏...")

            cur, _ = ocr_region(left_x, top_y, right_x, bottom_y)
            if not cur:
                no_new += 1
                if no_new >= 2:
                    break
                continue

            end_found, end_idx = _check_lines_for_end(cur)
            new = _deduplicate_lines(prev, cur)
            if not new:
                no_new += 1
                if no_new >= 2:
                    break
            else:
                no_new = 0
                if end_found:
                    for i, line in enumerate(new):
                        if _is_answer_end_marker(line):
                            all_lines.extend(new[:i])
                            ended = True
                            break
                    else:
                        ended = True
                else:
                    all_lines.extend(new)

            if ended:
                log.info("  回答结束")
                break
            prev = cur

    return '\n'.join(_clean_content(all_lines))

def extract_zhihu_question_and_answer(left_x, right_x, top_y, bottom_y,
                                       min_length=500, max_retries=3):
    from config import OCR_MAX_SCROLLS, WAIT_EXPAND_CLICK, WAIT_SCROLL_NEXT_ANSWER

    log.info("OCR 第一屏...")
    first_lines, _ = ocr_region(left_x, top_y, right_x, bottom_y)
    if not first_lines:
        return '', ''

    q_lines, a_lines = split_first_page(first_lines)
    title = extract_question_title(q_lines)
    log.info(f"  标题：{title[:50] if title else '(未识别到)'}")

    region = (left_x, top_y, right_x - left_x, bottom_y - top_y)
    expand = find_text_on_screen("展开阅读全文", region=region)
    if not expand:
        expand = find_text_on_screen("阅读全文", region=region)
    if expand:
        from config import random_mouse_duration
        log.info("点击展开全文...")
        pyautogui.click(expand[0], expand[1], duration=random_mouse_duration())
        time.sleep(WAIT_EXPAND_CLICK)
        first_lines2, _ = ocr_region(left_x, top_y, right_x, bottom_y)
        if first_lines2:
            _, a_lines = split_first_page(first_lines2)

    for attempt in range(max_retries):
        log.info(f"  提取第 {attempt+1} 个回答...")
        answer = scroll_and_ocr_answer(
            left_x, right_x, top_y, bottom_y,
            first_page_answer_lines=a_lines,
            max_scrolls=OCR_MAX_SCROLLS
        )
        if len(answer) >= min_length:
            log.info(f"  回答合格：{len(answer)} 字符")
            return title, answer
        else:
            log.warning(f"  回答过短（{len(answer)}字符），跳过")
            if attempt < max_retries - 1:
                log.info("  寻找下一个回答...")
                found = False
                for _ in range(10):
                    pyautogui.press('pagedown')
                    time.sleep(WAIT_SCROLL_NEXT_ANSWER)
                    cur, _ = ocr_region(left_x, top_y, right_x, bottom_y)
                    for line in cur:
                        if "人赞同了该回答" in line or re.search(r'等\s*\d+\s*人赞同', line):
                            found = True
                            break
                    if found:
                        _, a_lines = split_first_page(cur)
                        expand2 = find_text_on_screen("展开阅读全文", region=region)
                        if expand2:
                            from config import random_mouse_duration
                            pyautogui.click(expand2[0], expand2[1],
                                            duration=random_mouse_duration())
                            time.sleep(WAIT_EXPAND_CLICK)
                            cur2, _ = ocr_region(left_x, top_y, right_x, bottom_y)
                            if cur2:
                                _, a_lines = split_first_page(cur2)
                        break
                if not found:
                    break

    log.warning(f"  连续 {max_retries} 个回答不合格")
    return title, answer

# ============================================================
# 去重 + 清洗
# ============================================================

def _deduplicate_lines(prev, curr):
    if not prev or not curr:
        return curr
    tail = prev[-min(len(prev), 8):]
    best = 0
    for overlap in range(1, min(len(tail), len(curr)) + 1):
        if all(_fuzzy_match(p, c) for p, c in zip(tail[-overlap:], curr[:overlap])):
            best = overlap
    if best > 0:
        return curr[best:]
    for i, cl in enumerate(curr):
        if any(_fuzzy_match(cl, pl) for pl in tail):
            rest = curr[i+1:]
            return rest if rest else []
    return curr

def _fuzzy_match(a, b, threshold=0.7):
    if a == b:
        return True
    if not a or not b:
        return False
    longer = max(len(a), len(b))
    matches = sum(1 for ca, cb in zip(a, b) if ca == cb)
    return (matches / longer) >= threshold

def _clean_content(lines):
    noise_exact = {
        '赞同', '添加评论', '分享', '收藏', '举报', '喜欢',
        '写回答', '邀请回答', '关注问题', '好问题',
        '查看全部', '更多回答', '相关推荐', '下载知乎',
        '赞', '踩', '登录', '关注', '已关注', '+关注',
        '展开阅读全文', '收起', '显示全部',
    }
    noise_re = [
        re.compile(r'^\d+\s*(条评论|人赞同|万浏览|个回答)'),
        re.compile(r'^(发布于|编辑于)'),
        re.compile(r'^(首页|会员|发现|等你来答|默认排序|时间排序)'),
        re.compile(r'^\d+$'),
    ]
    filtered = []
    for line in lines:
        s = line.strip().replace('\u200b', '').replace('\u200c', '')
        if not s or len(s) < 2:
            continue
        if s in noise_exact:
            continue
        if any(p.search(s) for p in noise_re):
            continue
        filtered.append(s)
    deduped = []
    for line in filtered:
        if deduped and _fuzzy_match(line, deduped[-1], threshold=0.8):
            continue
        deduped.append(line)
    return deduped
