# ============================================================
# core/story_workspace.py — 故事工作区（文件系统持久化）
#
# 为长文模式提供 crash-safe 的文件读写管理。
# 每个故事在 data/stories/{story_id}/ 下拥有独立目录。
#
# 用法：
#   ws = StoryWorkspace()           # 新故事，自动生成 story_id
#   ws = StoryWorkspace(story_id)   # 恢复已有故事
#   ws = StoryWorkspace(task_id=5)  # 并行模式，用 task_id 做 story_id
# ============================================================

import json
import os
import logging

log = logging.getLogger(__name__)

# ============================================================
# 配置
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_story_root():
    from config import STORY_OUTPUT_DIR
    return os.path.join(SCRIPT_DIR, STORY_OUTPUT_DIR)


# ============================================================
# StoryWorkspace
# ============================================================


class StoryWorkspace:
    """故事工作区：管理一个长故事的持久化文件。"""

    def __init__(self, story_id=None, task_id=None):
        """
        参数：
            story_id: 直接指定目录名（用于 --resume 恢复）
            task_id:  并行模式下用任务编号做目录名
            两者都为 None 时自动生成 YYYYMMDD_HHMMSS 格式的 story_id
        """
        root = _get_story_root()

        if story_id:
            self.story_id = story_id
        elif task_id is not None:
            self.story_id = f"task_{task_id:03d}"
        else:
            from datetime import datetime
            self.story_id = datetime.now().strftime("%Y%m%d_%H%M%S")

        self._dir = os.path.join(root, self.story_id)
        self._init_dirs()

    def _init_dirs(self):
        """创建目录结构。"""
        for sub in ["chapters", "history"]:
            os.makedirs(os.path.join(self._dir, sub), exist_ok=True)

    # ============================================================
    # 路径工具
    # ============================================================

    def _path(self, *parts):
        return os.path.join(self._dir, *parts)

    def _read(self, *parts):
        p = self._path(*parts)
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            return f.read()

    def _write(self, text, *parts):
        p = self._path(*parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def _exists(self, *parts):
        return os.path.exists(self._path(*parts))

    # ============================================================
    # 进度文件
    # ============================================================

    @property
    def progress(self):
        """读取 _progress.json → dict。不存在返回 None。"""
        raw = self._read("_progress.json")
        if raw is None:
            return None
        return json.loads(raw)

    @progress.setter
    def progress(self, data):
        """写入 _progress.json。"""
        self._write(json.dumps(data, ensure_ascii=False, indent=2),
                    "_progress.json")

    # ============================================================
    # Layer 1：故事基石
    # ============================================================

    @property
    def foundation(self):
        return self._read("foundation.md")

    @foundation.setter
    def foundation(self, text):
        self._write(text, "foundation.md")
        log.info(f"[Workspace] foundation.md 已保存 ({len(text)} 字符)")

    # ============================================================
    # Layer 2：批量大纲（每批覆盖）
    # ============================================================

    @property
    def batch_outline(self):
        return self._read("batch_outline.md")

    @batch_outline.setter
    def batch_outline(self, text):
        self._write(text, "batch_outline.md")
        log.info(f"[Workspace] batch_outline.md 已保存 ({len(text)} 字符)")

    # ============================================================
    # Layer 3：章节
    # ============================================================

    def chapter(self, n):
        """读取第 n 章文本。n 为 int。"""
        return self._read("chapters", f"{n:02d}.md")

    def save_chapter(self, n, text):
        """写入第 n 章文本。"""
        self._write(text, "chapters", f"{n:02d}.md")
        log.info(f"[Workspace] 第{n}章已保存 ({len(text)} 字符)")

    # ============================================================
    # 快照
    # ============================================================

    def snapshot_foundation(self, label="v1"):
        text = self.foundation
        if text:
            self._write(text, "history", f"foundation_{label}.md")
            log.info(f"[Workspace] foundation 快照已存 → history/foundation_{label}.md")

    # ============================================================
    # 前一批章节全文（用于下一批大纲和写作的上下文）
    # ============================================================

    def previous_batch_text(self, up_to_chapter):
        """
        返回最近一批已写章节的全文。

        up_to_chapter: 已写的最后一章编号。
        如果是第一批（up_to_chapter <= 0），返回占位提示文本。
        """
        if up_to_chapter <= 0:
            return "（故事开始，无前文）"

        last = min(up_to_chapter, self._max_chapter_saved())
        if last <= 0:
            return "（故事开始，无前文）"

        # 返回本批全部章节全文（最多 BATCH_CHAPTER_COUNT 章）
        try:
            from config import BATCH_CHAPTER_COUNT
        except ImportError:
            BATCH_CHAPTER_COUNT = 5

        first = max(1, last - BATCH_CHAPTER_COUNT + 1)
        parts = []
        for n in range(first, last + 1):
            ch = self.chapter(n)
            if ch:
                parts.append(f"## 第{n}章\n\n{ch}")
            else:
                parts.append(f"## 第{n}章\n\n[未找到]")

        return "\n\n".join(parts)

    def _max_chapter_saved(self):
        """返回已保存的最大章节号。"""
        ch_dir = os.path.join(self._dir, "chapters")
        if not os.path.exists(ch_dir):
            return 0
        max_n = 0
        for fname in os.listdir(ch_dir):
            if fname.endswith(".md") and fname[:2].isdigit():
                n = int(fname[:2])
                if n > max_n:
                    max_n = n
        return max_n

    # ============================================================
    # 拼接
    # ============================================================

    def assemble(self):
        """拼接全部章节 → 返回完整故事文本。"""
        max_n = self._max_chapter_saved()
        parts = []
        for n in range(1, max_n + 1):
            ch = self.chapter(n)
            if ch:
                parts.append(ch)

        return "\n\n".join(parts)

    # ============================================================
    # 交付物输出
    # ============================================================

    def export_final(self):
        """生成最终交付文档到 story 根目录。"""
        full_story = self.assemble()

        self._write(full_story, "完整故事.md")
        log.info(f"[Workspace] 完整故事.md 已输出 ({len(full_story)} 字符)")

        fb = self.foundation
        if fb:
            self._write(fb, "故事设计.md")
            log.info("[Workspace] 故事设计.md 已输出")

        # 拼接所有批量大纲
        outline_parts = []
        try:
            history_dir = os.path.join(self._dir, "history")
            if os.path.exists(history_dir):
                for fname in sorted(os.listdir(history_dir)):
                    if fname.startswith("outline_batch") and fname.endswith(".md"):
                        ot = self._read("history", fname)
                        if ot:
                            outline_parts.append(ot)
        except OSError as e:
            log.warning(f"[Workspace] 无法读取 history 目录：{e}")

        if outline_parts:
            self._write("\n\n---\n\n".join(outline_parts), "完整大纲.md")
            log.info("[Workspace] 完整大纲.md 已输出")

        return full_story

    # ============================================================
    # 批量大纲快照
    # ============================================================

    def snapshot_outline(self, batch_num):
        text = self.batch_outline
        if text:
            self._write(text, "history", f"outline_batch{batch_num}.md")
            log.info(f"[Workspace] 大纲快照已存 → history/outline_batch{batch_num}.md")
