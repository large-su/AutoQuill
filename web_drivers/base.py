# ============================================================
# web_drivers/base.py — Web LLM 驱动基类 v2.1
#
# v2.1 更新：
#   - wait_complete() 改为抽象方法，子类各自实现
#   - 新增 _ocr_current_screen() / _check_completion_icon() 工具方法
#   - focus_input / copy_result 仍为默认实现
#
# 生命周期：
#   open_session → focus_input → paste → send → wait_complete → copy_result
# ============================================================

import pyautogui
import pyperclip
import numpy as np
import time
import os
import json
import logging

log = logging.getLogger(__name__)


class WebLLMDriver:
    """
    Web LLM 驱动基类。

    默认实现了大部分交互流程（聚焦输入框、复制结果），
    但 wait_complete() 需要子类各自实现——不同网站的生成行为差异较大：
      - 有的自动滚动跟随输出（如 DeepSeek）
      - 有的页面不动需要手动翻页（如 Aizex）
      - 完成标志也因网站而异

    新增网站时需实现：setup() + wait_complete()
    """

    name = "base"

    def __init__(self, config):
        self.config = config
        self.url = config.get("url", "")
        self._session_url = None
        self._tab_opened = False
        self._project_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )

    # ============================================================
    # 浏览器通用操作
    # ============================================================

    def _open_new_tab(self, url=None):
        pyautogui.hotkey('ctrl', 't')
        time.sleep(1.0)
        if url:
            self._navigate_to(url)

    def _navigate_to(self, url):
        from config import random_delay, WAIT_HOTKEY, WAIT_PAGE_LOAD
        pyautogui.hotkey('ctrl', 'l')
        random_delay(WAIT_HOTKEY)
        pyautogui.hotkey('ctrl', 'a')
        time.sleep(0.15)
        # ★ 剪贴板保护：多次操作后剪贴板可能被锁定，重试机制防止崩溃
        for attempt in range(3):
            try:
                pyperclip.copy(url)
                break
            except Exception as e:
                if attempt < 2:
                    log.warning(f"  剪贴板写入失败（{e}），重试 {attempt+2}/3...")
                    time.sleep(0.3)
                else:
                    raise RuntimeError(f"剪贴板连续3次写入失败：{e}") from e
        pyautogui.hotkey('ctrl', 'v')
        random_delay(WAIT_HOTKEY)
        pyautogui.press('enter')
        random_delay(WAIT_PAGE_LOAD)

    def _close_tab(self):
        pyautogui.hotkey('ctrl', 'w')
        time.sleep(0.3)

    def _paste_text(self, text):
        from config import random_delay, WAIT_PASTE
        # ★ 剪贴板保护：防止大文本写入失败导致崩溃
        for attempt in range(3):
            try:
                pyperclip.copy(text)
                break
            except Exception as e:
                if attempt < 2:
                    log.warning(f"  剪贴板写入失败（{e}），重试 {attempt+2}/3...")
                    time.sleep(0.3)
                else:
                    raise RuntimeError(f"剪贴板连续3次写入失败：{e}") from e
        time.sleep(0.15)
        pyautogui.hotkey('ctrl', 'v')
        random_delay(WAIT_PASTE)

    def _grab_url(self):
        pyautogui.hotkey('ctrl', 'l')
        time.sleep(0.2)
        pyautogui.hotkey('ctrl', 'c')
        time.sleep(0.2)
        # ★ 剪贴板保护
        try:
            url = pyperclip.paste()
        except Exception as e:
            log.warning(f"  剪贴板读取失败：{e}")
            url = ""
        pyautogui.press('escape')
        time.sleep(0.1)
        return url

    # ============================================================
    # 发送（默认 Enter，子类可重写）
    # ============================================================

    def send(self):
        pyautogui.press('enter')

    # ============================================================
    # 子类必须实现的方法
    # ============================================================

    def setup(self):
        """首次打开后的初始化（模式切换、模型选择等）"""
        pass

    def wait_complete(self):
        """
        等待生成完成。子类必须实现。

        不同网站行为差异大，不适合用统一逻辑：
        - DeepSeek：页面自动滚动，原地 OCR 检测稳定性
        - Aizex：页面不动，需主动 PageDown + OCR + 图标确认

        可调用基类工具方法：
        - self._ocr_current_screen() → (text, line_count)
        - self._check_completion_icon() → True/False
        """
        raise NotImplementedError(
            f"{self.name} 驱动未实现 wait_complete() 方法"
        )

    # ============================================================
    # 工具方法（供子类 wait_complete 调用）
    # ============================================================

    def _ocr_current_screen(self):
        """
        OCR 当前可见区域，返回 (拼接文本, 行数)。

        取屏幕中央 80% 区域，只用最后 10 行做稳定性比较
        （前面的行可能因滚动而变化，末尾更能反映生成进度）。
        """
        from ocr_utils import ocr_region

        sw, sh = pyautogui.size()
        left = int(sw * 0.1)
        top = int(sh * 0.1)
        right = int(sw * 0.9)
        bottom = int(sh * 0.85)

        lines, _ = ocr_region(left, top, right, bottom)
        text = '\n'.join(lines[-10:])
        return text, len(lines)

    def _check_completion_icon(self):
        """
        检测生成完成标志图标是否出现在屏幕上。

        使用 config 中的 completion_icon 图片做模板匹配。
        该图标应该是生成完成后才出现的操作按钮组合，
        不能是单独的复制按钮（因为问题区域也可能有复制按钮）。

        返回 True（检测到）/ False（未检测到或未配置）
        """
        from ocr_utils import _match_icon_on_screen
        from PIL import Image

        icon_rel = self.config.get("completion_icon", "")
        if not icon_rel:
            return False

        icon_path = os.path.join(self._project_root, icon_rel)
        if not os.path.exists(icon_path):
            return False

        try:
            icon_image = Image.open(icon_path)
        except Exception:
            return False

        match = _match_icon_on_screen(icon_image, min_confidence=0.65)
        if match:
            log.info(f"  完成图标匹配：conf={match[2]:.3f}")
            return True
        return False

    def _ocr_with_positions(self):
        """
        OCR 当前可见区域，返回带坐标的文字块列表。

        与 _ocr_current_screen 不同：此方法保留每个文字块的
        绝对屏幕坐标，用于锚点定位等需要位置信息的场景。

        返回 [(text, abs_x, abs_y), ...]，按 y 坐标排序。
        """
        from ocr_utils import ocr_region_raw

        sw, sh = pyautogui.size()
        return ocr_region_raw(
            int(sw * 0.1), int(sh * 0.1),
            int(sw * 0.9), int(sh * 0.85)
        )

    # ============================================================
    # 聚焦输入框（默认：OCR 占位文字 + 对比度增强）
    # ============================================================

    def focus_input(self):
        """OCR 识别聊天框占位文字并点击，自动增强对比度处理浅色文字。"""
        import cv2
        from ocr_utils import _get_engine
        engine = _get_engine()

        placeholder = self.config.get("chat_placeholder", "")
        if not placeholder:
            log.warning("  未配置 chat_placeholder，跳过输入框聚焦")
            return False

        placeholder_nospace = placeholder.replace(" ", "")
        screenshot = pyautogui.screenshot()
        img_array = np.array(screenshot)

        gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
        _, enhanced = cv2.threshold(gray, 230, 255, cv2.THRESH_BINARY)
        enhanced_rgb = cv2.cvtColor(enhanced, cv2.COLOR_GRAY2RGB)

        for img, label in [(enhanced_rgb, "增强"), (img_array, "原图")]:
            results = engine(img)
            if results and results[0]:
                for line in results[0]:
                    boxes, text, _conf = line
                    if placeholder_nospace in text.replace(" ", ""):
                        xs = [pt[0] for pt in boxes]
                        ys = [pt[1] for pt in boxes]
                        cx = int(sum(xs) / len(xs))
                        cy = int(sum(ys) / len(ys))
                        pyautogui.click(cx, cy)
                        time.sleep(0.3)
                        log.info(f"  聊天框聚焦（{label}）→ "
                                 f"({cx}, {cy})「{text}」")
                        return True

        log.warning(f"  未找到聊天框占位文字「{placeholder}」，尝试直接粘贴")
        return False

    # ============================================================
    # 复制结果（默认：图标匹配 → 校准坐标 → 手动兜底）
    # ============================================================

    def copy_result(self):
        """复制生成结果，策略链：图标匹配 → 校准坐标 → 手动兜底"""
        cfg = self.config
        wait_scroll = cfg.get("wait_scroll_end", 0.8)
        wait_copy = cfg.get("wait_copy_click", 0.6)

        log.info("尝试图标匹配复制按钮...")
        story = self._copy_by_icon(wait_scroll, wait_copy)
        if story:
            return story

        story = self._copy_by_position(wait_scroll, wait_copy)
        if story:
            return story

        log.warning("自动复制失败，请手动复制。")
        input("手动复制后按 Enter >> ")
        return pyperclip.paste()

    def _copy_by_icon(self, wait_scroll, wait_copy):
        """通过 OpenCV 多尺度模板匹配定位复制图标"""
        from ocr_utils import _match_icon_on_screen
        from config import random_mouse_duration
        from PIL import Image

        icon_rel = self.config.get("copy_icon", "")
        if not icon_rel:
            return None

        icon_path = os.path.join(self._project_root, icon_rel)
        if not os.path.exists(icon_path):
            log.info(f"  未找到参考图片：{icon_path}")
            return None

        try:
            icon_image = Image.open(icon_path)
        except Exception as e:
            log.warning(f"  参考图片读取失败：{e}")
            return None

        pyautogui.press('end')
        time.sleep(wait_scroll)

        match = _match_icon_on_screen(icon_image, min_confidence=0.65)
        if match:
            cx, cy, confidence, scale = match
            log.info(f"  图标匹配成功！({cx}, {cy})  conf={confidence:.3f}")
            pyautogui.click(cx, cy, duration=random_mouse_duration())
            time.sleep(wait_copy)
            content = pyperclip.paste()
            if content and len(content) > 100:
                log.info(f"  复制成功：{len(content)} 字符")
                return content

        log.info("  图标匹配未找到复制按钮")
        return None

    def _copy_by_position(self, wait_scroll, wait_copy):
        """通过校准坐标点击复制按钮"""
        from config import random_mouse_duration

        copy_btn_key = self.config.get(
            "copy_btn_key", f"{self.name.lower()}_copy_btn"
        )
        coords_file = os.path.join(self._project_root, "data", "coordinates.json")
        if not os.path.exists(coords_file):
            return None

        try:
            with open(coords_file, 'r', encoding='utf-8') as f:
                coords = json.load(f).get("coordinates", {})
        except Exception:
            return None

        if copy_btn_key not in coords:
            return None

        copy_x, copy_y = coords[copy_btn_key]
        log.info(f"使用校准点位复制 → ({copy_x}, {copy_y})")

        pyautogui.press('end')
        time.sleep(wait_scroll)
        pyautogui.click(copy_x, copy_y, duration=random_mouse_duration())
        time.sleep(wait_copy)

        content = pyperclip.paste()
        if content and len(content) > 100:
            log.info(f"  复制成功：{len(content)} 字符")
            return content

        pyautogui.scroll(3)
        time.sleep(0.5)
        pyautogui.click(copy_x, copy_y, duration=random_mouse_duration())
        time.sleep(wait_copy)

        content = pyperclip.paste()
        if content and len(content) > 100:
            log.info(f"  复制成功：{len(content)} 字符")
            return content

        log.warning("  校准点位复制失败")
        return None

    # ============================================================
    # 校准支持（子类可重写）
    # ============================================================

    @classmethod
    def get_calibration_keys(cls):
        return {}

    @classmethod
    def get_calibration_hints(cls):
        return {}

    # ============================================================
    # 会话管理
    # ============================================================

    def open_session(self):
        """打开新标签页或复用已有会话
        
        安全机制：若 _session_url 为空（首次使用或被 reset 过），
        强制打开新标签页以避免导航到无效 URL 导致崩溃。
        """
        wait_load = self.config.get("wait_load", 4.0)
        if not self._tab_opened or not self._session_url:
            if self._tab_opened and not self._session_url:
                log.info(f"会话 URL 丢失（可能因 tab 被导航到其他页面），"
                         f"重新打开 {self.name} 新标签页...")
                # 先关闭旧 tab（已被导航到其他页面）
                try:
                    self._close_tab()
                except Exception:
                    pass
                self._tab_opened = False
            log.info(f"首次调用，打开 {self.name} 新标签页...")
            self._open_new_tab(self.url)
            time.sleep(wait_load)
            self._tab_opened = True
            self.setup()
        else:
            log.info(f"复用会话：{self._session_url}")
            self._navigate_to(self._session_url)
            time.sleep(wait_load)

    def close_session(self):
        """关闭会话标签页，重置状态"""
        if self._tab_opened:
            try:
                self._close_tab()
                log.info(f"{self.name} 会话标签页已关闭")
            except Exception:
                pass
        self._session_url = None
        self._tab_opened = False

    # ============================================================
    # 完整生成流程编排
    # ============================================================

    def generate(self, prompt):
        """
        完整的 Web 生成流程：
        打开/复用会话 → 聚焦输入框 → 粘贴 → 发送 → 等待完成 → 复制结果
        
        ★ 修复：每次生成后刷新 _session_url，确保后续复用指向正确的对话。
        避免因 tab 被外部导航后，_session_url 仍指向过期地址导致状态混乱。
        """
        self.open_session()
        
        # ★ 聚焦输入框，失败时尝试备用策略
        if not self.focus_input():
            log.warning("  输入框聚焦失败，尝试点击页面底部并重试...")
            # 备用：滚动到底部后点击中心偏下位置（DeepSeek 输入框通常在底部）
            pyautogui.press('end')
            time.sleep(0.5)
            sw, sh = pyautogui.size()
            pyautogui.click(int(sw * 0.5), int(sh * 0.85))
            time.sleep(0.3)
            # 再次尝试聚焦
            if not self.focus_input():
                log.warning("  输入框聚焦再次失败，将尝试直接粘贴（可能失败）")

        log.info("粘贴 prompt 并发送...")
        self._paste_text(prompt)
        time.sleep(self.config.get("wait_after_paste", 0.5))

        self.send()
        time.sleep(self.config.get("wait_after_send", 1.5))
        log.info("已发送，等待生成...")

        # ★ 始终刷新会话 URL（而非仅首次缓存）
        # 在多轮 --single 模式下，外部 reset_driver() 会将 _session_url 置 None，
        # 此处重新抓取确保 URL 正确；在复用场景下刷新也不会有副作用
        try:
            time.sleep(self.config.get("wait_before_url_cache", 3))
            fresh_url = self._grab_url()
            if fresh_url and self.name.lower() in fresh_url.lower():
                self._session_url = fresh_url
                log.info(f"  会话 URL 已缓存：{self._session_url[:80]}...")
            elif not self._session_url:
                log.warning(f"  抓取到的 URL 不匹配 {self.name}，"
                            f"使用备用地址：{self.url}")
                self._session_url = self.url
        except Exception as e:
            log.warning(f"  会话 URL 缓存失败（{e}），使用备用地址")
            if not self._session_url:
                self._session_url = self.url

        self.wait_complete()

        story = self.copy_result()
        if story and len(story) > 100:
            log.info(f"  ✓ Web 生成完成：{len(story)} 字符")
            return story

        log.error("Web 生成/复制失败")
        return None
