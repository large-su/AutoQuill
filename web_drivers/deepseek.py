# ============================================================
# web_drivers/deepseek.py — DeepSeek 网页版驱动
#
# DeepSeek 特性：
#   - 页面自动滚动跟随输出，无需手动翻页
#   - 生成完成检测：原地 OCR 检测文字稳定性
#   - 首次打开需切换模式（快速/专家、深度思考、智能搜索）
# ============================================================

import pyautogui
import time
import logging

from web_drivers.base import WebLLMDriver

log = logging.getLogger(__name__)


class DeepSeekDriver(WebLLMDriver):
    """DeepSeek 网页版驱动"""

    name = "DeepSeek"

    # ============================================================
    # 初始化：模式切换
    # ============================================================

    def setup(self):
        """首次打开后切换模式（快速/专家、深度思考、智能搜索）"""
        from ocr_utils import find_text_on_screen

        cfg = self.config
        mode = cfg.get("mode", "expert")
        deep_think = cfg.get("deep_think", False)
        smart_search = cfg.get("smart_search", False)

        mode_name = "快速模式" if mode == "fast" else "专家模式"
        log.info(f"  设置 DeepSeek：模式={mode_name} "
                 f"深度思考={'开' if deep_think else '关'} "
                 f"智能搜索={'开' if smart_search else '关'}")

        pos = find_text_on_screen(mode_name)
        if pos:
            pyautogui.click(pos[0], pos[1])
            time.sleep(0.5)
            log.info(f"    点击「{mode_name}」→ ({pos[0]}, {pos[1]})")

        if deep_think:
            pos = find_text_on_screen("深度思考")
            if pos:
                pyautogui.click(pos[0], pos[1])
                time.sleep(0.5)
                log.info(f"    点击「深度思考」→ ({pos[0]}, {pos[1]})")

        if smart_search:
            pos = find_text_on_screen("智能搜索")
            if pos:
                pyautogui.click(pos[0], pos[1])
                time.sleep(0.5)
                log.info(f"    点击「智能搜索」→ ({pos[0]}, {pos[1]})")

        log.info("  DeepSeek 模式设置完成")

    # ============================================================
    # 生成完成检测：页面自动滚动，原地 OCR 稳定性检测
    # ============================================================

    def wait_complete(self):
        """
        生成完成检测：

        DeepSeek 页面默认会自动滚动跟随输出，但鼠标误触可能导致滚动中断。
        因此每次 OCR 检测前主动按一次 PageDown 兜底，确保屏幕始终在底部。

        检测逻辑：连续 stable_count 次 OCR 结果不变 → 判定完成。
        """
        import pyautogui as _pg

        cfg = self.config
        poll_interval = cfg.get("poll_interval", 5)
        stable_count = cfg.get("stable_count", 2)
        max_wait = cfg.get("max_wait", 360)
        pagedown_n = cfg.get("pagedown_per_cycle", 1)

        # 初始等待：跳过模型思考静默期
        first_wait = cfg.get("wait_first_reply", 0)
        if first_wait > 0:
            log.info(f"  初始等待 {first_wait}s（模型思考中）...")
            time.sleep(first_wait)

        log.info(f"轮询检测（每{poll_interval}s，"
                 f"每次PageDown×{pagedown_n}，"
                 f"连续{stable_count}次稳定，上限{max_wait}s）...")

        prev_text = ""
        stable = 0
        start = time.time()

        while time.time() - start < max_wait:
            # ★ 主动 PageDown 兜底：防止鼠标误触中断 DeepSeek 的自动滚动
            for _ in range(pagedown_n):
                _pg.press('pagedown')
                time.sleep(0.3)

            time.sleep(poll_interval)

            current_text, line_count = self._ocr_current_screen()
            elapsed = int(time.time() - start)
            log.info(f"  [{elapsed}s] 行数={line_count}")

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

    # ============================================================
    # 校准支持
    # ============================================================

    @classmethod
    def get_calibration_keys(cls):
        return {
            "deepseek_copy_btn": "DeepSeek 回复底部的复制图标按钮"
                                 "（可选，有图标匹配可跳过）",
        }

    @classmethod
    def get_calibration_hints(cls):
        return {
            "deepseek_copy_btn": (
                "在 DeepSeek 中让它生成一段回复，然后将鼠标移到\n"
                "    回复底部左下角的复制图标上（第一个小图标）"
            ),
        }
