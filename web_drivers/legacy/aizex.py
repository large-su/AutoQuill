# ============================================================
# web_drivers/aizex.py — Aizex 网页版驱动
#
# Aizex 特性：
#   - 页面不会自动滚动跟随输出，需主动 PageDown 翻页
#   - 生成完成检测：PageDown → OCR → 稳定性 + 完成图标双重确认
#   - 模型选择通过校准坐标打开下拉菜单 + OCR 定位模型名称
# ============================================================

import pyautogui
import pyperclip
import time
import os
import json
import logging
from datetime import datetime

from web_drivers.base import WebLLMDriver

log = logging.getLogger(__name__)


class AizexDriver(WebLLMDriver):
    """Aizex 网页版驱动"""

    name = "Aizex"

    # ============================================================
    # 初始化：模型选择（校准坐标打开菜单 + OCR 选模型）
    # ============================================================

    def setup(self):
        """首次打开后，从下拉菜单中选择目标模型"""
        from ocr_utils import find_text_on_screen

        cfg = self.config
        target_model = cfg.get("model", "Auto")
        model_menu = cfg.get("model_menu", {})

        log.info(f"  选择模型：{target_model}")

        # ===== 第1步：通过校准坐标点击下拉触发器 =====
        menu_pos = self._get_calibrated_pos("aizex_model_menu")
        if not menu_pos:
            log.warning("  未校准 aizex_model_menu，无法切换模型。"
                        "请运行 --calibrate")
            return

        pyautogui.click(menu_pos[0], menu_pos[1])
        time.sleep(1.0)
        log.info(f"    打开模型菜单（校准坐标）→ "
                 f"({menu_pos[0]}, {menu_pos[1]})")

        # ===== 第2步：查找目标模型的点击路径 =====
        top_level = model_menu.get("_top_level", [])

        # 检查是否为一级模型
        if self._find_in_list(target_model, top_level):
            if self._click_model(target_model):
                pyautogui.click(menu_pos[0], menu_pos[1])  # 新增
                time.sleep(0.5)                              # 新增
                return
            log.warning(f"  一级菜单未找到「{target_model}」")
            self._close_menu()
            return

        # 检查二级菜单
        for category, models in model_menu.items():
            if category == "_top_level":
                continue
            if not isinstance(models, list):
                continue
            if self._find_in_list(target_model, models):
                if self._click_category(category):
                    time.sleep(0.8)
                    if self._click_model(target_model):
                        pyautogui.click(menu_pos[0], menu_pos[1])  # 新增
                        time.sleep(0.5)                              # 新增
                        return
                log.warning(f"  二级菜单「{category}」下"
                            f"未找到「{target_model}」")
                self._close_menu()
                return

        log.warning(f"  模型「{target_model}」"
                    f"未在 model_menu 配置中找到，保持默认")
        self._close_menu()

    # ============================================================
    # 生成完成检测：PageDown 循环 + OCR 稳定性 + 图标确认
    # ============================================================

    def wait_complete(self):
        """
        Aizex 页面不会自动滚动，需主动 PageDown 翻页。

        流程：
        1. 初始等待（跳过模型思考静默期）
        2. 循环：PageDown → 等渲染 → OCR 检测
        3. 连续 N 次 OCR 结果不变 → 文字稳定
        4. 文字稳定后，检测完成图标作为二次确认
        5. 图标确认通过 或 额外多等一轮仍稳定 → 判定完成
        """
        cfg = self.config
        poll_interval = cfg.get("poll_interval", 5)
        stable_count = cfg.get("stable_count", 3)
        max_wait = cfg.get("max_wait", 360)

        # 初始等待
        first_wait = cfg.get("wait_first_reply", 6)
        if first_wait > 0:
            log.info(f"  初始等待 {first_wait}s（模型思考中）...")
            time.sleep(first_wait)

        log.info(f"翻页轮询（每{poll_interval}s，"
                 f"连续{stable_count}次稳定 + 图标确认，"
                 f"上限{max_wait}s）...")

        prev_text = ""
        stable = 0
        start = time.time()

        while time.time() - start < max_wait:
            # 每次循环按 N 次 PageDown 推动页面滚动
            n = cfg.get("pagedown_per_cycle", 1)
            for _ in range(n):
                pyautogui.press('pagedown')
                time.sleep(0.3)

            # 等待后 OCR
            time.sleep(poll_interval)
            current_text, line_count = self._ocr_current_screen()

            elapsed = int(time.time() - start)
            log.info(f"  [{elapsed}s] 行数={line_count}")

            if current_text and current_text == prev_text:
                stable += 1
                log.info(f"  稳定 ({stable}/{stable_count})")

                if stable >= stable_count:
                    # 文字已稳定，尝试图标确认
                    if self._check_completion_icon():
                        log.info("  ✓ 生成完成（图标确认）！")
                        return True

                    # 图标未检测到，可能不在可见区域，
                    # 再多给一轮机会
                    if stable >= stable_count + 1:
                        log.info("  ✓ 生成完成（稳定性判定）！")
                        return True
                    else:
                        log.info("  图标未检测到，继续等待一轮...")
            else:
                stable = 0

            prev_text = current_text

        log.warning(f"  超时（{max_wait}s）")
        return False

    # ============================================================
    # 模型选择辅助方法
    # ============================================================

    def _get_calibrated_pos(self, key):
        """从 data/coordinates.json 读取校准坐标"""
        coords_file = os.path.join(self._project_root, "data", "coordinates.json")
        if not os.path.exists(coords_file):
            return None
        try:
            with open(coords_file, 'r', encoding='utf-8') as f:
                coords = json.load(f).get("coordinates", {})
            if key in coords:
                return coords[key]
        except Exception:
            pass
        return None

    def _find_in_list(self, target, items):
        """检查 target 是否在列表中（完全匹配）"""
        for item in items:
            if target == item:
                return True
        return False

    def _click_model(self, model_name):
        """OCR 定位并点击模型名称"""
        from ocr_utils import find_text_on_screen

        # 尝试完整名称
        pos = find_text_on_screen(model_name)
        if pos:
            pyautogui.click(pos[0], pos[1])
            time.sleep(0.5)
            log.info(f"    ✓ 选择模型：{model_name} → "
                     f"({pos[0]}, {pos[1]})")
            return True

        # 去掉方括号后缀
        short = model_name.split('[')[0].strip()
        if short != model_name:
            pos = find_text_on_screen(short)
            if pos:
                pyautogui.click(pos[0], pos[1])
                time.sleep(0.5)
                log.info(f"    ✓ 选择模型（短名）：{short} → "
                         f"({pos[0]}, {pos[1]})")
                return True

        log.warning(f"    未找到模型文字「{model_name}」")
        return False

    def _click_category(self, category):
        """OCR 定位并点击分类名称展开二级菜单"""
        from ocr_utils import find_text_on_screen

        # 完整分类名
        pos = find_text_on_screen(category)
        if pos:
            pyautogui.click(pos[0], pos[1])
            log.info(f"    展开「{category}」→ ({pos[0]}, {pos[1]})")
            return True

        # 去括号
        short = category.split('[')[0].strip()
        if short != category:
            pos = find_text_on_screen(short)
            if pos:
                pyautogui.click(pos[0], pos[1])
                log.info(f"    展开（短名）「{short}」→ "
                         f"({pos[0]}, {pos[1]})")
                return True

        # 首词回退（如 "Gemini 系列" → "Gemini"）
        first_word = category.split()[0] if category.split() else ""
        if first_word and first_word != short:
            pos = find_text_on_screen(first_word)
            if pos:
                pyautogui.click(pos[0], pos[1])
                log.info(f"    展开（首词）「{first_word}」→ "
                         f"({pos[0]}, {pos[1]})")
                return True

        log.warning(f"    未找到分类「{category}」")
        return False

    def _close_menu(self):
        """按 Escape 关闭下拉菜单"""
        pyautogui.press('escape')
        time.sleep(0.3)

    # ============================================================
    # 图片生成 & 下载
    # ============================================================

    def generate_image(self, prompt, save_dir):
        """
        完整的图片生成 + 下载流程。

        与 generate() 的区别：
          - 生成完成后不复制文本，而是下载图片
          - 使用关键词 OCR 检测图片生成完成（"正在创建图片" / "图片已创建"）
          - 右键菜单 → Windows 保存对话框保存
        """
        from applications.image_gen.config import (
            IMAGE_GEN_WAIT_AFTER_SEND, IMAGE_GEN_MAX_WAIT,
        )

        log.info("=" * 50)
        log.info("Aizex 图片生成模式")
        log.info(f"  保存目录：{save_dir}")

        self.open_session()

        # === 聚焦输入框（失败则报错，不静默继续） ===
        if not self.focus_input():
            log.warning("  输入框聚焦失败，尝试滚动到底部后重试...")
            pyautogui.press('end')
            time.sleep(0.5)
            sw, sh = pyautogui.size()
            pyautogui.click(int(sw * 0.5), int(sh * 0.85))
            time.sleep(0.5)
            if not self.focus_input():
                pyautogui.press('end')
                time.sleep(0.3)
                pyautogui.click(int(sw * 0.5), int(sh * 0.88))
                time.sleep(0.5)
                if not self.focus_input():
                    raise RuntimeError(
                        "输入框聚焦连续失败，无法发送提示词。"
                        "请确认 Aizex 页面已完全加载。"
                    )

        # === 粘贴提示词并发送 ===
        log.info("粘贴图片提示词并发送...")
        self._paste_text(prompt)
        time.sleep(IMAGE_GEN_WAIT_AFTER_SEND)
        self.send()
        time.sleep(self.config.get("wait_after_send", 1.5))
        log.info("已发送，等待图片生成...")

        # 发送后立即翻页——提示词可能很长，"正在创建图片"在下方不可见
        for _ in range(3):
            pyautogui.press('pagedown')
            time.sleep(0.2)

        # 刷新会话 URL（延迟到 10s 后，等页面完成跳转）
        try:
            time.sleep(10)
            fresh_url = self._grab_url()
            if fresh_url and self.name.lower() in fresh_url.lower():
                self._session_url = fresh_url
                log.info(f"  会话 URL 已缓存：{self._session_url[:80]}...")
            # _grab_url() 会 Ctrl+L 聚焦地址栏，必须点回聊天区，
            # 否则后续 _wait_image_complete 中的 PageDown 全部无效
            sw, sh = pyautogui.size()
            pyautogui.click(int(sw * 0.5), int(sh * 0.55))
            time.sleep(0.3)
        except Exception as e:
            log.warning(f"  会话 URL 缓存失败（{e}）")

        # === 等待图片生成（关键词 OCR 检测） ===
        if not self._wait_image_complete(max_wait=IMAGE_GEN_MAX_WAIT):
            raise RuntimeError(
                "图片生成超时：未检测到「图片已创建」状态。"
            )

        # === 下载图片 ===
        filepath = self._download_image(save_dir)
        log.info(f"  ✓ 图片生成完成：{filepath}")
        return filepath

    def _wait_image_complete(self, max_wait=360):
        """
        图片生成完成检测——基于 OCR 关键词。

        检测逻辑：
          - "正在创建图片" → 仍在生成中，继续等待
          - "图片已创建"   → 生成完毕，等 5s 确保渲染完成
          - 每轮按多次 PageDown 确保滚动到最新输出
            （Aizex 聊天区是内嵌滚动容器，PageDown 比 End 更可靠）

        返回 True（完成）/ False（超时）。
        """
        from applications.image_gen.config import IMAGE_GEN_POLL_INTERVAL

        first_wait = self.config.get("wait_first_reply", 6)
        if first_wait > 0:
            log.info(f"  初始等待 {first_wait}s（模型思考中）...")
            time.sleep(first_wait)

        poll = IMAGE_GEN_POLL_INTERVAL
        log.info(f"  关键词轮询（每{poll}s，PDn×5 翻页，上限{max_wait}s）...")
        start = time.time()

        while time.time() - start < max_wait:
            # PageDown 翻页确保看到聊天区底部的最新状态
            for _ in range(5):
                pyautogui.press('pagedown')
                time.sleep(0.15)

            elapsed = int(time.time() - start)

            # OCR 当前屏幕
            sw, sh = pyautogui.size()
            from ocr_utils import ocr_region
            lines, _ = ocr_region(
                int(sw * 0.1), int(sh * 0.15),
                int(sw * 0.9), int(sh * 0.85)
            )
            full_text = '\n'.join(lines)

            # 先检查完成标志
            if '图片已创建' in full_text:
                log.info(f"  [{elapsed}s] ✓ 检测到「图片已创建」！"
                         f"等待 5s 确保图片渲染完成...")
                time.sleep(5)
                return True

            # 检查生成中标志
            if '正在创建图片' in full_text:
                log.info(f"  [{elapsed}s] 「正在创建图片」— 继续等待...")
            elif '创建' in full_text and '图片' in full_text:
                log.info(f"  [{elapsed}s] 检测到图片创建相关文字，继续等待...")
            else:
                log.info(f"  [{elapsed}s] 未检测到关键词，继续等待..."
                         f"（当前可见文本 {len(full_text)} 字符）")

            time.sleep(poll)

        log.warning(f"  超时（{max_wait}s），未检测到「图片已创建」")
        return False

    def _locate_image(self):
        """
        锚点插值法定位图片在聊天区的位置。

        策略：
          1. OCR 获取屏幕文字块 + 位置
          2. 找输入框占位文字 → input_y
          3. 找最下方 AI 文字块 → last_text_y
          4. 图片中心 = ((last_text_y + input_y) // 2, screen_width * 0.5)
          5. 右键验证 context menu 是否含"另存为"
          6. 失败则 y 偏移重试 6 次

        返回 (x, y) 绝对屏幕坐标。
        """
        from ocr_utils import ocr_region_raw

        pyautogui.press('end')
        time.sleep(0.5)

        sw, sh = pyautogui.size()
        placeholder = self.config.get("chat_placeholder", "有问题，尽管问")
        placeholder_nospace = placeholder.replace(" ", "")

        # OCR 获取带位置信息的文字块
        blocks = ocr_region_raw(
            int(sw * 0.1), int(sh * 0.1),
            int(sw * 0.9), int(sh * 0.9)
        )

        input_y = None
        last_text_y = 0

        for text, cx, cy in blocks:
            text_nospace = text.replace(" ", "")
            if placeholder_nospace in text_nospace:
                input_y = cy
            # 记录最后一个非输入框区域的文字 y 坐标
            if cy > last_text_y and (input_y is None or cy < input_y - 30):
                last_text_y = cy

        if input_y is not None and last_text_y > 0:
            image_cx = sw // 2
            image_cy = (last_text_y + input_y) // 2
            log.info(f"  锚点定位：last_text_y={last_text_y}, "
                     f"input_y={input_y} → image_center=({image_cx}, {image_cy})")
        else:
            # 兜底：屏幕比例估算
            image_cx = sw // 2
            image_cy = int(sh * 0.55)
            log.info(f"  锚点缺失，使用比例估算：({image_cx}, {image_cy})")

        # 右键验证 + y 偏移回退
        offsets = [0, -30, 30, -60, 60, -90]
        for dy in offsets:
            test_x, test_y = image_cx, image_cy + dy
            log.info(f"  尝试位置 ({test_x}, {test_y})，偏移 dy={dy}")
            pyautogui.rightClick(test_x, test_y)
            time.sleep(0.8)

            if self._check_save_as_menu():
                log.info(f"  ✓ 右键菜单确认 → 图片位于 ({test_x}, {test_y})")
                # 按 ESC 关闭菜单，让调用方重新右键
                pyautogui.press('escape')
                time.sleep(0.3)
                return (test_x, test_y)

            # 菜单不匹配，关闭后重试
            pyautogui.press('escape')
            time.sleep(0.3)

        raise RuntimeError(
            f"无法定位图片：所有偏移位置右键菜单均未检测到'另存为'"
        )

    def _check_save_as_menu(self):
        """OCR 检查当前屏幕是否出现了'另存为'右键菜单"""
        from ocr_utils import ocr_region

        sw, sh = pyautogui.size()
        lines, _ = ocr_region(
            int(sw * 0.25), int(sh * 0.25),
            int(sw * 0.7), int(sh * 0.7)
        )
        full = '\n'.join(lines)
        return '另存为' in full or 'Save image' in full or 'Save as' in full

    def _download_image(self, save_dir):
        """
        右键图片 → 另存为 → Windows 保存对话框 → 保存文件。

        返回保存的完整文件路径。
        """
        from applications.image_gen.config import (
            CONTEXT_SAVE_AS_KEY, SAVE_DIALOG_WAIT,
        )

        # 1. 定位图片
        cx, cy = self._locate_image()

        # 2. 直接右键打开 context menu
        log.info("右键打开 context menu...")
        pyautogui.rightClick(cx, cy)
        time.sleep(0.8)

        # 4. 按快捷键触发"另存为图片"
        log.info(f"按 '{CONTEXT_SAVE_AS_KEY}' 触发另存为...")
        pyautogui.press(CONTEXT_SAVE_AS_KEY)
        time.sleep(SAVE_DIALOG_WAIT)

        # 5. Windows 保存对话框 — 先导航到目标文件夹，再填文件名
        os.makedirs(save_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"aizex_{timestamp}.png"
        full_path = os.path.join(save_dir, filename)

        log.info(f"保存路径：{full_path}")

        # 步骤 A：Alt+D 聚焦地址栏 → 粘贴文件夹路径 → Enter 导航
        pyautogui.hotkey('alt', 'd')
        time.sleep(0.2)
        pyautogui.hotkey('ctrl', 'a')
        time.sleep(0.1)
        pyautogui.press('delete')
        time.sleep(0.1)
        pyperclip.copy(save_dir)
        time.sleep(0.1)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.3)
        pyautogui.press('enter')
        time.sleep(0.8)

        # 导航后对话框焦点可能跳到文件列表，点击标题栏区域重新锁定窗口
        sw, sh = pyautogui.size()
        pyautogui.click(int(sw * 0.5), int(sh * 0.15))
        time.sleep(0.3)

        # 步骤 B：Alt+N 聚焦文件名栏 → 粘贴文件名 → Enter 保存
        pyautogui.hotkey('alt', 'n')
        time.sleep(0.2)
        pyautogui.hotkey('ctrl', 'a')
        time.sleep(0.1)
        pyautogui.press('delete')
        time.sleep(0.1)
        pyperclip.copy(filename)
        time.sleep(0.1)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.3)
        pyautogui.press('enter')
        time.sleep(0.8)

        # 7. 检测同名覆盖对话框 → Alt+Y 确认
        if self._check_overwrite_dialog():
            log.info("  检测到同名文件覆盖提示，确认覆盖...")
            pyautogui.hotkey('alt', 'y')
            time.sleep(0.5)

        # 8. 等待文件写入完成
        waited = 0.0
        while waited < 5.0:
            if os.path.exists(full_path) and os.path.getsize(full_path) > 0:
                size_kb = os.path.getsize(full_path) / 1024
                log.info(f"  ✓ 文件已保存：{full_path} ({size_kb:.1f} KB)")
                return full_path
            time.sleep(0.5)
            waited += 0.5

        log.warning(f"  文件未在 {waited}s 内出现，可能保存失败")
        return full_path

    def _check_overwrite_dialog(self):
        """OCR 检测 Windows "确认另存为" 覆盖对话框"""
        from ocr_utils import ocr_region

        sw, sh = pyautogui.size()
        # 对话框通常在屏幕中央偏下
        lines, _ = ocr_region(
            int(sw * 0.25), int(sh * 0.35),
            int(sw * 0.75), int(sh * 0.65)
        )
        full = '\n'.join(lines)
        return '确认另存为' in full or 'Confirm Save As' in full or '覆盖' in full

    # ============================================================
    # 校准支持
    # ============================================================

    @classmethod
    def get_calibration_keys(cls):
        return {
            "aizex_model_menu": "Aizex 顶部的模型名称/下拉箭头"
                                "（必须校准，用于打开模型菜单）",
            "aizex_copy_btn": "Aizex 回复底部的复制图标按钮"
                              "（可选，有图标匹配可跳过）",
        }

    @classmethod
    def get_calibration_hints(cls):
        return {
            "aizex_model_menu": (
                "打开 Aizex 网站，将鼠标移到顶部显示当前模型名称的位置\n"
                "    （如「ChatGPT Auto」或「GPT-5.4 Thinking」处）"
            ),
            "aizex_copy_btn": (
                "在 Aizex 中让它生成一段回复，然后将鼠标移到\n"
                "    回复底部的复制图标上"
            ),
        }
