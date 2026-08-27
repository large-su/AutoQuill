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


class SessionMixin:

    def start(self):
        """启动持久化上下文，若存在已保存的登录态则自动恢复。
        （持久化 profile 本身也保留 cookie，这里双保险——
        无状态文件时保持全新会话，供首次手动登录。）"""
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
        # 可能还没释放锁，导致新实例启动后立即被关闭。等待并自动重试。
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
                    time.sleep(2.5)
        else:
            # 全部失败：丢弃半初始化驱动，避免残留进程占住 profile 锁
            self._pw = None
            raise last_exc
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.load_storage_state()
        log.info("browser_adapter: 浏览器就绪（共 %.1fs）", time.time() - t0)
        return self

    def close(self):
        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
            self.context = None
        _pw = getattr(self, "_pw", None)
        if _pw is not None:
            # start() 半途失败时 _pw 可能是未初始化对象（无 stop），
            # 用 getattr 防护，close 不能再次抛错掩盖原异常
            stop = getattr(_pw, "stop", None)
            if stop:
                try:
                    stop()
                except Exception:
                    pass
            self._pw = None

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
        """从本地文件恢复登录态；文件不存在时返回 False（需手动登录一次）。"""
        path = path or self.storage_state
        if not os.path.exists(path):
            log.info("browser_adapter: 无登录态文件 %s，需手动登录一次", path)
            return False
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        self.context.add_cookies(state.get("cookies", []))
        log.info("browser_adapter: 已恢复登录态（%d 条 cookie）",
                 len(state.get("cookies", [])))
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
