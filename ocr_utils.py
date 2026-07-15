# ============================================================
# ocr_utils.py v2.2
#
# 通用 OCR 感知原语（框架层）
# 知乎专用感知函数已迁移至 applications/zhihu_story/perception.py
# 所有等待时间统一引用 config.py
# ============================================================

import pyautogui
import numpy as np
import re
import time
import os
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


def _parse_interaction_number(num_str, unit):
    """把数字字符串 + 可选的 '万'/'k' 单位转成 int"""
    try:
        n = float(num_str)
    except (ValueError, TypeError):
        return 0
    if unit in ('万',):
        n *= 10000
    elif unit in ('k', 'K'):
        n *= 1000
    return int(n)


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
# OCR 查找
# ============================================================

def find_text_on_screen(target_text, region=None):
    """
    OCR 全屏（或指定区域）查找目标文字，返回 (cx, cy) 或 None。

    两阶段匹配策略：
      1. 快速路径：逐块子串匹配（单块包含目标文字时直接命中）
      2. 空间合并：将 y 坐标接近的块合并为"行"后再做子串匹配
         解决 OCR 引擎将同一行文字拆成多个块的问题
         （如 "Gemini 3 Flash [原生]" 被拆成 "Gemini" + "3 Flash" + "[原生]"）

    匹配时忽略空格，提高对 OCR 插入/丢失空格的容错性。
    """
    engine = _get_engine()
    screenshot = pyautogui.screenshot(region=region)
    result, _ = engine(np.array(screenshot))
    if not result:
        return None

    offset_x = region[0] if region else 0
    offset_y = region[1] if region else 0
    target_nospace = target_text.replace(" ", "")

    # === 阶段1：逐块匹配（快速路径）===
    for box, text, score in result:
        if target_nospace in text.replace(" ", ""):
            cx = sum(p[0] for p in box) / 4 + offset_x
            cy = sum(p[1] for p in box) / 4 + offset_y
            return (int(cx), int(cy))

    # === 阶段2：空间合并后匹配 ===
    blocks = []
    for box, text, score in result:
        bcx = sum(p[0] for p in box) / 4
        bcy = sum(p[1] for p in box) / 4
        blocks.append((bcy, bcx, text, box))

    blocks.sort(key=lambda b: (b[0], b[1]))
    y_threshold = 15
    lines = []
    cur_line = [blocks[0]]
    cur_y = blocks[0][0]

    for i in range(1, len(blocks)):
        bcy, bcx, text, box = blocks[i]
        if abs(bcy - cur_y) <= y_threshold:
            cur_line.append(blocks[i])
        else:
            lines.append(cur_line)
            cur_line = [blocks[i]]
            cur_y = bcy
    lines.append(cur_line)

    for line_blocks in lines:
        line_blocks.sort(key=lambda b: b[1])
        merged_text = ''.join(b[2] for b in line_blocks)
        merged_nospace = merged_text.replace(" ", "")

        if target_nospace in merged_nospace:
            avg_cx = sum(b[1] for b in line_blocks) / len(line_blocks) + offset_x
            avg_cy = sum(b[0] for b in line_blocks) / len(line_blocks) + offset_y
            log.info(f"  [空间合并] 匹配到「{target_text}」← 合并行「{merged_text[:50]}」")
            return (int(avg_cx), int(avg_cy))

    return None

# ============================================================
# 通用数字解析
# ============================================================

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

# ============================================================
# 通用图标匹配（供 Web 驱动调用）
# ============================================================

def _match_icon_on_screen(icon_image, min_confidence=0.65):
    """
    用 OpenCV 灰度 + 多尺度模板匹配在屏幕上定位图标。

    相比 pyautogui.locateOnScreen 的优势：
    - 灰度匹配：消除子像素渲染 / ClearType / 背景色差异
    - 多尺度：处理 DPI 缩放不一致（±25% 范围）
    - 可控阈值：直接拿到 matchTemplate 的相似度数值

    参数：
        icon_image: PIL.Image 对象
        min_confidence: 最低匹配阈值（0-1，推荐 0.65）

    返回：
        匹配成功: (center_x, center_y, confidence, scale)
        匹配失败: None
    """
    try:
        import cv2
    except ImportError:
        log.warning("  opencv-python 未安装，无法使用高级图标匹配")
        log.warning("  请运行：pip install opencv-python")
        return None

    # 截屏 → 灰度
    screenshot = pyautogui.screenshot()
    screen_np = np.array(screenshot)
    screen_gray = cv2.cvtColor(screen_np, cv2.COLOR_RGB2GRAY)

    # 模板 → 灰度
    template_np = np.array(icon_image.convert('RGB'))
    template_gray = cv2.cvtColor(template_np, cv2.COLOR_RGB2GRAY)
    th, tw = template_gray.shape[:2]

    best_val = -1
    best_loc = None
    best_scale = 1.0

    # 多尺度搜索（覆盖 75%~125%，处理 DPI 差异）
    for scale in [1.0, 0.95, 1.05, 0.9, 1.1, 0.85, 1.15, 0.8, 1.2, 0.75, 1.25]:
        new_w = int(tw * scale)
        new_h = int(th * scale)
        if new_w < 5 or new_h < 5:
            continue
        if new_h > screen_gray.shape[0] or new_w > screen_gray.shape[1]:
            continue

        resized = cv2.resize(template_gray, (new_w, new_h), interpolation=cv2.INTER_AREA)
        result = cv2.matchTemplate(screen_gray, resized, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        if max_val > best_val:
            best_val = max_val
            best_loc = max_loc
            best_scale = scale

    log.info(f"  模板匹配最佳：confidence={best_val:.3f} scale={best_scale:.2f}")

    if best_val >= min_confidence and best_loc:
        cx = best_loc[0] + int(tw * best_scale / 2)
        cy = best_loc[1] + int(th * best_scale / 2)
        return (cx, cy, best_val, best_scale)

    return None

# ============================================================
# 去重 + 模糊匹配
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

# ============================================================
# 向后兼容：重导出知乎专用感知函数
# ============================================================
from applications.zhihu_story.perception import *
