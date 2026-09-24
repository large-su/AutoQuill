# ============================================================
# applications/zhihu_story/browser_adapter.py — DOM 语义化浏览器适配层
#
# 核心目标：浏览器操作与物理鼠标/屏幕完全解绑。
#   - Python 直连 playwright，launch_persistent_context 启动独立 Edge 实例
#     （独立 user-data-dir，不占用用户日常 Edge；登录态存 storage_state）
#   - 所有交互通过 DOM 指令（evaluate / click selector）触发，
#     与分辨率、缩放、鼠标位置无关；运行期间用户可干其他事
#   - 复用本会话验证过的知乎 DOM 提取逻辑（问题页/作者页/推荐页）
#
# 语义接口（与具体网页结构解耦，供 workflows/zhihu.py 调用）：
#   ZhihuBrowser.open_question(url)          → 打开问题页
#   ZhihuBrowser.get_recommend_questions()   → 推荐页候选列表
#   ZhihuBrowser.get_primary_answer(url)     → 问题页首答（正文+互动数据）
#   ZhihuBrowser.get_author_answer_links()   → 作者页全部答案链接
#   ZhihuBrowser.get_author_answer(url)      → 指定作者某篇答案全文
#   ZhihuBrowser.save_storage_state()        → 保存登录态（敏感，gitignored）
#
# 架构位置：Layer 5 (Applications) — 知乎平台浏览器通道（DOM 主通道）
# ============================================================

import json
import logging
import os
import re
import time

log = logging.getLogger(__name__)

from core.paths import data as _data_path



# —— P0 拆分：纯工具/常量移至 browser_utils.py；行为方法拆入三个 mixin，
# 此处只保留组合类声明与跨切面基础件(_safe_evaluate/_button_with_text/
# eval_js/click)。MRO：基自身基础件优先于 mixin。
from .browser_utils import (   # 组合类定义期需要的名字
    _EVAL_TIMEOUT,
    STORAGE_STATE_PATH,
    USER_DATA_DIR,
)
# —— 兼容门面：历史调用方(tests/tools/workflows)从这里一站式导入
from .browser_utils import (   # noqa: F401
    _AUTHOR_LINKS_JS,
    _CLEAN_EDGE_UA,
    _EXPAND_FIRST_COLLAPSED_JS,
    _LAUNCH_TIMEOUT_MS,
    _NAV_TIMEOUT,
    _PRIMARY_ANSWER_JS,
    _RECOMMEND_QUESTIONS_JS,
    _ZHIHU_HOME,
    EDGE_PATH,
    build_draft_marker,
    build_story_record,
    clean_story_markdown,
    extract_answer_id,
    normalize_author_url,
    normalize_question_url,
    page_needs_login,
    story_markdown_to_html,
)
from .browser_dom import DomReadMixin
from .browser_session import SessionMixin
from .browser_write import WriteActionsMixin

class ZhihuBrowser(SessionMixin, DomReadMixin, WriteActionsMixin):
    """知乎 DOM 浏览器通道。启动独立 Edge 实例，复用持久化登录态。"""

    def __init__(self, user_data_dir=USER_DATA_DIR,
                 storage_state=STORAGE_STATE_PATH, headless=False):
        self.user_data_dir = user_data_dir
        self.storage_state = storage_state
        self.headless = headless
        self.context = None
        self.page = None

    # ----------------------------------------------------------
    # 生命周期
    # ----------------------------------------------------------

    def _safe_evaluate(self, js, *args, timeout=_EVAL_TIMEOUT):
        """有界页面交互（实现下沉 web_drivers/browser_pool.safe_evaluate）。"""
        return safe_evaluate(self.page, js, *args, timeout=timeout)

    # ----------------------------------------------------------
    # 语义接口：选题
    # ----------------------------------------------------------


    def _button_with_text(self, text):
        """当前页面是否存在文本恰好等于 text 的 <button>（去掉零宽字符）。

        「写回答」「编辑回答」按精确文本区分：写回答=未答过、编辑回答=已答过。
        """
        return bool(self._safe_evaluate(
            """(text) =>
              Array.from(document.querySelectorAll('button'))
                .some(e => e.textContent
                    .replace(/[\\u200b-\\u200d\\ufeff]/g, '').trim() === text)""",
            text))

    # ----------------------------------------------------------
    # 语义接口：发布（导入文档到编辑器）
    # ----------------------------------------------------------

    _WRITE_BUTTON_TEXTS = ("写回答", "编辑回答")

    def _extract_question_id(self, url=None):
        m = re.search(r"/question/(\d+)", url or self.page.url)
        return m.group(1) if m else None


    def eval_js(self, js, *args):
        return self._safe_evaluate(js, *args)

    def click(self, selector=None, text=None):
        """DOM 直点：在页面 JS 上下文内直接触发原生 click 事件。
        不经过坐标命中测试 —— 不受遮挡、滚动、分辨率影响，
        真正与鼠标/视图解绑（playwright 的 page.click 仍会做坐标
        命中测试，遇遮挡即失败，故不用）。
        selector 为 CSS 选择器；text 为按钮文本（精确匹配）。"""
        if text is not None:
            clicked = self._safe_evaluate("""(text) => {
              // 知乎按钮文本常带零宽空格(​)，trim 不去除，需先剥离
              const clean = s => s.replace(/[\\u200b-\\u200d\\ufeff]/g, '').trim();
              const el = Array.from(document.querySelectorAll('button'))
                .find(e => clean(e.textContent || '') === text);
              if (!el) return false;
              el.click();
              return true;
            }""", text)
            if not clicked:
                raise ValueError(f"未找到文本为 {text!r} 的按钮")
        else:
            clicked = self._safe_evaluate("""(sel) => {
              const el = document.querySelector(sel);
              if (!el) return false;
              el.click();
              return true;
            }""", selector)
            if not clicked:
                raise ValueError(f"选择器 {selector!r} 未匹配到元素")
        return True


# ----------------------------------------------------------
# 浏览器基础设施（实现下沉 web_drivers/browser_pool；此处 re-export
# 垫片保持 workflows/tools/collector/webui 调用点零改动。工厂模块级
# 注册——引用 ZhihuBrowser 模块全局，mock.patch.object(mod, "ZhihuBrowser")
# 拦截链不断）
# ----------------------------------------------------------

from web_drivers.browser_pool import (
    WorkflowCancelled,
    set_cancel_hook,
    _check_cancel,
    _browser_lock,
    get_browser,
    close_shared_browser,
    safe_evaluate,
    register_browser_factory,
    create_browser,
)




def _browser_factory(headless):
    """浏览器创建工厂（browser_pool 注册）：创建未启动的 ZhihuBrowser。

    返回未启动实例：get_browser 在 pool 锁内 start()；登录引导等
    独立实例用 `with create_browser(...)` 经 __enter__ 启动。"""
    return ZhihuBrowser(headless=headless)


register_browser_factory(_browser_factory)


# ---- 登录态失效识别（2026-09-19）----
# 事故：知乎服务端把会话判失效后，cookie 里的 z_c0 仍在（is_logged_in 只看
# cookie → 仍报「已登录」），但任何页面都会被重定向到 /signin。抓取端于是
# 拿到 0 条，界面只显示模糊的「刷新失败」。这里给出「页面是否停在登录页」的
# 直接证据，供看板/草稿箱抓取与删除链路复用。
LOGIN_EXPIRED_MSG = (
    "知乎登录态已失效（页面被重定向到登录页），请在控制台右上角「设置」里"
    "重新登录知乎，然后再点刷新")


class ZhihuLoginRequired(RuntimeError):
    """知乎登录态失效：页面停在登录页，需要用户重新登录。"""


# page_needs_login 已下沉 browser_utils（2026-09-23：写通道 publish_draft
# 也要用同一口径识别登录失效，放叶子模块避免 adapter <-> write 循环引用）。
# 这里由上面的兼容门面 re-export，历史调用方/测试的导入路径不变。


def verify_zhihu_login(headless=True, lock_timeout=8):
    """真实检查知乎登录态：打开知乎首页，看是否被重定向到登录页。

    与 is_logged_in()（只看 z_c0 cookie）不同——cookie 还在但服务端已把会话
    登出时，cookie 检查会假阳性（2026-09-19 看板/草稿箱「刷新失败」的真因）。
    返回 (logged_in: bool, detail: str)；异常按「检查失败」返回，不抛出。

    ★ 2026-09-23：本检查要与共享浏览器/登录引导共用同一 user-data-dir，而
      Chromium 单例锁禁止同目录并发——原先没加锁，撞上「登录引导」或网页版
      登录检查就是一次莫名失败（「Target page, context or browser has been
      closed」）。这里取 _browser_lock 且有界等待：拿不到就明确回报「被占用」，
      不让请求线程干等（登录引导最长持有 5 分钟）。
    """
    from web_drivers.browser_pool import _browser_lock
    if not _browser_lock.acquire(timeout=lock_timeout):
        return False, ("检查失败：浏览器正被其它任务占用（登录引导/任务运行中），"
                       "请稍后重试")
    try:
        with ZhihuBrowser(headless=headless) as browser:
            browser.page.goto(_ZHIHU_HOME,
                              wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
            time.sleep(2.5)
            if page_needs_login(browser.page):
                return False, "已登出（知乎把会话登出，页面被重定向到登录页）"
            return True, "登录态有效（知乎首页正常打开）"
    except Exception as exc:      # noqa: BLE001
        return False, f"检查失败：{exc}"
    finally:
        _browser_lock.release()


def _zhihu_token(browser):
    """当前上下文的知乎凭证 cookie 值（z_c0）；没有则空串。

    比 is_logged_in() 多给一个「值」：登录接口会换发新 token，凭值的变化
    就能判断「刚刚真的登录过」，而不是只看 cookie 在不在（老代码的坑）。
    """
    try:
        for c in browser.context.cookies(_ZHIHU_HOME):
            if c.get("name") == "z_c0" and c.get("value"):
                return c["value"]
    except Exception:             # noqa: BLE001 页面/上下文已断
        return ""
    return ""


def _any_page_off_signin(browser):
    """上下文里是否已有页面离开登录页。

    为什么要看全部页面：用户可能在**新标签页/弹窗**里完成登录（扫码、
    第三方登录都会另开页），原页面会一直停在 /signin——只盯当前页就会
    一直等到超时（2026-09-23 用户实测：登完了但窗口不关）。
    """
    pages = []
    try:
        pages = list(browser.context.pages)
    except Exception:             # noqa: BLE001
        pass
    if not pages:
        try:
            pages = [browser.page]
        except Exception:         # noqa: BLE001
            return False
    for p in pages:
        try:
            if p.url and not page_needs_login(p):
                return True
        except Exception:         # noqa: BLE001 页面已关
            continue
    return False


def _probe_session_ok(browser, timeout_ms=15000):
    """会话探测：用共享 cookie 的 HTTP 客户端问一次知乎首页。

    2026-09-23 实测：登录失效时 https://www.zhihu.com/ 直接 302 到
    /signin；登录有效则 200。用 context.request 探测**不开标签页、不碰
    用户正在操作的页面**（他可能还在输验证码），所以可以周期性跑。
    返回 True 只在拿到 200 时——其余（302/异常）一律按「还没登上」处理，
    下一轮再探，避免又一次假成功。
    """
    try:
        resp = browser.context.request.get(
            _ZHIHU_HOME, max_redirects=0, timeout=timeout_ms,
            headers={"User-Agent": _CLEAN_EDGE_UA})
    except Exception as exc:      # noqa: BLE001 网络抖动：下一轮再试
        log.debug("登录引导：会话探测失败（忽略）：%s", exc)
        return False
    if resp.status == 200:
        return True
    if resp.status in (301, 302, 303, 307, 308):
        loc = resp.headers.get("location", "") or ""
        if "/signin" in loc:
            return False
        log.debug("登录引导：会话探测到重定向 %s（按未登录处理）", loc[:80])
    return False


def zhihu_login_confirmed(browser, token_before="", probe=True):
    """手动登录是否真的完成。三条判据（都必须先有凭证 cookie z_c0）：

      1. 凭证被换新（与流程开始时的值不同）→ 服务端刚换发会话 = 登录成功；
      2. 当前页或上下文里任一页面已离开登录页（用户可能在新标签页登录）；
      3. 会话探测：知乎首页不再 302 到 /signin（probe=False 时跳过）。

    ★ 为什么不是「只看 cookie」也不是「只看页面」（2026-09-23 两次事故）：
      · 只看 cookie：知乎把会话登出后 z_c0 仍留在 profile 里 → 一进函数就
        假成功，引导窗口「闪一下就被关闭」，用户永远登不上；
      · 只看当前页：登录在别的标签页完成、或登录接口换了 token 但页面还没
        跳转时，会一直判「没登上」，等满 5 分钟也不保存、不关窗。
    """
    token = _zhihu_token(browser)
    if not token:
        return False              # 没有凭证 cookie：一定没登上
    if token_before and token != token_before:
        return True
    if _any_page_off_signin(browser):
        return True
    return bool(probe) and _probe_session_ok(browser)


def login_zhihu_flow(timeout=300):
    """打开可见 Edge 窗口引导用户手动登录知乎，检测到登录后保存登录态。

    供 CLI（--login）与 Web 首启引导（/api/setup/zhihu-login）共用。
    返回 (是否成功, 提示信息)。独立实例 + 持 _browser_lock（与
    login_deepseek_web_flow 同理：不碰共享浏览器、独占 profile）。

    ★ 2026-09-23 修「弹窗闪一下就被关闭、怎么点都登不上」：原实现先看
      is_logged_in()（只看 z_c0 cookie）——知乎把会话登出后 z_c0 仍在
      profile 里，于是一进函数就判定「已登录」，窗口还没画出来就走完
      with 块 close() 掉。现与 verify_zhihu_login 同一口径：先打开知乎
      首页看是否被重定向到登录页，确实失效才打开登录页等用户操作；等待
      期间同样要求「页面已离开登录页」才算成功（否则旧 cookie 会让循环
      第一轮就假成功）。
    """
    with _browser_lock:
        with ZhihuBrowser(headless=False) as browser:
            browser.page.goto(_ZHIHU_HOME, wait_until="domcontentloaded",
                              timeout=_NAV_TIMEOUT)
            time.sleep(2.5)
            if not page_needs_login(browser.page):
                browser.save_storage_state()
                return True, "已登录，登录态已保存"
            log.info("知乎登录态已失效（首页被重定向到登录页），"
                     "打开登录页等待手动登录…")
            browser.page.goto("https://www.zhihu.com/signin",
                              wait_until="domcontentloaded",
                              timeout=_NAV_TIMEOUT)
            token_before = _zhihu_token(browser)   # 登出前的旧凭证（用于判换新）
            deadline = time.time() + timeout
            round_no = 0
            while time.time() < deadline:
                time.sleep(3)
                round_no += 1
                # 本地判据（token 换新 / 页面离开登录页）每轮都查；会话探测
                # 要发一次 HTTP，改成每 4 轮（约 12 秒）一次，别反复打知乎首页
                if zhihu_login_confirmed(browser, token_before=token_before,
                                         probe=(round_no % 4 == 0)):
                    browser.save_storage_state()
                    return True, "检测到登录成功"
            # 临门一脚：判失败前再做一次权威探测——用户可能刚好在最后几秒
            # 登完（2026-09-23 实录：登录窗口 20:56:06 关闭，z_c0 20:56:07
            # 才落盘，差一两秒就白登一次）
            if zhihu_login_confirmed(browser, token_before=token_before,
                                     probe=True):
                browser.save_storage_state()
                return True, "检测到登录成功"
            log.warning("登录引导：等满 %d 分钟仍未判定登录成功（页面 %s）",
                        timeout // 60, getattr(browser.page, "url", "?"))
            return False, f"超时（{timeout // 60} 分钟）未检测到登录"


def main():
    """CLI：python -m applications.zhihu_story.browser_adapter --check-login
    或 --collect-author <作者页URL> --author 镜中花"""
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

    parser = argparse.ArgumentParser(description="知乎 DOM 浏览器通道")
    parser.add_argument("--check-login", action="store_true", help="检查登录态")
    parser.add_argument("--login", action="store_true",
                        help="打开浏览器等待手动登录，登录成功后保存登录态")
    parser.add_argument("--collect-author", metavar="URL", default="",
                        help="作者主页 URL，采集其全部答案")
    parser.add_argument("--author", default="", help="作者名（写入采集库）")
    parser.add_argument("--save-state", action="store_true",
                        help="登录后保存登录态")
    args = parser.parse_args()

    with ZhihuBrowser() as browser:
        if args.check_login:
            logged = browser.is_logged_in()
            print(f"登录态：{'已登录' if logged else '未登录'}")
            if logged and args.save_state:
                browser.save_storage_state()
            return

        if args.login:
            ok, msg = login_zhihu_flow()
            print(msg)
            if not ok:
                sys.exit(1)
            print(f"登录态已保存 → {browser.storage_state}")
            return

        if args.collect_author:
            if not browser.is_logged_in():
                print("❌ 未登录知乎，请先手动登录（--check-login 打开后登录一次）")
                sys.exit(1)
            links = browser.get_author_answer_links(args.collect_author)
            print(f"作者页发现 {len(links)} 篇答案")
            for link in links[:10]:
                print(f"  [{link['likes'] or 0:>4}赞] {link['title']}")

            from applications.zhihu_story.author_profiler import (
                load_author_stories, STORY_LIB)
            existing = load_author_stories(args.author or "")
            seen_titles = {s["title"] for s in existing}
            new_count = 0
            with open(STORY_LIB, "a", encoding="utf-8") as f:
                for link in links:
                    if link["title"] in seen_titles:
                        continue
                    data = browser.get_author_answer(link["href"], args.author)
                    if not data:
                        continue
                    rec = build_story_record(data, args.author)
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    seen_titles.add(rec["title"])
                    new_count += 1
                    print(f"  ✓ 新采集：{rec['title'][:30]}（{len(rec['answer'])}字）")
            print(f"完成：新增 {new_count} 篇")
            return

        parser.print_help()


if __name__ == "__main__":
    main()
