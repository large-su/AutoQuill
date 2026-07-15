"""知乎专用感知函数 — 回答检测、Footer 解析、推荐页解析、内容提取。

架构位置：Application Layer — zhihu_story
对应文档：agent_framework_architecture_manifesto.md

所有对 ocr_utils 通用原语的引用均为函数内懒导入，避免循环依赖。
"""

import re
import time
import logging

import pyautogui
import numpy as np

log = logging.getLogger(__name__)

# 显式 __all__ 确保 ocr_utils 的 `from ... import *` 能拉取以下划线开头的名称
__all__ = [
    # 标志检测
    '_END_PATTERN', '_END_TIMESTAMP_PATTERN',
    '_is_answer_end_marker', '_check_lines_for_end',
    # Footer 解析
    '_NUM_WITH_UNIT',
    'parse_likes_only', 'parse_footer_line', 'parse_end_timestamp',
    'extract_footer_from_lines', '_extract_footer_with_fallback',
    'get_likes_action_bounds', 'get_upvote_button_bounds',
    # 回答加载检测
    'wait_for_answer_load',
    # 推荐页解析
    '_TITLE_NOISE', '_RECOMMEND_NOISE_RE', '_HOT_KEYWORDS',
    '_is_metrics_line', '_is_valid_title', '_extract_metric',
    'parse_recommend_questions',
    # 知乎内容提取
    'split_first_page', 'extract_question_title',
    'scroll_and_ocr_answer', 'scroll_and_ocr_answer_fast',
    'extract_zhihu_question_and_answer',
    # 清洗
    '_clean_content',
]

# ============================================================
# 标志检测
# ============================================================

_END_PATTERN = re.compile(
    r'(编辑于|发布于)\s*\d{2,4}[-/年.]\d{1,2}[-/月.]?\d{0,2}'
)

_END_TIMESTAMP_PATTERN = re.compile(
    r'(?:编辑于|发布于)\s*'
    r'(\d{4})[-/年.]\s*(\d{1,2})[-/月.]\s*(\d{1,2})'
    r'(?:\s*日?)?'
    r'(?:\s*(\d{1,2})[:：](\d{1,2}))?'
)


def _is_answer_end_marker(line):
    return bool(_END_PATTERN.search(line))


def _check_lines_for_end(lines):
    for i, line in enumerate(lines):
        if _is_answer_end_marker(line):
            return True, i
    return False, -1


# ============================================================
# Footer 解析（互动数据 + 发表时间）
# ============================================================

_NUM_WITH_UNIT = re.compile(r'(\d+(?:\.\d+)?)\s*(万|k|K)?')


def parse_likes_only(text):
    """只解析赞同数；兼容「赞同 640」「640 赞同」「640 人赞同」等 OCR 形态。"""
    from ocr_utils import _parse_interaction_number

    if not text or '赞同' not in text:
        return None

    normalized = text.replace(',', '').replace('，', '')

    patterns = (
        r'(\d+(?:\.\d+)?)\s*(万|k|K)?\s*(?:人)?\s*赞同',
        r'赞同\s*(\d+(?:\.\d+)?)\s*(万|k|K)?'
        r'(?![\d.])(?!(?:\s*)(?:条评论|条评|评论))',
    )
    for pattern in patterns:
        m = re.search(pattern, normalized)
        if m:
            return _parse_interaction_number(m.group(1), m.group(2))

    return None


def get_likes_action_bounds(left_x, right_x, content_bottom):
    """返回固定赞同操作栏的 OCR 区域，紧贴正文 OCR 区域下方。"""
    _, screen_height = pyautogui.size()
    full_bottom = min(screen_height - 40, content_bottom + 90)
    full_height = max(1, full_bottom - content_bottom)
    return (
        left_x,
        content_bottom,
        right_x,
        content_bottom + round(full_height * 0.8),
    )


def get_upvote_button_bounds(left_x, right_x, content_bottom):
    """返回互动栏左侧「赞同」按钮的数字 OCR 区域。"""
    likes_left, likes_top, _, _ = get_likes_action_bounds(
        left_x, right_x, content_bottom
    )
    return (
        likes_left + 40,
        likes_top + 8,
        likes_left + 160,
        likes_top + 55,
    )


def _ocr_np_image_lines(np_image):
    """对已截取的图像做 OCR，供正文与赞同按钮共用同一帧。"""
    from ocr_utils import _get_engine, _merge_to_lines

    if np_image is None or np_image.size == 0:
        return []
    result, _ = _get_engine()(np_image)
    if not result:
        return []
    result.sort(key=lambda item: (
        sum(p[1] for p in item[0]) / 4,
        sum(p[0] for p in item[0]) / 4
    ))
    return _merge_to_lines(result)


def _capture_answer_page_with_upvote(left_x, right_x, top_y, bottom_y,
                                     scan_likes=True):
    """同帧采集正文与赞同按钮；赞同数锁定后只采集正文。"""
    from ocr_utils import ocr_region

    if not scan_likes:
        body_lines, _ = ocr_region(left_x, top_y, right_x, bottom_y)
        return body_lines, []

    upvote_box = get_upvote_button_bounds(left_x, right_x, bottom_y)
    width = right_x - left_x
    body_height = bottom_y - top_y
    total_height = upvote_box[3] - top_y
    if width <= 0 or body_height <= 0 or total_height <= body_height:
        body_lines, _ = ocr_region(left_x, top_y, right_x, bottom_y)
        return body_lines, []

    screenshot = pyautogui.screenshot(
        region=(left_x, top_y, width, total_height)
    )
    np_image = np.array(screenshot)
    body_lines = _ocr_np_image_lines(np_image[:body_height])

    x1 = upvote_box[0] - left_x
    y1 = upvote_box[1] - top_y
    x2 = upvote_box[2] - left_x
    y2 = upvote_box[3] - top_y
    upvote_lines = _ocr_np_image_lines(np_image[y1:y2, x1:x2])
    return body_lines, upvote_lines


def _extract_upvote_likes(lines):
    """从单独裁切的赞同按钮 OCR 行中提取赞同数。"""
    for line in lines:
        normalized = re.sub(r'赞同+', '赞同', line)
        likes = parse_likes_only(normalized)
        if likes is not None:
            return {'value': likes, 'raw_line': line}

    merged = ' '.join(lines)
    normalized = re.sub(r'赞同+', '赞同', merged)
    likes = parse_likes_only(normalized)
    if likes is not None:
        return {'value': likes, 'raw_line': merged}
    return None


def _merge_upvote_likes(footer, upvote_likes):
    """将独立按钮采集的赞同数作为最终值，保留 footer 的其他字段。"""
    if upvote_likes is None:
        return footer

    result = dict(footer or {})
    result.update({
        'likes': upvote_likes['value'],
        'likes_source': 'upvote_button',
        'raw_likes_line': upvote_likes['raw_line'],
    })
    return result


def parse_footer_line(text):
    """解析 footer 互动行，返回 {likes, comments, collects, hearts} 或 None。"""
    from ocr_utils import _parse_interaction_number

    if not text or '赞同' not in text:
        return None

    zan_start = text.index('赞同') + len('赞同')

    end_pos = len(text)
    for marker in ('分享', '收起', '举报'):
        idx = text.find(marker, zan_start)
        if idx > 0 and idx < end_pos:
            end_pos = idx
    segment = text[zan_start:end_pos]

    matches = list(_NUM_WITH_UNIT.finditer(segment))
    if not matches:
        likes = parse_likes_only(text)
        if likes is None:
            return None
        return {'likes': likes, 'comments': 0, 'collects': 0, 'hearts': 0}

    comment_anchor_idx = -1
    for tag in ('条评论', '条评'):
        pos = segment.find(tag)
        if pos >= 0:
            comment_anchor_idx = pos
            break

    result = {'likes': None, 'comments': None, 'collects': None, 'hearts': None}

    if comment_anchor_idx >= 0:
        before_anchor = [m for m in matches if m.end() <= comment_anchor_idx]
        after_anchor = [m for m in matches if m.start() >= comment_anchor_idx + 2]

        if before_anchor:
            comments_m = before_anchor[-1]
            result['comments'] = _parse_interaction_number(comments_m.group(1), comments_m.group(2))
            if len(before_anchor) >= 2:
                likes_m = before_anchor[-2]
                result['likes'] = _parse_interaction_number(likes_m.group(1), likes_m.group(2))
            else:
                result['likes'] = parse_likes_only(text)
                if result['likes'] is None:
                    result['likes'] = 0

        if after_anchor:
            if len(after_anchor) >= 1:
                c_m = after_anchor[0]
                result['collects'] = _parse_interaction_number(c_m.group(1), c_m.group(2))
            if len(after_anchor) >= 2:
                h_m = after_anchor[1]
                result['hearts'] = _parse_interaction_number(h_m.group(1), h_m.group(2))

        for k in ('likes', 'comments', 'collects', 'hearts'):
            if result[k] is None:
                result[k] = 0
        return result

    keys = ['likes', 'comments', 'collects', 'hearts']
    for i, m in enumerate(matches[:4]):
        result[keys[i]] = _parse_interaction_number(m.group(1), m.group(2))
    for k in keys:
        if result[k] is None:
            result[k] = 0
    return result


def parse_end_timestamp(line):
    """从 '编辑于 2024-07-31 18:43' 这类行解析发表时间，返回 ISO 格式字符串。"""
    if not line:
        return None
    m = _END_TIMESTAMP_PATTERN.search(line)
    if not m:
        return None
    try:
        year = int(m.group(1))
        month = int(m.group(2))
        day = int(m.group(3))
        hour = int(m.group(4)) if m.group(4) else 0
        minute = int(m.group(5)) if m.group(5) else 0
        from datetime import datetime as _dt
        return _dt(year, month, day, hour, minute).isoformat(timespec='minutes')
    except (ValueError, TypeError) as e:
        log.warning(f"footer: 时间戳解析失败 '{line[:50]}' → {e}")
        return None


def extract_footer_from_lines(lines):
    """从 OCR 行中抽取 footer（发表时间 + 互动数据）。"""
    if not lines:
        return None

    date_idx = -1
    for i, line in enumerate(lines):
        if _is_answer_end_marker(line):
            date_idx = i
            break
    if date_idx < 0:
        return None

    date_line = lines[date_idx]
    publish_time = parse_end_timestamp(date_line)

    stats = None
    stats_line = None
    for j in range(date_idx + 1, min(date_idx + 6, len(lines))):
        if '赞同' in lines[j]:
            stats = parse_footer_line(lines[j])
            if stats:
                stats_line = lines[j]
                break

    if publish_time is None and stats is None:
        return None

    result = {
        'publish_time': publish_time,
        'raw_date_line': date_line,
        'raw_stats_line': stats_line,
    }
    if stats:
        result.update(stats)
    else:
        result.update({'likes': None, 'comments': None, 'collects': None, 'hearts': None})
    return result


def _extract_footer_with_fallback(lines, left_x, right_x, top_y, bottom_y):
    """先尝试从当前屏的 lines 提取 footer；互动行没采到就做一次小幅滚动兜底。"""
    from ocr_utils import ocr_region

    footer = extract_footer_from_lines(lines)
    if footer and footer.get('likes') is not None:
        return footer

    log.info("  footer 互动行不在本屏，小幅滚动兜底...")
    try:
        pyautogui.scroll(-3)
        time.sleep(0.4)
        cur2, _ = ocr_region(left_x, top_y, right_x, bottom_y)
        if cur2:
            footer2 = extract_footer_from_lines(cur2)
            if footer2 and footer2.get('likes') is not None:
                return footer2
            if footer and footer2 and footer2.get('publish_time') and not footer.get('publish_time'):
                footer['publish_time'] = footer2['publish_time']
    except Exception as e:
        log.warning(f"  footer 兜底滚动失败（忽略）：{e}")

    return footer


# ============================================================
# 智能检测回答是否已加载
# ============================================================

def wait_for_answer_load(left_x, top_y, right_x, bottom_y,
                          poll_interval=1.0, max_wait=10.0):
    """进入问题页后，反复向下滚动并 OCR 检测，直到回答加载出来。"""
    from ocr_utils import ocr_region

    log.info("触发回答加载...")

    pyautogui.scroll(-5)
    time.sleep(0.5)

    baseline_lines, _ = ocr_region(left_x, top_y, right_x, bottom_y)
    baseline_count = len(baseline_lines)

    start = time.time()
    scroll_count = 0

    while time.time() - start < max_wait:
        time.sleep(poll_interval)

        lines, _ = ocr_region(left_x, top_y, right_x, bottom_y)
        full_text = '\n'.join(lines)

        if "人赞同了该回答" in full_text or "人赞同" in full_text:
            log.info("  ✓ 检测到「人赞同」，回答已加载")
            return True

        if "个回答" in full_text:
            log.info("  ✓ 检测到「个回答」，回答已加载")
            return True

        if len(lines) > baseline_count + 5:
            log.info(f"  ✓ 行数从 {baseline_count} 增加到 {len(lines)}，回答已加载")
            return True

        elapsed = int(time.time() - start)
        log.info(f"  [{elapsed}s] 未检测到回答... 当前 {len(lines)} 行，再滚动一次")

        pyautogui.scroll(-5)
        scroll_count += 1
        time.sleep(0.5)

    log.warning(f"  回答加载超时（{max_wait}s，滚动了 {scroll_count+1} 次），继续尝试")
    return False


# ============================================================
# 推荐页问题解析
# ============================================================

_TITLE_NOISE = {'飙升', '火爆', '热门', '问题', '推荐理由', '操作', '稍后答', '写回答',
                '小说', '短篇小说', '古言', '现言', '言情', '爽文'}

_RECOMMEND_NOISE_RE = re.compile(
    r'(你在「|话题下获得|个赞同|你关注的|回答了该问题|试试帮|她解答)'
)

_HOT_KEYWORDS = {'飙升', '火爆', '热门'}


def _is_metrics_line(text):
    """判断是否为指标行。容忍左侧截断。"""
    has_browse = '浏览' in text or ('览' in text and '回答' in text)
    has_detail = '回答' in text or '关注' in text
    return has_browse and has_detail


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
    if re.match(r'^[分级][\d,]+[）)]?$', s):
        return False
    return True


def _extract_metric(text, keyword):
    """从指标文本中提取数字。容忍左侧截断和常见 OCR 误读，支持万/亿。"""
    from ocr_utils import _parse_chinese_number

    if keyword == '回答':
        keywords = ['回答', '回客', '回舍', '回笞']
    elif keyword == '关注':
        keywords = ['关注', '关汪', '关洼']
    elif keyword == '浏览':
        keywords = ['浏览', '浏贤', '流览', '览']
    else:
        keywords = [keyword]

    pattern = re.compile(r'([\d.]+)\s*[万亿]?\s*(?:' + '|'.join(re.escape(k) for k in keywords) + r')')
    m = pattern.search(text)

    if not m:
        return 0
    num_str = m.group(1)
    segment = text[:m.end()]
    if '亿' in segment[segment.rfind(num_str):]:
        return _parse_chinese_number(num_str + '亿')
    if '万' in segment[segment.rfind(num_str):]:
        return _parse_chinese_number(num_str + '万')
    return _parse_chinese_number(num_str)


def parse_recommend_questions(left_x, top_y, right_x, bottom_y):
    """OCR 推荐页左栏，解析每个问题卡片。飙升优先：有飙升只返回飙升问题，无飙升返回全部。"""
    from ocr_utils import ocr_region_raw

    extend_left = max(left_x - 100, 0)
    left_col_right = left_x + int((right_x - left_x) * 0.55)
    blocks = ocr_region_raw(extend_left, top_y, left_col_right, bottom_y)
    if not blocks:
        return []

    log.info(f"  左栏 OCR: {len(blocks)} 个文字块")

    for idx, (text, cx, cy) in enumerate(blocks):
        tag = " [指标行]" if _is_metrics_line(text) else ""
        tag += " [热门]" if any(kw in text for kw in _HOT_KEYWORDS) else ""
        log.info(f"    块{idx}: Y={int(cy)} X={int(cx)} 「{text[:30]}」{tag}")

    questions = []

    for i, (text, cx, cy) in enumerate(blocks):
        if not _is_metrics_line(text):
            continue

        metrics_text = text
        metrics_y = cy

        nearby = []
        for j in range(i - 1, max(i - 6, -1), -1):
            prev_text, prev_cx, prev_cy = blocks[j]
            if metrics_y - prev_cy > 120:
                break
            if prev_cx < left_x - 20:
                continue
            nearby.append((prev_text, prev_cx, prev_cy, j))

        is_hot = False
        for nb_text, _, _, _ in nearby:
            if any(kw in nb_text for kw in _HOT_KEYWORDS):
                is_hot = True
                break

        title = ''
        title_y = metrics_y
        title_x = cx

        for nb_text, nb_cx, nb_cy, nb_idx in nearby:
            clean = nb_text.strip()

            if clean in _HOT_KEYWORDS:
                continue

            for kw in _HOT_KEYWORDS:
                if clean.startswith(kw):
                    is_hot = True
                    clean = clean[len(kw):].strip()
                    break
            if not clean:
                continue

            if not _is_valid_title(clean):
                continue

            title = clean
            title_y = nb_cy
            title_x = nb_cx
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

        log.info(f"  → {'🔥' if is_hot else '  '} {title[:30]} | "
                 f"{views:.0f}浏览 {answers:.0f}回答 {followers:.0f}关注 "
                 f"score={score:.0f}")

    questions.sort(key=lambda q: q['score'], reverse=True)
    return questions


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
    """从"关注问题"之前的 OCR 行中提取问题标题。"""
    noise = {'关注', '回答', '邀请回答', '好问题', '写回答',
             '添加评论', '分享', '收藏', '举报', '登录',
             '知乎', '首页', '会员', '发现', '等待',
             '关注问题', '被浏览', '显示全部', '知势榜',
             '邀请你回答此问题', '邀请别人', '忽略邀请',
             '关注者', '痴情老人', '修改问题', '好问题',
             '显示全部', '条评论', '编辑回答', '邀请回答'}

    _noise_substrings = ['邀请你回答此问题', '邀请别人', '忽略邀请',
                         '显示全部', '展开阅读全文']

    candidates = []
    for line in q_lines:
        s = line.strip()
        if len(s) < 2:
            continue
        if s in noise:
            continue
        is_ui_noise = False
        for ns in _noise_substrings:
            if ns in s and len(s) < 25:
                is_ui_noise = True
                break
        if is_ui_noise:
            continue
        if re.match(r'^[\d,\s.]+$', s.replace('万', '').replace('亿', '')):
            continue
        if re.search(r'^\d[\d,.]*\s*(关注者|被浏览|个回答|条评论)', s):
            continue
        candidates.append(s)

    if not candidates:
        return ''

    if len(candidates) == 1:
        return candidates[0]

    title_line = None
    title_idx = -1

    for i, s in enumerate(candidates):
        if s.endswith('？') or s.endswith('?'):
            if title_line is None or len(s) > len(title_line):
                title_line = s
                title_idx = i

    if title_line is None:
        title_line = max(candidates, key=len)
        title_idx = candidates.index(title_line)

    if title_line and title_idx > 0:
        prev_idx = title_idx - 1
        prev_line = candidates[prev_idx]
        if (not prev_line.endswith(('？', '?', '！', '!', '。', '.'))
                and len(prev_line) > 5
                and not re.match(r'^[一-鿿]{1,3}(\s+[一-鿿]{1,3})*$', prev_line)):
            title_line = prev_line + title_line
            title_idx = prev_idx

    other_lines = [s for i, s in enumerate(candidates) if i != title_idx]

    result_parts = [title_line]
    if other_lines:
        result_parts.extend(other_lines)

    return '\n'.join(result_parts)


def scroll_and_ocr_answer(left_x, right_x, top_y, bottom_y,
                           first_page_answer_lines=None,
                           first_page_upvote_likes=None, max_scrolls=20):
    """滚动 + OCR 提取单篇回答全文。"""
    from config import WAIT_PAGE_DOWN
    from ocr_utils import _deduplicate_lines

    all_lines = list(first_page_answer_lines or [])
    prev = list(first_page_answer_lines or [])
    no_new = 0
    ended = False
    footer = None
    upvote_likes = first_page_upvote_likes

    end_found, end_idx = _check_lines_for_end(all_lines)
    if end_found:
        footer = _extract_footer_with_fallback(all_lines, left_x, right_x, top_y, bottom_y)
        all_lines = all_lines[:end_idx]
        ended = True

    if not ended:
        for idx in range(max_scrolls):
            pyautogui.press('pagedown')
            time.sleep(WAIT_PAGE_DOWN)
            log.info(f"  第 {idx+1}/{max_scrolls} 屏...")

            cur, upvote_lines = _capture_answer_page_with_upvote(
                left_x, right_x, top_y, bottom_y,
                scan_likes=upvote_likes is None
            )
            if upvote_likes is None:
                upvote_likes = _extract_upvote_likes(upvote_lines)
                if upvote_likes is not None:
                    log.info("  ✓ 赞同按钮识别到赞同数：%s（%s）",
                             upvote_likes['value'],
                             upvote_likes['raw_line'][:60])
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
                    footer = _extract_footer_with_fallback(cur, left_x, right_x, top_y, bottom_y)
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

    return ('\n'.join(_clean_content(all_lines)),
            _merge_upvote_likes(footer, upvote_likes))


def scroll_and_ocr_answer_fast(left_x, right_x, top_y, bottom_y,
                                first_page_answer_lines=None, max_scrolls=20):
    """快速版回答提取：截屏缓存 + 并行 OCR。"""
    from config import WAIT_PAGE_DOWN
    from concurrent.futures import ThreadPoolExecutor
    from ocr_utils import _get_engine, _merge_to_lines, _deduplicate_lines

    all_lines = list(first_page_answer_lines or [])
    footer = None

    end_found, end_idx = _check_lines_for_end(all_lines)
    if end_found:
        footer = _extract_footer_with_fallback(all_lines, left_x, right_x, top_y, bottom_y)
        return '\n'.join(_clean_content(all_lines[:end_idx])), footer

    w = right_x - left_x
    h = bottom_y - top_y
    screenshots = []
    prev_pixels = None
    no_change = 0

    scroll_wait = max(WAIT_PAGE_DOWN * 0.6, 0.3)

    log.info(f"  快速截屏中（每页 {scroll_wait:.2f}s）...")
    scroll_start = time.time()

    for idx in range(max_scrolls):
        pyautogui.press('pagedown')
        time.sleep(scroll_wait)

        img = pyautogui.screenshot(region=(left_x, top_y, w, h))
        np_img = np.array(img)

        sampled = np_img[::8, ::8]
        if prev_pixels is not None:
            diff = np.mean(np.abs(sampled.astype(int) - prev_pixels.astype(int)))
            if diff < 3:
                no_change += 1
                if no_change >= 2:
                    log.info(f"    第 {idx+1} 屏无变化，停止")
                    break
            else:
                no_change = 0

        screenshots.append(np_img)
        prev_pixels = sampled

    scroll_time = time.time() - scroll_start
    log.info(f"    缓存 {len(screenshots)} 张截屏（{scroll_time:.1f}s）")

    if not screenshots:
        return '\n'.join(_clean_content(all_lines)), footer

    def _ocr_one(np_img):
        engine = _get_engine()
        result, _ = engine(np_img)
        if not result:
            return []
        result.sort(key=lambda item: (
            sum(p[1] for p in item[0]) / 4,
            sum(p[0] for p in item[0]) / 4
        ))
        return _merge_to_lines(result)

    ocr_start = time.time()
    workers = min(len(screenshots), 4)
    log.info(f"  并行 OCR（{workers} 线程）...")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        ocr_results = list(pool.map(_ocr_one, screenshots))

    ocr_time = time.time() - ocr_start
    log.info(f"    OCR 完成：{ocr_time:.1f}s（{len(screenshots)} 张）")

    prev = list(first_page_answer_lines or [])
    ended = False

    for page_idx, page_lines in enumerate(ocr_results):
        if not page_lines:
            continue

        end_found, _ = _check_lines_for_end(page_lines)
        new = _deduplicate_lines(prev, page_lines)

        if not new:
            continue

        if end_found:
            footer = extract_footer_from_lines(page_lines)
            if (footer is None or footer.get('likes') is None) and page_idx + 1 < len(ocr_results):
                next_page = ocr_results[page_idx + 1]
                if next_page:
                    footer_next = extract_footer_from_lines(next_page)
                    if footer_next and footer_next.get('likes') is not None:
                        if footer and footer.get('publish_time') and not footer_next.get('publish_time'):
                            footer_next['publish_time'] = footer['publish_time']
                        footer = footer_next

            for i, line in enumerate(new):
                if _is_answer_end_marker(line):
                    all_lines.extend(new[:i])
                    ended = True
                    break
            if ended:
                break
        else:
            all_lines.extend(new)

        prev = page_lines

    total = scroll_time + ocr_time
    log.info(f"  快速提取完成：{len(all_lines)} 行（滚屏{scroll_time:.1f}s + OCR{ocr_time:.1f}s = {total:.1f}s）")

    return '\n'.join(_clean_content(all_lines)), footer


def extract_zhihu_question_and_answer(left_x, right_x, top_y, bottom_y,
                                       min_length=500, max_retries=3):
    """提取问题标题 + 合格回答 + footer（互动数据 + 发表时间）。"""
    from config import OCR_MAX_SCROLLS, WAIT_EXPAND_CLICK, WAIT_SCROLL_NEXT_ANSWER
    from ocr_utils import find_text_on_screen

    log.info("OCR 第一屏...")
    first_lines, first_upvote_lines = _capture_answer_page_with_upvote(
        left_x, right_x, top_y, bottom_y
    )
    if not first_lines:
        return '', '', None

    q_lines, a_lines = split_first_page(first_lines)
    first_page_upvote_likes = (
        _extract_upvote_likes(first_upvote_lines) if a_lines else None
    )
    if first_page_upvote_likes is not None:
        log.info("  ✓ 赞同按钮识别到赞同数：%s（%s）",
                 first_page_upvote_likes['value'],
                 first_page_upvote_likes['raw_line'][:60])
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
        first_lines2, upvote_lines2 = _capture_answer_page_with_upvote(
            left_x, right_x, top_y, bottom_y,
            scan_likes=first_page_upvote_likes is None
        )
        if first_lines2:
            _, a_lines = split_first_page(first_lines2)
            if first_page_upvote_likes is None and a_lines:
                first_page_upvote_likes = _extract_upvote_likes(upvote_lines2)
                if first_page_upvote_likes is not None:
                    log.info("  ✓ 赞同按钮识别到赞同数：%s（%s）",
                             first_page_upvote_likes['value'],
                             first_page_upvote_likes['raw_line'][:60])

    answer = ''
    footer = None
    for attempt in range(max_retries):
        log.info(f"  提取第 {attempt+1} 个回答...")
        answer, footer = scroll_and_ocr_answer(
            left_x, right_x, top_y, bottom_y,
            first_page_answer_lines=a_lines,
            first_page_upvote_likes=first_page_upvote_likes,
            max_scrolls=OCR_MAX_SCROLLS
        )
        if len(answer) >= min_length:
            log.info(f"  回答合格：{len(answer)} 字符"
                     f"{'（含 footer）' if footer else '（无 footer）'}")
            return title, answer, footer
        else:
            log.warning(f"  回答过短（{len(answer)}字符），跳过")
            if attempt < max_retries - 1:
                log.info("  寻找下一个回答...")
                found = False
                for _ in range(10):
                    pyautogui.press('pagedown')
                    time.sleep(WAIT_SCROLL_NEXT_ANSWER)
                    cur, upvote_lines = _capture_answer_page_with_upvote(
                        left_x, right_x, top_y, bottom_y
                    )
                    for line in cur:
                        if "人赞同了该回答" in line or re.search(r'等\s*\d+\s*人赞同', line):
                            found = True
                            break
                    if found:
                        _, a_lines = split_first_page(cur)
                        first_page_upvote_likes = (
                            _extract_upvote_likes(upvote_lines)
                            if a_lines else None
                        )
                        if first_page_upvote_likes is not None:
                            log.info("  ✓ 赞同按钮识别到赞同数：%s（%s）",
                                     first_page_upvote_likes['value'],
                                     first_page_upvote_likes['raw_line'][:60])
                        expand2 = find_text_on_screen("展开阅读全文", region=region)
                        if expand2:
                            from config import random_mouse_duration
                            pyautogui.click(expand2[0], expand2[1],
                                            duration=random_mouse_duration())
                            time.sleep(WAIT_EXPAND_CLICK)
                            cur2, upvote_lines2 = _capture_answer_page_with_upvote(
                                left_x, right_x, top_y, bottom_y,
                                scan_likes=first_page_upvote_likes is None
                            )
                            if cur2:
                                _, a_lines = split_first_page(cur2)
                                if first_page_upvote_likes is None and a_lines:
                                    first_page_upvote_likes = _extract_upvote_likes(
                                        upvote_lines2
                                    )
                                    if first_page_upvote_likes is not None:
                                        log.info("  ✓ 赞同按钮识别到赞同数：%s（%s）",
                                                 first_page_upvote_likes['value'],
                                                 first_page_upvote_likes['raw_line'][:60])
                        break
                if not found:
                    break

    log.warning(f"  连续 {max_retries} 个回答不合格")
    return title, answer, footer


# ============================================================
# 清洗
# ============================================================

def _clean_content(lines):
    from ocr_utils import _fuzzy_match

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
        s = line.strip().replace('​', '').replace('‌', '')
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
