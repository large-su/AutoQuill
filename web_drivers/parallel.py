# ============================================================
# web_drivers/parallel.py — Web LLM 并行调度器（DOM 版）
#
# 调度模式沿袭旧 OCR 版 ParallelWebRunner（V2.x）：
#   外部 SlotState 管理轮询状态 + 单主循环派发/轮询/收集。
# 底层换成 DOM 技术栈（web_drivers/base.py）：
#   - 每 slot = 一个独立 driver 实例（共享 context 的独立 page），
#     由 create_driver() 构造，不触碰 get_driver() 单例
#   - 单线程主循环轮询所有 slot——天然规避 Playwright sync API
#     线程亲和问题（profile 锁也不允许第二浏览器实例）
#   - 完成判定逐轮复刻 deepseek.py:wait_complete 的语义：
#     停止按钮曾出现→消失即完成；否则文本稳定 + 超时兜底
#
# 用法：
#   runner = ParallelWebRunner(num_slots=2, threshold=2, scan_interval=2)
#   try:
#       runner.setup()
#       results = runner.run(tasks)   # tasks: [(prompt, meta), ...]
#       # results[i] 对应 tasks[i] 的生成结果（str 或 None）
#   finally:
#       runner.teardown()
# ============================================================

import logging
import time

log = logging.getLogger(__name__)

_MIN_SLOTS = 1
_MAX_SLOTS = 8


class SlotState:
    """单个 page（driver 实例）的运行时状态"""

    # 状态枚举
    IDLE = "IDLE"           # 空闲，等待派发任务
    GENERATING = "GENERATING"  # 生成中，主循环每轮轮询
    RESETTING = "RESETTING"  # 会话重建中（超时/连续失败后）
    DEAD = "DEAD"           # 多次重建失败，不再使用

    def __init__(self, slot_id, driver):
        self.slot_id = slot_id
        self.driver = driver
        self.status = SlotState.IDLE
        self.task_idx = None      # 当前任务的全局索引
        self.task_title = ""      # 日志显示用

        # 轮询状态（每次派发后重置，外部管理，不依赖 driver 实例变量）
        self.last_len = 0
        self.last_think = 0
        self.stable = 0
        self.body_started = False  # 正文已出现 → 之后只打生成心跳
        self.stop_seen = False    # 停止按钮曾出现（生成中）→ 消失才算完成
        self.pending_readback = None  # (长度, 重读轮数)：稳定判定后验证
        self.start_time = 0.0

        # 失败计数
        self.consecutive_fails = 0  # 任务失败计数，成功清零
        self.reset_fails = 0        # 连续重建失败计数，≥3 置 DEAD


class ParallelWebRunner:
    """Web LLM 并行调度器（DOM 版）。

    num_slots:  并行页面数（DeepSeek 网页版同账号并发上限实测为 2）
    threshold:  连续失败 N 次后重置该 slot 的会话
    scan_interval: 主循环每轮扫描间隔（秒）
    """

    def __init__(self, num_slots=2, threshold=2, scan_interval=2):
        if not (_MIN_SLOTS <= num_slots <= _MAX_SLOTS):
            raise ValueError(
                f"num_slots 必须在 {_MIN_SLOTS}-{_MAX_SLOTS} 之间：{num_slots}"
            )
        self.num_slots = num_slots
        self.threshold = threshold
        self.scan_interval = scan_interval
        self.slots = []
        self._setup_done = False

    # ============================================================
    # setup / teardown
    # ============================================================

    def setup(self):
        """初始化：每 slot 一个独立 driver 实例并打开全新对话。"""
        from web_drivers import create_driver

        log.info("=" * 60)
        log.info("Web 并行模式启动：%d 个页面，失败阈值 %d，扫描间隔 %ds",
                 self.num_slots, self.threshold, self.scan_interval)
        log.info("=" * 60)

        for i in range(self.num_slots):
            drv = create_driver()
            try:
                drv.new_chat()
                drv.setup()
            except Exception as exc:
                # 初始化失败不中断：派发时走失败路径自愈
                log.warning("  Slot %d 初始化异常：%s（派发时会重试）",
                            i, exc)
            self.slots.append(SlotState(i, drv))
        self._setup_done = True
        return self

    def teardown(self):
        """关闭所有 slot 的页面（不关共享浏览器，不触碰单例）。"""
        for slot in self.slots:
            try:
                slot.driver.close_session()
            except Exception:
                pass
        self.slots = []
        self._setup_done = False

    # ============================================================
    # 主循环
    # ============================================================

    def run(self, tasks):
        """执行任务列表，返回与 tasks 顺序一致的 results（str 或 None）。"""
        if not tasks:
            return []

        queue = list(enumerate(tasks))
        results = [None] * len(tasks)

        while queue or any(s.status not in (SlotState.IDLE, SlotState.DEAD)
                           for s in self.slots):
            for slot in self.slots:
                if slot.status == SlotState.DEAD:
                    continue
                if slot.status == SlotState.IDLE and queue:
                    task_idx, (prompt, meta) = queue.pop(0)
                    slot.task_idx = task_idx
                    slot.task_title = str(getattr(meta, "get", lambda k, d=None: d)(
                        "title", ""))[:40] if meta else ""
                    if not self._dispatch(slot, prompt, meta):
                        self._on_failure(slot)  # 派发失败计失败，结果留 None
                elif slot.status == SlotState.GENERATING:
                    st = self._poll(slot)
                    if st == "DONE":
                        story = self._collect(slot)
                        if story and len(story) >= 500:
                            results[slot.task_idx] = story
                            slot.consecutive_fails = 0
                            self._release(slot)
                        else:
                            self._on_failure(slot)
                    elif st == "TIMEOUT":
                        # 后台可能仍在生成，页面不可复用 → 重建会话
                        slot.status = SlotState.RESETTING
                elif slot.status == SlotState.RESETTING:
                    self._do_reset(slot)

            if queue and all(s.status == SlotState.DEAD for s in self.slots):
                log.error("所有 slot 均不可用，剩余 %d 个任务标记失败",
                          len(queue))
                break

            time.sleep(self.scan_interval)

        return results

    # ============================================================
    # 派发 / 轮询 / 收集
    # ============================================================

    def _dispatch(self, slot, prompt, meta):
        """派发任务：new_chat → setup → input → send。

        同步但快速（约 1-3s，期间其他 slot 在浏览器后台继续生成）；
        绝不调用阻塞的 wait_complete。
        """
        drv = slot.driver
        try:
            drv.new_chat()   # 全新对话：丢弃历史上下文（每任务必做）
            drv.setup()
            drv.input(prompt)
            drv.send()
        except Exception as exc:
            log.error("[Slot %d] 派发异常：%s", slot.slot_id, exc)
            return False
        slot.status = SlotState.GENERATING
        slot.last_len = 0
        slot.last_think = 0
        slot.stable = 0
        slot.stop_seen = False
        slot.pending_readback = None
        slot.body_started = False
        slot.start_time = time.time()
        log.info("[Slot %d] → 派发任务 %d（%s）", slot.slot_id,
                 slot.task_idx, slot.task_title or "")
        return True

    def _poll(self, slot):
        """非阻塞轮询一个生成中的 slot，返回 DONE / TIMEOUT / CONTINUING。

        判定语义与 deepseek.py:wait_complete 逐行同构：
        停止按钮曾出现→消失即完成；否则文本稳定 stable_count 轮且
        非空即完成；超时失败。每轮 _check_cancel()——Web 控制台
        「停止」按钮直接生效（WorkflowCancelled 冒泡）。
        """
        from applications.zhihu_story.browser_adapter import _check_cancel
        drv = slot.driver
        cfg = drv.config
        _check_cancel()
        cur_len = drv._current_reply_len()
        # 思考阶段心跳（与 deepseek.py:wait_complete 同构；无 _think_len
        # 的驱动/假驱动兜底为 0，行为退化为纯正文心跳）
        think_len = drv._think_len() if hasattr(drv, "_think_len") else 0
        if drv._stop_button_present():
            slot.stop_seen = True
        elif slot.stop_seen:
            log.info("[Slot %d] 停止按钮已消失，生成完成（%.1fs，%d 字符）",
                     slot.slot_id, time.time() - slot.start_time, cur_len)
            return "DONE"
        if cur_len != slot.last_len:
            slot.stable = 0
            slot.last_len = cur_len
            slot.pending_readback = None  # 长度变化 → 挂起的验证作废
            if cur_len:
                slot.body_started = True
                log.info("[Slot %d] 故事生成中… 已生成 %d 字",
                         slot.slot_id, cur_len)
        elif not slot.body_started and think_len != slot.last_think:
            slot.stable = 0
            slot.last_think = think_len
            if think_len:
                log.info("[Slot %d] 模型思考中… 已思考 %d 字符",
                         slot.slot_id, think_len)
        else:
            slot.stable += 1
        if slot.stable >= cfg.get("stable_count", 2) and cur_len:
            # read-back 验证（与 deepseek.py:wait_complete 同构）：LLM
            # 流式输出可能中途停顿 >8s（长 JSON 间歇），稳定判定可能是
            # 暂停而非完成——连续 2 轮重读长度不变才判定完成
            if slot.pending_readback is None:
                slot.pending_readback = (cur_len, 0)
                return "CONTINUING"
            exp_len, rounds = slot.pending_readback
            if cur_len != exp_len:
                log.info("[Slot %d] 稳定判定后输出仍增长（%d→%d），继续等待",
                         slot.slot_id, exp_len, cur_len)
                slot.pending_readback = None
                slot.stable = 0
                slot.last_len = cur_len
                return "CONTINUING"
            if rounds >= 1:
                slot.pending_readback = None
                log.info("[Slot %d] 文本稳定 %d 轮，判定完成"
                         "（%.1fs，%d 字符）",
                         slot.slot_id, cfg.get("stable_count", 2),
                         time.time() - slot.start_time, cur_len)
                return "DONE"
            slot.pending_readback = (exp_len, rounds + 1)
            return "CONTINUING"
        if time.time() - slot.start_time > cfg.get("max_wait", 600):
            log.warning("[Slot %d] 生成超时（%ds），重置会话",
                        slot.slot_id, cfg.get("max_wait", 600))
            return "TIMEOUT"
        return "CONTINUING"

    def _collect(self, slot):
        """读取该 slot 的最终回复（read_result 是一次快速 evaluate）。"""
        try:
            return slot.driver.read_result()
        except Exception as exc:
            log.error("[Slot %d] 读取结果失败：%s", slot.slot_id, exc)
            return None

    # ============================================================
    # 释放 / 失败 / 重置
    # ============================================================

    def _release(self, slot):
        """成功完成后释放 slot 接下一个任务。"""
        slot.status = SlotState.IDLE
        slot.task_idx = None
        slot.task_title = ""
        slot.last_len = 0
        slot.stable = 0
        slot.stop_seen = False
        slot.pending_readback = None
        slot.reset_fails = 0

    def _on_failure(self, slot):
        """任务失败：累计连续失败，达阈值 → 重置会话，否则继续复用。"""
        slot.consecutive_fails += 1
        slot.reset_fails = 0
        if slot.consecutive_fails >= self.threshold:
            log.warning("[Slot %d] 连续失败 %d 次，重置会话",
                        slot.slot_id, slot.consecutive_fails)
            slot.status = SlotState.RESETTING
        else:
            self._release(slot)

    def _do_reset(self, slot):
        """重建 slot 会话（对应旧版「关 tab 重建」）。"""
        drv = slot.driver
        try:
            drv.close_session()   # 关页（不关共享浏览器）
        except Exception:
            pass
        try:
            drv.new_chat()
            drv.setup()
            slot.status = SlotState.IDLE
            slot.consecutive_fails = 0
            slot.reset_fails = 0
        except Exception as exc:
            slot.reset_fails += 1
            log.error("[Slot %d] 重置失败 %d 次：%s",
                      slot.slot_id, slot.reset_fails, exc)
            if slot.reset_fails >= 3:
                slot.status = SlotState.DEAD
