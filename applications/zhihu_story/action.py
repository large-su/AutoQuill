"""知乎专用操作函数 — 坐标边界获取。

架构位置：Application Layer — zhihu_story

所有对 desktop_utils/ocr_utils 的引用均为函数内懒导入，避免循环依赖。
"""


def get_bounds():
    """获取 OCR 内容区域的四个边界坐标"""
    from desktop_utils import get_coord

    lx, _ = get_coord("ocr_content_left")
    rx, _ = get_coord("ocr_content_right")
    _, ty = get_coord("ocr_content_top")
    _, by = get_coord("ocr_content_bottom")
    return lx, rx, ty, by
