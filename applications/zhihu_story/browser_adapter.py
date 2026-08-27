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


def login_zhihu_flow(timeout=300):
    """打开可见 Edge 窗口引导用户手动登录知乎，检测到登录后保存登录态。

    供 CLI（--login）与 Web 首启引导（/api/setup/zhihu-login）共用。
    返回 (是否成功, 提示信息)。独立实例 + 持 _browser_lock（与
    login_deepseek_web_flow 同理：不碰共享浏览器、独占 profile）。"""
    with _browser_lock:
        with ZhihuBrowser(headless=False) as browser:
            if browser.is_logged_in():
                browser.save_storage_state()
                return True, "已登录，登录态已保存"
            browser.page.goto("https://www.zhihu.com/signin",
                              wait_until="domcontentloaded")
            deadline = time.time() + timeout
            while time.time() < deadline:
                time.sleep(3)
                if browser.is_logged_in():
                    break
            else:
                return False, f"超时（{timeout // 60} 分钟）未检测到登录"
            browser.save_storage_state()
            return True, "检测到登录成功"


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
