# ============================================================
# tools/debug_legacy.py — OCR/UIA 时代的调试命令（自 main.py 原样移出）
#
# 历史：坐标/OCR 时代的排障工具（区域标注、OCR 测试、无障碍树导出）。
# DOM+API 通道为主后不再参与主流程，仅作 CLI 调试保留。
# main.py 的 CLI 分发仍指向这里（--test-ocr/--debug-ocr-region/
# --probe-a11y），行为与移出前完全一致。
#
# 架构位置：Layer 0 (Tools) — 开发期诊断，不在运行时依赖路径上
# ============================================================

import logging
import os
import sys
import time
from datetime import datetime

import pyautogui

log = logging.getLogger(__name__)

# 保证从仓库根之外运行也能导入顶层模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _draw_region(draw, box, color, label):
    """在调试截图上画区域框。"""
    x1, y1, x2, y2 = [int(v) for v in box]
    draw.rectangle((x1, y1, x2, y2), outline=color, width=5)
    draw.rectangle((x1, max(0, y1 - 22), x1 + 260, y1), fill=color)
    draw.text((x1 + 6, max(0, y1 - 20)), label, fill="white")


def _ocr_image_lines(image):
    """对已截取的同一帧图像 OCR，供区域调试比较。"""
    import numpy as np
    from ocr_utils import _get_engine, _merge_to_lines

    result, _ = _get_engine()(np.array(image))
    if not result:
        return []
    result.sort(key=lambda item: (
        sum(p[1] for p in item[0]) / 4,
        sum(p[0] for p in item[0]) / 4
    ))
    return _merge_to_lines(result)


def debug_ocr_region_mode():
    """
    按真实采集流程进入一个知乎回答页，然后保存 OCR 区域可视化截图。

    红框：正文 OCR 区域
    绿框：正文区域下方的候选赞同栏

    同时对绿框原图、2 倍放大图、左侧赞同按钮图 OCR，并输出严格的赞同数解析结果。
    """
    from desktop_utils import load_coords, get_bounds, ensure_edge, focus_edge

    if not load_coords():
        print("  ❌ 请先 --calibrate")
        return

    from ocr_utils import _get_engine
    _get_engine()

    if not ensure_edge():
        print("  ❌ 无法启动 Edge 浏览器，请手动打开后重试。")
        return

    from workflows.zhihu import ZhihuWorkflow

    workflow = ZhihuWorkflow()
    print("  将按当前选题模式进入一个知乎问题页...")
    url = workflow.select_topic()
    print(f"  当前问题页：{url}")

    focus_edge()
    time.sleep(0.5)

    lx, rx, ty, by = get_bounds()
    sw, sh = pyautogui.size()
    from applications.zhihu_story.perception import (
        get_likes_action_bounds, get_upvote_button_bounds
    )

    content_box = (lx, ty, rx, by)
    likes_screen_bottom_box = get_likes_action_bounds(lx, rx, by)

    raw_img = pyautogui.screenshot()
    img = raw_img.copy()
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    _draw_region(draw, content_box, "red", "CONTENT OCR")
    _draw_region(draw, likes_screen_bottom_box, "green", "SCREEN BOTTOM LIKES")

    os.makedirs("screenshots", exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    img_path = os.path.join("screenshots", f"ocr_regions_{stamp}.png")
    txt_path = os.path.join("screenshots", f"ocr_regions_{stamp}.txt")
    likes_raw_path = os.path.join("screenshots", f"ocr_likes_raw_{stamp}.png")
    upvote_raw_path = os.path.join("screenshots", f"ocr_upvote_raw_{stamp}.png")
    img.save(img_path)

    content_lines = _ocr_image_lines(raw_img.crop(content_box))
    likes_raw_img = raw_img.crop(likes_screen_bottom_box)
    likes_raw_img.save(likes_raw_path)
    likes_native_lines = _ocr_image_lines(likes_raw_img)
    likes_2x_img = likes_raw_img.resize(
        (likes_raw_img.width * 2, likes_raw_img.height * 2)
    )
    likes_2x_lines = _ocr_image_lines(likes_2x_img)
    upvote_button_box = get_upvote_button_bounds(lx, rx, by)
    upvote_raw_img = raw_img.crop(upvote_button_box)
    upvote_raw_img.save(upvote_raw_path)
    upvote_lines = _ocr_image_lines(upvote_raw_img)

    from applications.zhihu_story.perception import parse_likes_only
    likes_variants = [
        ("native", likes_native_lines),
        ("2x", likes_2x_lines),
        ("upvote_button", upvote_lines),
    ]

    sections = [
        ("CONTENT OCR", content_box, content_lines),
        ("SCREEN BOTTOM LIKES (native)", likes_screen_bottom_box,
         likes_native_lines),
        ("SCREEN BOTTOM LIKES (2x)", likes_screen_bottom_box,
         likes_2x_lines),
        ("UPVOTE BUTTON (native)", upvote_button_box, upvote_lines),
    ]
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"url: {url}\n")
        f.write(f"screen: {sw}x{sh}\n\n")
        for name, box, lines in sections:
            f.write(f"[{name}] {tuple(int(v) for v in box)}\n")
            for i, line in enumerate(lines, 1):
                f.write(f"{i:02d}. {line}\n")
            f.write("\n")

        f.write("[LIKES PARSE]\n")
        for variant, lines in likes_variants:
            raw_text = " ".join(lines)
            likes = parse_likes_only(raw_text)
            f.write(f"{variant}: {likes if likes is not None else 'NOT_FOUND'}\n")
            f.write(f"  raw: {raw_text}\n")

    print(f"  ✓ 区域截图已保存：{img_path}")
    print(f"  ✓ OCR 文本已保存：{txt_path}")
    print(f"  ✓ 绿框原图已保存：{likes_raw_path}")
    print(f"  ✓ 赞同按钮原图已保存：{upvote_raw_path}")
    for variant, lines in likes_variants:
        likes = parse_likes_only(" ".join(lines))
        label = likes if likes is not None else "未识别"
        print(f"  · 绿框 {variant} OCR 赞同数：{label}")


def test_ocr_mode():
    from desktop_utils import load_coords, get_bounds, focus_edge

    if not load_coords():
        print("  ❌ 请先 --calibrate")
        return

    lx, rx, ty, by = get_bounds()
    print(f"\n  OCR 区域：({lx},{ty})~({rx},{by})")
    print("  请在 Edge 打开知乎问题页。")
    input("  按 Enter 测试...")
    focus_edge()
    time.sleep(0.5)

    from ocr_utils import ocr_region
    from applications.zhihu_story.perception import _is_answer_end_marker
    lines, _ = ocr_region(lx, ty, rx, by)
    for i, l in enumerate(lines):
        marks = []
        if "关注问题" in l:
            marks.append("◀问题结束")
        if "人赞同" in l:
            marks.append("◀回答开始")
        if _is_answer_end_marker(l):
            marks.append("◀回答结束")
        m = f"  {'  '.join(marks)}" if marks else ""
        print(f"  {i+1:2d}. {l}{m}")
    print(f"\n  共 {len(lines)} 行")


def _get_cli_option(argv, option):
    """Return an optional CLI value without introducing a parser dependency."""
    try:
        index = argv.index(option)
    except ValueError:
        return None
    if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
        raise ValueError(f"{option} 需要提供 URL")
    return argv[index + 1]


def probe_a11y_mode(argv):
    """Run the read-only UI Automation probe against the active Edge window."""
    try:
        target_url = _get_cli_option(argv, "--url")
    except ValueError as exc:
        print(f"  ✗ {exc}")
        print("  用法：python main.py --probe-a11y [--url https://www.zhihu.com/...] ")
        return

    from desktop_utils import focus_edge, navigate_to_url

    if not focus_edge():
        print("  ✗ 未找到可聚焦的 Edge 窗口，请先打开 Edge 后重试。")
        return

    if target_url:
        print(f"  通过浏览器常规导航打开：{target_url}")
        navigate_to_url(target_url)

    print("  正在只读枚举当前 Edge 的 Windows 无障碍树，不会点击或写入页面...")
    try:
        from applications.zhihu_story.a11y_probe import probe_foreground_edge
        result = probe_foreground_edge(source_url=target_url)
    except Exception as exc:
        log.error(f"UIA 探针失败：{exc}")
        print(f"  ✗ UIA 探针失败：{exc}")
        return

    print(f"  ✓ UIA 树已导出：{result['path']}")
    print(f"    共读取 {result['node_count']} 个元素"
          f"{'（达到安全上限，结果已截断）' if result['truncated'] else ''}")
    print("    回答状态：" + " | ".join(
        f"{name}={count}" for name, count in result['actions'].items()
    ))
    print(f"    标题候选={result['question_titles']}，"
          f"互动文本/值={result['interactions']}")
