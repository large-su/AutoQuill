"""知乎专用操作函数 — 编辑器等待、坐标边界获取。

架构位置：Application Layer — zhihu_story
对应文档：agent_framework_architecture_manifesto.md

所有对 desktop_utils/ocr_utils 的引用均为函数内懒导入，避免循环依赖。
"""

import time
import pyautogui


def wait_editor_ready(timeout=8.0):
    """等待知乎编辑器就绪（工具栏或编辑区出现标志性文字）。

    返回 True（就绪）/ False（超时）
    """
    from ocr_utils import find_text_on_screen
    from desktop_utils import make_region

    start = time.time()
    sw, sh = pyautogui.size()
    toolbar_region = make_region(sw * 0.45, sh * 0.08, sw * 0.95, sh * 0.28)
    editor_region = make_region(sw * 0.18, sh * 0.18, sw * 0.92, sh * 0.90)

    while time.time() - start < timeout:
        for text in ["导入", "发布", "回答", "写回答"]:
            if find_text_on_screen(text, region=toolbar_region):
                return True
        for text in ["输入", "Markdown", "正文"]:
            if find_text_on_screen(text, region=editor_region):
                return True
        time.sleep(0.5)
    return False


def get_bounds():
    """获取 OCR 内容区域的四个边界坐标"""
    from desktop_utils import get_coord

    lx, _ = get_coord("ocr_content_left")
    rx, _ = get_coord("ocr_content_right")
    _, ty = get_coord("ocr_content_top")
    _, by = get_coord("ocr_content_bottom")
    return lx, rx, ty, by
