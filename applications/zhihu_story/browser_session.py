# ============================================================
# applications/zhihu_story/browser_session.py
# 会话生命周期：启动/关闭/登录态/storage/多页管理与滚动
# P0 拆分自 browser_adapter.ZhihuBrowser；方法体逐字搬运未改动，
# 行为由 test_browser_adapter 的源码锚点断言守护。
# ============================================================

import json
import logging
import os
import time

log = logging.getLogger(__name__)

from core.paths import data as _data_path

from .browser_utils import (
    EDGE_PATH,
    USER_DATA_DIR,
    STORAGE_STATE_PATH,
    _CLEAN_EDGE_UA,
    _LAUNCH_TIMEOUT_MS,
    _ZHIHU_HOME,
)




def _kill_stale_profile_processes(user_data_dir):
    """清理仍占用本 profile 的残留 Edge 进程（Windows，尽力而为）。

    现象：任务被停止/进程被杀后，msedge.exe 可能还握着 user-data-dir
    的 profile 锁；下次新实例启动立即以 exit 21 /
    \"Target page, context or browser has been closed\" 退出，
    并让 Web 通道预检误报「未登录 DeepSeek」。
    只按命令行里的本 profile 路径匹配，绝不碰用户日常浏览器。

    ★★ 2026-09-26 修「互相残杀」（安装版真机事故）：本函数按命令行匹配，
    会连**我们自己正在干活的浏览器**一起 taskkill。自动化常驻后浏览器启动
    频繁（任务 + 网页版登录检查 + 知乎登录态检查 + 登录引导），于是每次新
    启动都杀一遍正在跑的实例——任务被打断、cookie 来不及落盘（会话 cookie
    丢失 = 知乎判定登出），还会形成「越杀越起不来」的死循环。
    现在：进程内还有活着的实例（live_browsers() > 0）就直接返回，
    只在确认「没有自己人」时才清理真正的残留。
    """
    if not user_data_dir or os.name != "nt":
        return
    try:
        from web_drivers.browser_pool import live_browsers
        alive = live_browsers()
        if alive > 0:
            # 自己人还活着：所谓「残留」就是正在工作的浏览器，绝不强杀
            log.debug("跳过残留进程清理（进程内有 %d 个活着的浏览器实例）",
                      alive)
            return
    except Exception:              # noqa: BLE001 拿不到计数就不清理（更安全）
        return
    script = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.Name -eq 'msedge.exe' -and "
        f"$_.CommandLine -like '*{user_data_dir}*' }} | "
        "ForEach-Object { [int]$_.ProcessId }")
    try:
        from desktop_utils import run_process_silent
        out = run_process_silent(
            ["powershell", "-NoProfile", "-Command", script], text=True)
        pids = [ln.strip() for ln in (out.stdout or "").splitlines()
                if ln.strip().isdigit()]
        if not pids:
            return
        log.warning("清理残留 Edge 进程（占用 profile 锁）: %s",
                    ", ".join(pids))
        run_process_silent(["taskkill", "/F", "/PID"] + pids)
        time.sleep(0.8)   # 等锁释放
    except Exception as exc:
        log.debug("清理残留 Edge 进程失败(不影响启动重试): %s", exc)

class SessionMixin:

    # profile 生命周期租约的等待上限（秒）：单个浏览器实例独占 profile，
    # 拿不到就等——任务默认 90s（够一个任务收尾），登录引导会调大（见 240s）。
    lease_timeout = 90.0

    def start(self):
        """启动持久化上下文，若存在已保存的登录态则自动恢复。
        （持久化 profile 本身也保留 cookie，这里双保险——
        无状态文件时保持全新会话，供首次手动登录。）

        ★ 2026-09-26：启动前先申请 profile 生命周期租约（close 时释放）。
        同一 user-data-dir 并发起两个 Chromium 必然第二个 exitCode=21 失败，
        旧代码还会在失败路径 taskkill 掉正在干活的浏览器——「登录态隔天失效」
        与「重新登录打不开窗口」都源于此。
        """
        from web_drivers.browser_pool import acquire_profile
        if not acquire_profile(self.lease_timeout, purpose="ZhihuBrowser"):
            from web_drivers.browser_pool import ProfileBusy
            raise ProfileBusy(
                "浏览器正被其它任务占用（同一 profile 只能开一个实例）；"
                "若正在跑任务或登录引导，请等它结束再试。")
        self._lease_held = True
        try:
            return self._start_locked()
        except Exception:
            self._release_lease()
            raise

    def _start_locked(self):
        """真正拉起浏览器（调用方已持有 profile 租约）。"""
        from playwright.sync_api import sync_playwright
        t0 = time.time()
        log.info("browser_adapter: 启动浏览器…（Playwright 驱动）")
        self._pw = sync_playwright().start()
        log.info("browser_adapter: 驱动就绪（%.1fs），拉起 Edge 持久化上下文…",
                 time.time() - t0)
        os.makedirs(self.user_data_dir, exist_ok=True)
        if not EDGE_PATH:
            raise RuntimeError(
                "未找到系统 Microsoft Edge！请安装 Edge 后重试"
                "（或设置 AQ_EDGE_PATH 环境变量指向 msedge.exe）")
        launch_kwargs = dict(
            user_data_dir=self.user_data_dir,
            executable_path=EDGE_PATH,
            headless=self.headless,
            locale="zh-CN",
            timeout=_LAUNCH_TIMEOUT_MS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        if self.headless:
            # 无头 UA 含 HeadlessChrome → 知乎不渲染作者列表，
            # 用去掉 Headless 的正常 Edge UA 覆盖。
            launch_kwargs["user_agent"] = _CLEAN_EDGE_UA

        # 后台任务（删除/抓取）刚结束时，同一个持久化 profile 的 Edge
        # 可能还没释放锁，导致新实例启动后立即被关闭。启动前先清残留
        # 进程（任务被停止/进程被杀时最常残留），再进入自动重试。
        _kill_stale_profile_processes(self.user_data_dir)
        last_exc = None
        for attempt in range(1, 4):
            try:
                self.context = self._pw.chromium.launch_persistent_context(
                    **launch_kwargs)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                log.warning(
                    "browser_adapter: 浏览器启动失败（第 %d 次）：%s",
                    attempt, exc)
                if attempt < 3:
                    # 每次重试前再清一次：上一次失败可能正是锁没释放
                    _kill_stale_profile_processes(self.user_data_dir)
                    time.sleep(2.5)
        else:
            # 全部失败：★ 必须停掉 Playwright 驱动（2026-09-26 修）——
            # sync_playwright().start() 会在**当前线程**里跑一个事件循环
            # （greenlet 泵），不 stop 就泄漏；该线程之后任何 sync_playwright()
            # 都会报「It looks like you are using Playwright Sync API inside
            # the asyncio loop」，把 FastAPI 线程池的 worker 一个个毒化——
            # 线上表现就是「登录态检查」接连报这个莫名其妙的错。
            self._stop_driver()
            if last_exc and "browser has been closed" in str(last_exc):
                log.error(
                    "浏览器 3 次启动失败：很可能是有残留 msedge.exe 占用"
                    " profile 锁（错误含 'Target page, context or browser "
                    "has been closed'）。已自动清理仍失败，请关闭残留 Edge"
                    " 进程或重启 AutoQuill 后重试")
            raise last_exc
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.load_storage_state()
        from web_drivers.browser_pool import note_browser_opened
        note_browser_opened()          # 活实例 +1：清理残留进程时据此自保
        log.info("browser_adapter: 浏览器就绪（共 %.1fs）", time.time() - t0)
        return self

    def _stop_driver(self):
        """停掉 Playwright 驱动（幂等）：失败路径与 close 共用。"""
        pw, self._pw = getattr(self, "_pw", None), None
        stop = getattr(pw, "stop", None)
        if stop:
            try:
                stop()
            except Exception:          # noqa: BLE001
                pass

    def _release_lease(self):
        """释放 profile 租约（幂等）。"""
        if not getattr(self, "_lease_held", False):
            return
        self._lease_held = False
        from web_drivers.browser_pool import (
            note_browser_closed, release_profile,
        )
        release_profile()
        note_browser_closed()

    def close(self):
        """关闭浏览器：先落登录态、再关上下文与驱动、最后释放 profile 租约。

        ★ 2026-09-26：关之前若仍是登录态，就把 cookie 快照写回
        browser_state.json——这样即使 profile 里的**会话 cookie**（SESSIONID
        这类，桌面浏览器被杀时容易丢）真丢了，下次启动也能从快照补回来
        （load_storage_state 只补缺不覆盖）。这是「登录一次能长期用」的兜底。
        """
        if self.context:
            try:
                if self.is_logged_in():
                    self.save_storage_state()
            except Exception:          # noqa: BLE001 存快照失败不影响关闭
                log.debug("关闭前保存登录态失败（忽略）", exc_info=True)
            try:
                self.context.close()
            except Exception:
                pass
            self.context = None
        # start() 半途失败时 _pw 可能是未初始化对象（无 stop），
        # _stop_driver 用 getattr 防护，close 不能再次抛错掩盖原异常
        self._stop_driver()
        self._release_lease()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # ----------------------------------------------------------
    # 登录态
    # ----------------------------------------------------------

    def is_logged_in(self):
        """登录检测：以知乎登录凭证 cookie z_c0 为准（httpOnly，
        DOM 选择器会随改版失效，cookie 检测与页面结构无关）。"""
        cookies = self.context.cookies(_ZHIHU_HOME)
        return any(c["name"] == "z_c0" and c.get("value") for c in cookies)

    def save_storage_state(self, path=None):
        """把当前登录态保存到本地文件（含会话 Cookie，勿提交 git）。"""
        path = path or self.storage_state
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.context.storage_state(), f, ensure_ascii=False)
        log.info("browser_adapter: 登录态已保存 → %s", path)

    def load_storage_state(self, path=None):
        """从本地文件恢复登录态；文件不存在时返回 False（需手动登录一次）。

        ★ 2026-09-23 修：**只补缺，绝不覆盖**。持久化 profile 本身就带 cookie，
        而这份文件可能是几小时前的旧登录态——原先无条件 add_cookies 会用文件里
        的旧 z_c0 盖掉 profile 里刚登录出来的新 z_c0。线上现象：用户在登录窗口
        里登成功，窗口一关（20:56:06）紧接着网页版登录检查启动浏览器又把旧
        cookie 写回（20:56:07「已恢复登录态（79 条 cookie）」）→「登了等于没登」。
        文件的作用只剩「profile 是空的/新建的」时兜底，语义与注释一致。
        """
        path = path or self.storage_state
        if not os.path.exists(path):
            log.info("browser_adapter: 无登录态文件 %s，需手动登录一次", path)
            return False
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        saved = state.get("cookies", []) or []
        try:
            live = {(c.get("name"), c.get("domain"), c.get("path"))
                    for c in self.context.cookies()}
        except Exception:          # noqa: BLE001 取不到就按全量补（首次启动）
            live = set()
        missing = [c for c in saved
                   if (c.get("name"), c.get("domain"), c.get("path")) not in live]
        if missing:
            self.context.add_cookies(missing)
        log.info("browser_adapter: 已恢复登录态（文件 %d 条，补入 %d 条，"
                 "profile 已有的不覆盖）", len(saved), len(missing))
        return True

    # ----------------------------------------------------------
    # 底层：有界页面交互
    # ----------------------------------------------------------

    def open_new_page(self, url=None):
        """新开一个页面（替代中键新开 tab）。"""
        page = self.context.new_page()
        if url:
            page.goto(url, wait_until="domcontentloaded")
        return page

    def switch_page(self, page):
        """切换当前操作页面（批量采集新开 tab 后指向新页）。"""
        self.page = page

    def close_page(self, page):
        try:
            page.close()
        except Exception:
            pass

    def scroll_feed(self, pixels=1500):
        """推荐页滚动加载更多：JS 滚动窗口，与键盘/鼠标解绑。"""
        self._safe_evaluate(f"() => window.scrollBy(0, {int(pixels)})")
        self.page.wait_for_timeout(1200)

    # ----------------------------------------------------------
    # 底层工具（供发布环节等扩展使用）
    # ----------------------------------------------------------
