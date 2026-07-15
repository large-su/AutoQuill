# ============================================================
# web_drivers/parallel_runner.py — Web LLM 并行调度器
#
# 核心思路：
#   在一个独立的 Edge 窗口里开 N 个 tab，每个 tab 对应一个 slot。
#   主循环不断轮询所有 slot：空闲的派发新任务，生成中的 OCR 检测进度，
#   完成的立即复制结果并接下一个任务。
#
# 与现有单 tab 串行逻辑完全独立：
#   - 不修改 base.py / deepseek.py / aizex.py / __init__.py(singleton)
#   - 只调用 driver 已有的底层方法（_ocr_current_screen / _paste_text
#     / focus_input / _copy_by_icon / _copy_by_position / setup 等）
#   - 所有轮询状态由 SlotState 外部管理，不依赖 driver 的实例变量
#
# 生命周期：
#   runner.setup()   → 开 Edge 窗口 + N 个 tab + 每个 tab 各自 driver.setup()
#   runner.run(tasks) → 主循环派发+轮询+收集，返回 [result, ...]
#   runner.teardown() → 关闭 Edge 窗口
# ============================================================

import time
import logging
import pyautogui
import pyperclip

log = logging.getLogger(__name__)


# ============================================================
# Slot 状态
# ============================================================

class SlotState:
    """单个 tab 的运行时状态"""

    # 状态枚举
    IDLE        = "IDLE"         # 空闲，等待派发任务
    GENERATING  = "GENERATING"   # 生成中，等待 OCR 轮询
    RESETTING   = "RESETTING"    # 需要关 tab 重建会话

    def __init__(self, slot_id, driver, tab_position):
        self.slot_id = slot_id              # 逻辑 slot 编号（0..N-1）
        self.driver = driver                # 该 slot 专属的 driver 实例
        self.tab_position = tab_position    # 浏览器中的 tab 位置（1..N，1-indexed）

        self.status = SlotState.IDLE
        self.task_idx = None                # 当前任务的全局索引
        self.task_title = ""                # 用于日志显示

        # 轮询状态（每次 dispatch 后重置）
        self.last_text = ""
        self.stable_count = 0
        self.start_time = 0.0
        self.ready_time = 0.0               # 初始等待期结束时间

        # 累计失败计数（成功即清零；触发阈值后重置会话）
        self.consecutive_fails = 0


# ============================================================
# 并行调度器
# ============================================================

class ParallelWebRunner:
    """
    Web LLM 并行调度器。

    用法：
        runner = ParallelWebRunner(num_slots=3, threshold=2, scan_interval=2)
        try:
            runner.setup()
            results = runner.run(tasks)   # tasks: [(prompt, meta), ...]
            # results[i] 对应 tasks[i] 的生成结果（str 或 None）
        finally:
            runner.teardown()
    """

    def __init__(self, num_slots=3, threshold=2, scan_interval=2):
        if not (1 <= num_slots <= 8):
            raise ValueError(
                f"num_slots 必须在 1-8 之间：{num_slots}（Ctrl+9 有歧义）"
            )
        self.num_slots = num_slots
        self.threshold = threshold
        self.scan_interval = scan_interval

        self.slots = []              # [SlotState, ...]
        self.edge_proc = None        # subprocess.Popen 句柄
        self._setup_done = False

    # ============================================================
    # setup：开 Edge 窗口 + N 个 tab + 每个 tab 各自 setup()
    # ============================================================

    def setup(self):
        """初始化：启动独立 Edge 窗口，在其中开 N 个 tab 并完成 driver.setup()"""
        from desktop_utils import (
            open_new_edge_window, navigate_to_url, focus_edge
        )
        from web_drivers import create_driver

        log.info("=" * 60)
        log.info(f"并行模式启动：{self.num_slots} 个 tab，"
                 f"失败阈值 {self.threshold}，扫描间隔 {self.scan_interval}s")
        log.info("=" * 60)

        # --- Slot 0：启动 Edge 独立窗口，初始 tab 就是 slot 0 ---
        drv0 = create_driver()
        url = drv0.url
        wait_load = drv0.config.get("wait_load", 4.0)

        self.edge_proc = open_new_edge_window(url)
        time.sleep(wait_load)
        # 不在这里主动调用 focus_edge()——新窗口 Popen 后已自动前台
        # 且 focus_edge() 可能选错窗口（用户有多个 Edge 窗口时）

        # Slot 0 的 tab 已经加载并完成初始化
        drv0._tab_opened = True
        try:
            drv0.setup()
        except Exception as e:
            log.error(f"  Slot 0 setup 异常：{e}")
        self.slots.append(SlotState(0, drv0, tab_position=1))
        log.info(f"  ✓ Slot 0 就绪（tab 位置 1）")

        # --- Slot 1..N-1：Ctrl+T 开新 tab + 导航 + setup ---
        for i in range(1, self.num_slots):
            # Ctrl+T 作用于当前前台窗口，我们依赖焦点没被打断
            # （用户不要在 setup 阶段点击其他窗口）
            pyautogui.hotkey('ctrl', 't')
            time.sleep(1.2)

            navigate_to_url(url)
            time.sleep(wait_load)

            drv = create_driver()
            drv._tab_opened = True
            try:
                drv.setup()
            except Exception as e:
                log.error(f"  Slot {i} setup 异常：{e}")

            self.slots.append(SlotState(i, drv, tab_position=i + 1))
            log.info(f"  ✓ Slot {i} 就绪（tab 位置 {i + 1}）")

        self._setup_done = True
        log.info(f"并行运行器就绪：{self.num_slots} 个独立会话已建立")
        log.info("=" * 60)

    # ============================================================
    # run：主循环
    # ============================================================

    def run(self, tasks):
        """
        主循环：派发任务 + 轮询检测 + 收集结果。

        参数：
            tasks: [(prompt, meta), ...]
                   prompt: str，喂给 LLM 的完整 prompt
                   meta:   任意（dict），仅用于日志显示（如 materials 项）

        返回：
            results: list，长度 == len(tasks)，顺序一致
                     元素是 str（成功）或 None（失败/超时）
        """
        if not self._setup_done:
            raise RuntimeError("必须先调用 setup()")

        from desktop_utils import switch_to_tab

        total = len(tasks)
        queue = list(enumerate(tasks))  # [(task_idx, (prompt, meta)), ...]
        results = [None] * total
        done_count = 0

        log.info(f"\n开始并行生成：{total} 个任务，{self.num_slots} 个 slot\n")

        while queue or any(s.status != SlotState.IDLE for s in self.slots):
            for i in range(self.num_slots):
                slot = self.slots[i]

                # 切换到该 slot 对应的 tab
                try:
                    switch_to_tab(slot.tab_position)
                except Exception as e:
                    log.error(f"[Slot {i}] 切换 tab {slot.tab_position} 失败：{e}")
                    continue

                # ========== 状态机 ==========
                if slot.status == SlotState.IDLE:
                    if queue:
                        task_idx, (prompt, meta) = queue.pop(0)
                        self._dispatch(slot, task_idx, prompt, meta)

                elif slot.status == SlotState.GENERATING:
                    status = self._poll(slot)

                    if status == "DONE":
                        story = self._collect(slot)
                        if story and len(story) >= 500:
                            results[slot.task_idx] = story
                            done_count += 1
                            log.info(
                                f"[Slot {i}] ✓ 任务 {slot.task_idx + 1} 完成"
                                f"（{len(story)}字符，进度 {done_count}/{total}）"
                            )
                            slot.consecutive_fails = 0
                            self._release(slot)
                        else:
                            log.warning(
                                f"[Slot {i}] ✗ 任务 {slot.task_idx + 1} "
                                f"复制失败或内容过短"
                            )
                            self._on_failure(slot)

                    elif status == "TIMEOUT":
                        log.warning(
                            f"[Slot {i}] ✗ 任务 {slot.task_idx + 1} 超时"
                        )
                        # 超时的 tab 可能还在后台生成，不可复用 → 强制重置
                        slot.status = SlotState.RESETTING

                    # CONTINUING / WAITING：继续轮询，不做动作

                elif slot.status == SlotState.RESETTING:
                    self._do_reset(slot)

            # 一轮扫描完，间歇一下再下一轮
            time.sleep(self.scan_interval)

        log.info(f"\n并行生成全部结束：成功 {done_count}/{total}\n")
        return results

    # ============================================================
    # teardown：关闭 Edge 窗口
    # ============================================================

    def teardown(self):
        """关闭独立 Edge 窗口，释放 subprocess 句柄"""
        from desktop_utils import close_browser_window, switch_to_tab

        if not self._setup_done:
            return

        log.info("关闭并行 Edge 窗口...")
        try:
            # 切到第一个 tab，再 Ctrl+Shift+W 关闭整个窗口
            switch_to_tab(1)
            time.sleep(0.3)
            close_browser_window()
        except Exception as e:
            log.warning(f"  close_browser_window 异常：{e}")

        # 兜底：如果窗口还活着，强制杀进程
        if self.edge_proc:
            try:
                # Popen.poll() == None 表示进程仍在运行
                if self.edge_proc.poll() is None:
                    self.edge_proc.terminate()
                    time.sleep(0.5)
            except Exception:
                pass
            self.edge_proc = None

        self._setup_done = False
        self.slots.clear()
        log.info("并行运行器已关闭")

    # ============================================================
    # 内部：派发 / 轮询 / 收集 / 失败处理 / 重置
    # ============================================================

    def _dispatch(self, slot, task_idx, prompt, meta):
        """派发新任务到当前 slot（已切到对应 tab）"""
        drv = slot.driver
        cfg = drv.config

        title = ""
        if isinstance(meta, dict):
            title = meta.get('title', '')
        title_disp = title[:30] if title else f"task#{task_idx + 1}"

        log.info(f"[Slot {slot.slot_id}] → 派发任务 {task_idx + 1}：{title_disp}")

        try:
            drv.focus_input()
            drv._paste_text(prompt)
            time.sleep(cfg.get("wait_after_paste", 0.5))
            drv.send()
            time.sleep(cfg.get("wait_after_send", 1.5))
        except Exception as e:
            log.error(f"[Slot {slot.slot_id}] 派发异常：{e}")
            # 失败就地处理：不进入 GENERATING，留在 IDLE 让下一轮重试
            slot.status = SlotState.IDLE
            return

        # 进入生成状态，重置轮询字段
        slot.status = SlotState.GENERATING
        slot.task_idx = task_idx
        slot.task_title = title_disp
        slot.last_text = ""
        slot.stable_count = 0
        slot.start_time = time.time()
        slot.ready_time = time.time() + cfg.get("wait_first_reply", 0)

    def _poll(self, slot):
        """
        非阻塞轮询单个 slot（已切到对应 tab）。

        返回：
            "DONE"       生成完成，可以复制结果
            "TIMEOUT"    超过 max_wait
            "WAITING"    还在初始思考静默期
            "CONTINUING" 仍在生成，下一轮再来
        """
        drv = slot.driver
        cfg = drv.config

        # --- 初始思考静默期 ---
        if time.time() < slot.ready_time:
            return "WAITING"

        # --- 前置动作：Aizex 需 PageDown × N ---
        pagedown_n = cfg.get("pagedown_per_cycle", 0)
        if pagedown_n > 0:
            for _ in range(pagedown_n):
                pyautogui.press('pagedown')
                time.sleep(0.3)

        # --- OCR 检测 ---
        try:
            current_text, line_count = drv._ocr_current_screen()
        except Exception as e:
            log.warning(f"[Slot {slot.slot_id}] OCR 异常：{e}")
            current_text, line_count = "", 0

        elapsed = int(time.time() - slot.start_time)
        log.info(
            f"  [Slot {slot.slot_id} / {elapsed}s / 任务{slot.task_idx + 1}] "
            f"行数={line_count}"
        )

        stable_count = cfg.get("stable_count", 2)

        # --- 稳定性判定 ---
        if current_text and current_text == slot.last_text:
            slot.stable_count += 1
            log.info(f"    Slot {slot.slot_id} 稳定 "
                     f"({slot.stable_count}/{stable_count})")

            if slot.stable_count >= stable_count:
                # 配置了 completion_icon（如 Aizex）→ 图标二次确认
                has_icon = bool(cfg.get("completion_icon"))
                if has_icon:
                    if drv._check_completion_icon():
                        return "DONE"
                    elif slot.stable_count >= stable_count + 1:
                        log.info(f"    Slot {slot.slot_id} 稳定达 N+1 次，"
                                 f"强制判定完成")
                        return "DONE"
                    # 否则继续等待一轮图标机会
                else:
                    return "DONE"
        else:
            slot.stable_count = 0
        slot.last_text = current_text

        # --- 超时判定 ---
        max_wait = cfg.get("max_wait", 360)
        if time.time() - slot.start_time > max_wait:
            return "TIMEOUT"

        return "CONTINUING"

    def _collect(self, slot):
        """
        复制当前 slot 的生成结果（已切到对应 tab）。

        只走 _copy_by_icon：
        - 不走 drv.copy_result()，因为其兜底会 input() 阻塞整个调度
        - 不走 _copy_by_position，因为它依赖固定校准坐标，但每个 tab
          的复制按钮 Y 坐标取决于生成内容长度，并非固定。多 tab 场景
          下校准兜底必然失败（或误点到其他 tab 的坐标），反而浪费时间
        - 图标匹配失败就返回 None，交给主循环根据失败计数决定是否重置会话
        """
        drv = slot.driver
        cfg = drv.config
        wait_scroll = cfg.get("wait_scroll_end", 0.8)
        wait_copy = cfg.get("wait_copy_click", 0.6)

        # 清空剪贴板，防止残留上个 slot 的结果被误读
        pyperclip.copy("")
        time.sleep(0.1)

        try:
            story = drv._copy_by_icon(wait_scroll, wait_copy)
            if story:
                return story
        except Exception as e:
            log.warning(f"[Slot {slot.slot_id}] _copy_by_icon 异常：{e}")

        return None

    def _release(self, slot):
        """成功完成后释放 slot，准备接下一个任务"""
        slot.status = SlotState.IDLE
        slot.task_idx = None
        slot.task_title = ""
        slot.last_text = ""
        slot.stable_count = 0

    def _on_failure(self, slot):
        """生成失败（非超时）：计数未达阈值则继续复用，达阈值则重置会话"""
        slot.consecutive_fails += 1
        if slot.consecutive_fails >= self.threshold:
            log.warning(
                f"[Slot {slot.slot_id}] 连续失败 {slot.consecutive_fails} 次"
                f"（阈值 {self.threshold}），准备重置会话"
            )
            slot.status = SlotState.RESETTING
        else:
            log.info(
                f"[Slot {slot.slot_id}] 失败计数 {slot.consecutive_fails}"
                f"/{self.threshold}，保留会话继续"
            )
            self._release(slot)

    def _do_reset(self, slot):
        """
        关 tab → 开新 tab → 导航 → setup。

        tab 位置变化：
          - 关掉位置 P 的 tab 后，位置 > P 的 tab 都左移 1
          - 新开的 tab 一定出现在最后（位置 = num_slots）
        """
        from desktop_utils import navigate_to_url, focus_edge

        drv = slot.driver
        old_pos = slot.tab_position

        log.info(f"[Slot {slot.slot_id}] 重置会话："
                 f"关 tab {old_pos} → 开新 tab")

        # 我们已经切到了 slot 的 tab（主循环开头做的），直接关
        try:
            pyautogui.hotkey('ctrl', 'w')
            time.sleep(1.0)
        except Exception as e:
            log.error(f"[Slot {slot.slot_id}] 关 tab 异常：{e}")

        # 其他 slot 的 tab 位置 > old_pos 的，全都左移 1
        for other in self.slots:
            if other.slot_id != slot.slot_id and other.tab_position > old_pos:
                other.tab_position -= 1

        # 打开新 tab（必然在最后位置）
        try:
            focus_edge()
            time.sleep(0.2)
            pyautogui.hotkey('ctrl', 't')
            time.sleep(1.2)
        except Exception as e:
            log.error(f"[Slot {slot.slot_id}] 开新 tab 异常：{e}")
            # 严重异常，标记为 IDLE 尝试自愈
            slot.status = SlotState.IDLE
            slot.consecutive_fails = 0
            return

        # 新 tab 一定在最后位置
        slot.tab_position = self.num_slots

        # 导航到 LLM URL + 等加载
        url = drv.url
        wait_load = drv.config.get("wait_load", 4.0)
        try:
            navigate_to_url(url)
            time.sleep(wait_load)
        except Exception as e:
            log.error(f"[Slot {slot.slot_id}] 导航异常：{e}")

        # 重置 driver 的会话相关实例状态
        drv._session_url = None
        drv._tab_opened = True

        # 重新执行 driver.setup()（模式切换/模型选择等）
        try:
            drv.setup()
        except Exception as e:
            log.error(f"[Slot {slot.slot_id}] setup 异常：{e}")

        # 复位 slot 状态
        slot.status = SlotState.IDLE
        slot.consecutive_fails = 0
        slot.last_text = ""
        slot.stable_count = 0
        slot.task_idx = None
        slot.task_title = ""

        log.info(f"[Slot {slot.slot_id}] 重置完成，新 tab 位置 "
                 f"{slot.tab_position}")
