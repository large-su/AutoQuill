# ============================================================
# tests/test_browser_adapter.py — DOM 浏览器适配层测试
#
# 覆盖：链接规整、采集记录构造、JS 提取脚本完整性
# （JS 本身跑在真实浏览器中，这里防 Python 侧回归）
#
# 运行：python -m unittest discover -s tests -v
# ============================================================

import unittest
from unittest import mock

from applications.zhihu_story.browser_adapter import (
    normalize_question_url,
    extract_answer_id,
    build_story_record,
    clean_story_markdown,
    story_markdown_to_html,
    _PRIMARY_ANSWER_JS,
    _AUTHOR_LINKS_JS,
    _RECOMMEND_QUESTIONS_JS,
    set_cancel_hook,
    _check_cancel,
    WorkflowCancelled,
)


class TestCancelHook(unittest.TestCase):
    """Web 控制台「停止」的取消钩子（CLI 模式下无 hook 零影响）。"""

    def tearDown(self):
        set_cancel_hook(None)

    def test_no_hook_noop(self):
        set_cancel_hook(None)
        _check_cancel()  # 不应抛

    def test_hook_false_noop(self):
        set_cancel_hook(lambda: False)
        _check_cancel()

    def test_hook_true_raises(self):
        set_cancel_hook(lambda: True)
        with self.assertRaises(WorkflowCancelled):
            _check_cancel()

    def test_hook_cleared(self):
        set_cancel_hook(lambda: True)
        set_cancel_hook(None)
        _check_cancel()


class TestNormalizeQuestionUrl(unittest.TestCase):
    def test_answer_url_to_question(self):
        self.assertEqual(
            normalize_question_url(
                "//www.zhihu.com/question/660828255/answer/2063252941843206790"),
            "https://www.zhihu.com/question/660828255",
        )

    def test_relative_path(self):
        self.assertEqual(
            normalize_question_url("/question/12345/answer/67890"),
            "https://www.zhihu.com/question/12345",
        )

    def test_full_url(self):
        self.assertEqual(
            normalize_question_url("https://www.zhihu.com/question/42"),
            "https://www.zhihu.com/question/42",
        )

    def test_waiting_tab_rejected(self):
        self.assertIsNone(normalize_question_url(
            "https://www.zhihu.com/question/waiting"))
        self.assertIsNone(normalize_question_url("https://example.com/foo"))

    def test_empty(self):
        self.assertIsNone(normalize_question_url(""))
        self.assertIsNone(normalize_question_url(None))


class TestExtractAnswerId(unittest.TestCase):
    def test_full_answer_url(self):
        self.assertEqual(
            extract_answer_id(
                "https://www.zhihu.com/question/509766383/answer/1942712179510998697"),
            "1942712179510998697")

    def test_protocol_relative(self):
        self.assertEqual(
            extract_answer_id("//www.zhihu.com/question/1/answer/2063252941843206790"),
            "2063252941843206790")

    def test_bare_answer_url(self):
        self.assertEqual(
            extract_answer_id("https://www.zhihu.com/answer/1942712179510998697"),
            "1942712179510998697")

    def test_question_url_rejected(self):
        self.assertIsNone(extract_answer_id("https://www.zhihu.com/question/42"))

    def test_empty(self):
        self.assertIsNone(extract_answer_id(""))
        self.assertIsNone(extract_answer_id(None))


class TestBuildStoryRecord(unittest.TestCase):
    def test_full_record(self):
        data = {"title": "测试题", "answer": "正文内容",
                "footer": {"likes": 100}}
        rec = build_story_record(data, "镜中花")
        self.assertEqual(rec["title"], "测试题")
        self.assertEqual(rec["answer"], "正文内容")
        self.assertEqual(rec["author"], "镜中花")
        self.assertEqual(rec["source"], "author_page_dom")
        self.assertEqual(rec["footer"]["likes"], 100)
        self.assertRegex(rec["collected_at"], r"^\d{4}-\d{2}-\d{2}$")

    def test_missing_footer(self):
        rec = build_story_record({"title": "t", "answer": "a"}, "作者")
        self.assertEqual(rec["footer"], {})

    def test_whitespace_trimmed(self):
        rec = build_story_record({"title": "  t  ", "answer": "  a  "}, "作者")
        self.assertEqual(rec["title"], "t")
        self.assertEqual(rec["answer"], "a")


class TestExtractionScriptsIntact(unittest.TestCase):
    """JS 提取脚本防回归：关键选择器/逻辑被误删时立刻失败。"""

    def test_primary_answer_has_core_selectors(self):
        self.assertIn("QuestionAnswer-content", _PRIMARY_ANSWER_JS)
        self.assertIn("RichContent-inner", _PRIMARY_ANSWER_JS)
        self.assertIn("VoteButton", _PRIMARY_ANSWER_JS)
        self.assertIn("innerText", _PRIMARY_ANSWER_JS)

    def test_primary_answer_expands_collapsed_text(self):
        # 长答案折叠必须有点开逻辑，否则提取到预览文本
        self.assertIn("RichContent-collapsedText", _PRIMARY_ANSWER_JS)
        self.assertIn("click()", _PRIMARY_ANSWER_JS)

    def test_primary_answer_strips_zero_width_space(self):
        # 知乎正文常带零宽空格（段落布局符），注入 prompt 前必须剥离
        self.assertIn("\\u200b-\\u200d\\ufeff", _PRIMARY_ANSWER_JS)

    def test_primary_answer_reads_time_before_expand(self):
        # 发布时间必须在点击"阅读全文"之前读取：展开触发 DOM 重排，
        # 重排后 scope 内查询会落空（2026-08 实测 publish_time 恒空）
        js = _PRIMARY_ANSWER_JS
        time_def = js.find("timeEl")
        expand_def = js.find("RichContent-collapsedText")
        self.assertGreaterEqual(time_def, 0, "时间元素读取逻辑被删")
        self.assertGreaterEqual(expand_def, 0, "展开逻辑被删")
        self.assertLess(time_def, expand_def, "时间必须先于展开读取")
        self.assertIn("document.querySelector", js)
        self.assertIn("ContentItem-time", js)

    def test_author_links_has_core_selectors(self):
        self.assertIn("answer/", _AUTHOR_LINKS_JS)
        self.assertIn("赞同", _AUTHOR_LINKS_JS)
        self.assertIn("条评论", _AUTHOR_LINKS_JS)

    def test_recommend_normalizes_question_url(self):
        self.assertIn("match(/\\/question\\/(\\d+)/)", _RECOMMEND_QUESTIONS_JS)
        self.assertIn("TopstoryItem", _RECOMMEND_QUESTIONS_JS)

    def test_recommend_supports_creator_question_page(self):
        # 创作中心「推荐问题」页（原 workflow 选题入口）卡片结构：
        # .ToolsQuestion 行卡片 + 关注/回答指标，必须能解析
        self.assertIn("ToolsQuestion", _RECOMMEND_QUESTIONS_JS)
        self.assertIn("followers", _RECOMMEND_QUESTIONS_JS)
        self.assertIn("answers", _RECOMMEND_QUESTIONS_JS)
        self.assertIn("回答", _RECOMMEND_QUESTIONS_JS)
        self.assertIn("关注", _RECOMMEND_QUESTIONS_JS)

    def test_recommend_detects_hot_labels(self):
        # 推荐卡片必须能识别热度标签（飙升/火爆/热门），选题环节依赖
        self.assertIn("is_hot", _RECOMMEND_QUESTIONS_JS)
        for kw in ("飙升", "火爆", "热门"):
            self.assertIn(kw, _RECOMMEND_QUESTIONS_JS)

    def test_open_recommend_page_defaults_to_creator_url(self):
        # 选题默认入口必须是创作中心「推荐问题」，不是首页推荐流
        from applications.zhihu_story.browser_adapter import ZhihuBrowser
        import inspect
        src = inspect.getsource(ZhihuBrowser.open_recommend_page)
        self.assertIn("ZHIHU_RECOMMEND_URL", src)
        self.assertIn("url is None", src)

    def test_scripts_are_balanced(self):
        # 粗校验：花括号成对（防手滑删掉一半）
        for js in (_PRIMARY_ANSWER_JS, _AUTHOR_LINKS_JS, _RECOMMEND_QUESTIONS_JS):
            self.assertEqual(js.count("{"), js.count("}"), "花括号不平衡")
            self.assertEqual(js.count("("), js.count(")"), "圆括号不平衡")


class TestSemanticInterfaces(unittest.TestCase):
    """发布/检测语义接口防回归：DOM 驱动、不落回坐标/OCR/对话框。"""

    def _src(self, method_name):
        from applications.zhihu_story.browser_adapter import ZhihuBrowser
        import inspect
        return inspect.getsource(getattr(ZhihuBrowser, method_name))

    def test_check_answerable_uses_dom_text(self):
        src = self._src("check_answerable")
        self.assertIn("撤销删除", src)      # 硬信号：曾删过回答
        self.assertIn("查看我的回答", src)   # 硬信号：已发布过回答
        self.assertIn("写回答", src)         # 软信号：按钮存在
        self.assertIn("innerText", src)

    def test_publish_story_uses_rich_paste_write(self):
        src = self._src("publish_story")
        # 主通道：编辑器 contenteditable + 剪贴板富文本（Draft.js 粘贴
        # 才落盘 <b>，fill 纯文本会把 md 符号原样写进草稿）
        self.assertIn("contenteditable", src)
        self.assertIn("_paste_rich", src)
        self.assertIn("story_markdown_to_html", src)
        self.assertIn("clean_story_markdown", src)
        # 打开编辑器 + 清空旧草稿 + 服务端草稿 API 确认
        self.assertIn("_find_write_button", src)
        self.assertIn("execCommand", src)   # selectAll 清空旧草稿
        self.assertIn("wait_draft_content", src)
        # 保存确认 marker 必须来自清洗后的文本（草稿里没有 md 符号）
        self.assertIn("build_draft_marker(plain", src)
        # 不采用导入上传（知乎程序化导入落盘不可靠）
        self.assertNotIn("set_input_files", src)
        self.assertNotIn("pyautogui", src)
        self.assertNotIn("ctrl", src)

    def test_publish_story_requires_write_button(self):
        src = self._src("publish_story")
        self.assertIn("写回答", src)

    def test_get_primary_answer_retries_on_slow_load(self):
        # 容器缺失/首答过短时重试 + reload 兜底，避免误降级 OCR
        src = self._src("get_primary_answer")
        self.assertIn("retries", src)
        self.assertIn("_wait_answer_container", src)
        self.assertIn("page.reload", src)
        src2 = self._src("_wait_answer_container")
        # 渲染窗口：下滑后轮询等容器出现（500ms×4，最多 ~2s）
        self.assertIn("wait_for_timeout(500)", src2)

    def test_extraction_path_settles_lazy_loading(self):
        # 问题页懒加载：提取前必须先走就绪流程（滚动→展开→稳定），
        # 否则刚进入只有骨架，首答不渲染/只有预览文本
        for method in ("get_primary_answer", "get_author_answer"):
            src = self._src(method)
            self.assertIn("_settle_answer_page", src, method)

    def test_get_author_answer_uses_answer_page_not_question(self):
        # ★ 回归：作者采集必须走独立回答页 /answer/{aid}——链接若被
        #   规整成问题页，提取到的是排名第一的回答而非作者的回答
        src = self._src("get_author_answer")
        self.assertIn("extract_answer_id(answer_url)", src)
        self.assertIn("answer/{aid}", src)   # 独立回答页
        self.assertNotIn("open_question", src)   # 不再经问题页幂等导航
        self.assertNotIn("normalize_question_url", src)

    def test_get_author_answer_links_polls_until_nonempty(self):
        # ★ 回归：无头模式/慢渲染下固定等待窗口常读空——V4.2.2 用户
        #   反馈无头采集 0 篇（「答案列表未出现」×5 后列表读尽，切前台
        #   同作者立即可采）。必须轮询直到链接非空，不再 wait_for_selector
        src = self._src("get_author_answer_links")
        self.assertIn("deadline = time.time() + 20", src)
        self.assertIn("while time.time() < deadline", src)
        self.assertIn("_AUTHOR_LINKS_JS", src)
        self.assertIn("if links:", src)
        self.assertIn("time.sleep(1)", src)
        self.assertNotIn("wait_for_selector", src)   # 固定等待窗口（8s）
        self.assertNotIn("wait_for_timeout", src)

    def test_wait_container_detect_scroll_return_loop(self):
        # 懒加载循环：检测 → 无则下滑触发渲染 → 等渲染完成（最多 ~2s）
        # → 滑回原位 → 再检测。回位是关键：一直下滑会触发无限加载更多
        # 回答、首答 scope 漂移。渲染窗口不得短于渲染耗时：曾固定 1s
        # 即回位，知乎懒加载未完成、检测永远落空（线上事故）
        src = self._src("_wait_answer_container")
        # 下滑触发渲染：分段小步滚动 + 间隔（模拟人手滚轮），
        # 快速瞬间滚动知乎懒加载可能不触发
        self.assertIn("scrollBy(0, stepPx)", src)
        self.assertIn("setTimeout", src)
        self.assertIn("wait_for_timeout(500)", src)
        self.assertIn("scrollTo(0, 0)", src)     # 滑回原位
        self.assertIn("wait_for_timeout(400)", src)

    def test_wait_container_returns_to_top_when_found(self):
        # 检测到容器后也必须滑回原位：提取以第一个回答为 scope
        src = self._src("_wait_answer_container")
        self.assertIn("scrollTo(0, 0)", src)

    def test_settle_expands_first_answer_only(self):
        # 只展开第一个回答的「阅读全文」（后面的回答不是提取目标）；
        # 就绪流程不做任何滚动
        src = self._src("_settle_answer_page")
        self.assertIn("_EXPAND_FIRST_COLLAPSED_JS", src)
        self.assertIn("stable", src)          # 稳定判据（连续两轮不变）
        self.assertIn("_answer_text_len", src)
        self.assertNotIn("scroll", src)       # 展开环节不再滚动

    def test_expand_first_js_targets_first_container(self):
        from applications.zhihu_story.browser_adapter import (
            _EXPAND_FIRST_COLLAPSED_JS)
        self.assertIn(".QuestionAnswer-content, .AnswerItem",
                      _EXPAND_FIRST_COLLAPSED_JS)
        self.assertIn("querySelector('.RichContent-collapsedText')",
                      _EXPAND_FIRST_COLLAPSED_JS)   # 单容器内查询，非全部
        self.assertNotIn("querySelectorAll", _EXPAND_FIRST_COLLAPSED_JS)
        self.assertNotIn("forEach", _EXPAND_FIRST_COLLAPSED_JS)
        self.assertIn("click()", _EXPAND_FIRST_COLLAPSED_JS)
        self.assertEqual(_EXPAND_FIRST_COLLAPSED_JS.count("{"),
                         _EXPAND_FIRST_COLLAPSED_JS.count("}"))

    def test_wait_draft_content_polls_draft_api(self):
        src = self._src("wait_draft_content")
        self.assertIn("get_draft_content", src)   # 轮询服务端草稿 API
        self.assertIn("wait_for_timeout(2000)", src)
        self.assertIn("marker in", src)
        # 草稿是 HTML（段落 <br><br>），必须剥标签+空白后匹配，
        # 否则跨段 marker 永远匹配不上——上次线上失败正是这个原因
        self.assertIn("<[^>]+>", src)

    def test_build_draft_marker_strips_whitespace(self):
        from applications.zhihu_story.browser_adapter import build_draft_marker
        m = build_draft_marker("第一句。\n\n第二句。")
        self.assertEqual(m, "第一句。第二句。")
        self.assertEqual(build_draft_marker("  a  b  "), "ab")

    def test_get_draft_content_reads_top_level_content(self):
        # 草稿 API 的 content 字段在响应顶层（不在 data 里）——取错层级
        # 会把已落盘的草稿误判为空，这是保存检测的关键
        src = self._src("get_draft_content")
        self.assertIn("draft", src)
        self.assertIn("fetch", src)
        self.assertIn("d.content", src)
        self.assertNotIn("d.data", src)

    def test_publish_story_confirms_via_draft_api(self):
        src = self._src("publish_story")
        self.assertIn("wait_draft_content", src)  # 成功判定走草稿 API
        self.assertIn("build_draft_marker", src)
        self.assertNotIn("草稿已保存", src)

    def test_publish_story_uses_contenteditable(self):
        src = self._src("publish_story")
        self.assertIn("contenteditable", src)
        # 富文本粘贴：真实 Ctrl+V 键事件（fill 只在剪贴板失败的降级里）
        paste_src = self._src("_paste_rich")
        self.assertIn("Control+V", paste_src)
        self.assertIn("ClipboardItem", paste_src)
        # 发布也要自己打开编辑器，且不得走坐标点击
        self.assertIn("_find_write_button", src)
        self.assertNotIn("editor.click", src)

    def test_publish_story_waits_longer_for_write_button(self):
        # 首次按钮等待 20s，避免慢加载下反复 reload 制造多次导航
        src = self._src("publish_story")
        self.assertIn("_find_write_button(timeout=20)", src)

    def test_open_question_idempotent_same_url(self):
        # 同一问题页重进时跳过 goto（不再整页重载），发布路径单跳定位
        src = self._src("open_question")
        self.assertIn("normalize_question_url(self.page.url)", src)
        self.assertIn("current == target", src)
        self.assertIn("page.goto", src)

    def test_all_page_interactions_are_bounded(self):
        # 历史事故：风控页/加载中页面的 evaluate 无限阻塞，整条 workflow
        # 挂死数分钟无日志。所有页面交互必须走 _safe_evaluate（带超时），
        # 裸 page.evaluate 只允许存在于 browser_pool.safe_evaluate 自身。
        import inspect
        from applications.zhihu_story.browser_adapter import ZhihuBrowser
        from web_drivers.browser_pool import safe_evaluate
        cls_src = inspect.getsource(ZhihuBrowser)
        self.assertNotIn("page.evaluate(", cls_src,
                         "存在无超时的裸 page.evaluate 调用")
        # Playwright 1.62 evaluate 无 timeout 参数且 sync API 有线程亲和
        # 性：用 JS 内部 Promise.race 自限时哨兵实现有界等待（唯一实现
        # 下沉 web_drivers/browser_pool，类方法/驱动基类均委托它）
        pool_src = inspect.getsource(safe_evaluate)
        self.assertIn("Promise.race", pool_src)
        self.assertIn("__aq_timeout__", pool_src)
        self.assertIn("return None", pool_src)

    def test_extraction_path_uses_safe_evaluate(self):
        # 提取/发布主链路方法必须经 _safe_evaluate 走有界等待
        for method in ("get_primary_answer", "get_author_answer",
                       "get_author_answer_links", "check_answerable",
                       "_find_write_button", "get_draft_content",
                       "_wait_answer_container"):
            src = self._src(method)
            self.assertIn("self._safe_evaluate(", src, method)

    def test_navigation_has_timeout(self):
        # goto/reload 必须有导航超时，慢页面/风控页不能无限等
        for method in ("open_question", "open_recommend_page",
                       "get_author_answer_links"):
            src = self._src(method)
            self.assertIn("timeout=_NAV_TIMEOUT", src, method)

    def test_story_markdown_to_html(self):
        md = "开头一段。\n\n## **1**\n\n第二段有**加粗**和普通。"
        html = story_markdown_to_html(md)
        self.assertIn("<p>开头一段。</p>", html)
        self.assertIn("<p><b>1</b></p>", html)
        self.assertIn("<b>加粗</b>", html)

    def test_clean_story_markdown_strips_symbols(self):
        md = "开头一段。\n\n## **1**\n\n第二段有**加粗**和普通。"
        self.assertEqual(
            clean_story_markdown(md),
            "开头一段。\n\n1\n\n第二段有加粗和普通。")
        self.assertEqual(clean_story_markdown(""), "")
        self.assertEqual(clean_story_markdown("## 标题"), "标题")

    def test_find_write_button_polls_with_retry(self):
        src = self._src("_find_write_button")
        self.assertIn("wait_for_timeout(1000)", src)   # 轮询重试
        self.assertIn("deadline", src)
        self.assertIn("btn.click()", src)              # DOM 直点

    def test_scroll_feed_is_js_scroll(self):
        src = self._src("scroll_feed")
        self.assertIn("scrollBy", src)
        self.assertNotIn("keyboard", src)   # 不允许键盘翻页，必须 JS 滚动

    def test_scroll_feed_inlines_pixels_no_arguments(self):
        # 回归：曾用 page.evaluate("scrollBy(0, arguments[0])", px)，
        # Playwright 把字符串包装成箭头函数，arguments 不可用 → ReferenceError，
        # 滚动扩池从未生效。必须内插数值（函数形式与 _safe_evaluate 包装兼容）。
        src = self._src("scroll_feed")
        self.assertNotIn("arguments[0]", src)
        self.assertIn("f\"() => window.scrollBy(0, {int(pixels)})\"", src)


class TestClickByText(unittest.TestCase):
    """click(text=...) 的 JS 防回归：零宽空格剥离、原生 click 触发。"""

    def test_click_js_strips_zero_width_space(self):
        from applications.zhihu_story.browser_adapter import ZhihuBrowser
        import inspect
        src = inspect.getsource(ZhihuBrowser.click)
        self.assertIn("u200b", src)
        self.assertIn("el.click()", src)

    def test_click_uses_native_dom_click_not_page_click(self):
        # 关键约束：不能回退到 playwright 坐标式 page.click
        from applications.zhihu_story.browser_adapter import ZhihuBrowser
        import inspect
        src = inspect.getsource(ZhihuBrowser.click)
        self.assertNotIn("self.page.click", src)
        self.assertIn("_safe_evaluate", src)  # DOM 直点 + 有界等待


class TestWebLlmLoggedIn(unittest.TestCase):
    """web_llm_logged_in 真实登录判定：cookie 存在 + 页面不在登录页。

    ★ 回归：仅查 cookie 会假阳性（过期/无效 cookie 残留），预检放行
    切 Web，运行才撞登录页（线上：页面停在 chat.deepseek.com/sign_in，
    报「找不到 DeepSeek 输入框」，且切换时不弹登录引导）。"""

    def _run(self, cookies, page_url, goto_error=None):
        from unittest import mock
        from applications.zhihu_story import browser_adapter as mod

        class _Page:
            url = page_url

            def goto(self, *a, **k):
                if goto_error:
                    raise goto_error
                _Page.url = page_url  # 模拟 SPA 落地后的最终 URL

            def wait_for_timeout(self, *a):
                pass

            def close(self):
                pass

        class _Ctx:
            def __init__(self):
                self.page = _Page()

            def cookies(self):
                return cookies

            def new_page(self):
                return self.page

        class _Browser:
            def __init__(self, ctx):
                self.context = ctx

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def close(self):
                pass

        import web_drivers.deepseek as ds
        ctx = _Ctx()
        with mock.patch.object(mod, "ZhihuBrowser",
                               return_value=_Browser(ctx)):
            return ds.web_llm_logged_in(), ctx

    def test_no_cookies_false(self):
        ok, _ = self._run([], "https://chat.deepseek.com/")
        self.assertFalse(ok)

    def test_cookies_but_stuck_on_sign_in_false(self):
        # ★ 线上场景：cookie 残留 + 页面停在登录页 → 必须判未登录
        ok, _ = self._run(
            [{"domain": "chat.deepseek.com", "value": "x"}],
            "https://chat.deepseek.com/sign_in")
        self.assertFalse(ok)

    def test_cookies_and_home_true(self):
        ok, _ = self._run(
            [{"domain": "chat.deepseek.com", "value": "x"}],
            "https://chat.deepseek.com/")
        self.assertTrue(ok)

    def test_browser_failure_false(self):
        ok, _ = self._run([], "", goto_error=RuntimeError("boom"))
        self.assertFalse(ok)


class TestGetBrowserConcurrency(unittest.TestCase):
    """get_browser 懒启动并发安全。

    ★ 回归（V4.1.4 线上）：并发调用时多个线程同时 start()，互抢同一
    Chromium profile；失败实例（context=None）在 start() 抛错前已
    赋给 _shared_browser → 登录引导永久复用坏实例报
    'NoneType' object has no attribute 'new_page'。"""

    def setUp(self):
        from applications.zhihu_story import browser_adapter as mod
        from web_drivers import browser_pool as bp
        self.mod = mod
        self.bp = bp
        bp._shared_browser = None

    def tearDown(self):
        self.bp._shared_browser = None

    def test_concurrent_calls_start_once(self):
        from unittest import mock
        import threading

        b = self.mod.ZhihuBrowser()
        b.context = mock.Mock()
        started = []

        def _start():
            started.append(1)

        b.start = _start
        with mock.patch.object(self.mod, "ZhihuBrowser", return_value=b):
            results = [None] * 8

            def _get(i):
                results[i] = self.mod.get_browser()

            threads = [threading.Thread(target=_get, args=(i,))
                       for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(started), 1)  # 并发下只启动一次
        self.assertTrue(all(r is results[0] for r in results))

    def test_failed_start_not_cached(self):
        from unittest import mock

        class _Broken:
            def start(self):
                raise RuntimeError("profile locked")

        with mock.patch.object(self.mod, "ZhihuBrowser",
                               return_value=_Broken()):
            with self.assertRaises(RuntimeError):
                self.mod.get_browser()
        # 坏实例未落盘：_shared_browser 保持 None，下次可重试
        self.assertIsNone(self.bp._shared_browser)

        ok = mock.Mock()
        ok.context = mock.Mock()
        with mock.patch.object(self.mod, "ZhihuBrowser",
                               return_value=ok):
            self.assertIs(self.mod.get_browser(), ok)

    def test_stale_context_rebuilt(self):
        # 异常路径残留的 context=None 实例 → 自动重建而非复用
        from unittest import mock
        stale = mock.Mock()
        stale.context = None
        self.bp._shared_browser = stale
        fresh = mock.Mock()
        fresh.context = mock.Mock()
        with mock.patch.object(self.mod, "ZhihuBrowser",
                               return_value=fresh):
            self.assertIs(self.mod.get_browser(), fresh)


class TestLoginFlows(unittest.TestCase):
    """登录引导流程用独立可见实例 + 全程持锁。

    ★ 回归（V4.1.5 线上）：login 复用 get_browser 的共享浏览器——
    登录线程创建后退出，用户再次点击登录时新线程复用该实例报
    Playwright「cannot switch to a different thread (which happens
    to have exited)」；且共享浏览器存活期间，登录态检查的独立实例
    同 profile 并发互杀（Target page closed）。"""

    def _src(self, fn_name, mod=None):
        import inspect
        from applications.zhihu_story import browser_adapter as mod_
        return inspect.getsource(getattr(mod or mod_, fn_name))

    def test_deepseek_login_uses_dedicated_visible_browser(self):
        import inspect
        import web_drivers.deepseek as ds
        src = inspect.getsource(ds.login_deepseek_web_flow)
        self.assertIn("create_browser(headless=False)", src)  # 可见实例
        self.assertIn("_browser_lock", src)                 # 独占 profile
        self.assertNotIn("get_browser(", src)               # 不碰共享实例

    def test_zhihu_login_uses_dedicated_visible_browser(self):
        src = self._src("login_zhihu_flow")
        self.assertIn("ZhihuBrowser(headless=False)", src)
        self.assertIn("_browser_lock", src)
        self.assertNotIn("get_browser(", src)


class _LoginFakeResponse:
    """假 HTTP 响应：probe_ok=True → 200（会话有效），否则 302 到登录页。"""

    def __init__(self, ok):
        self.status = 200 if ok else 302
        self.headers = {} if ok else {"location": "//www.zhihu.com/signin?next=%2F"}


class _LoginFakeRequest:
    """假 context.request：只回答知乎首页探测。"""

    def __init__(self, probe_ok=False):
        self.probe_ok = probe_ok
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1
        return _LoginFakeResponse(self.probe_ok)


class _LoginFakeContext:
    """假 context：页面列表 + cookie 罐 + HTTP 客户端（探测用）。"""

    def __init__(self, pages, token="tok-old", probe_ok=False):
        self.pages = pages
        self.token = token
        self.request = _LoginFakeRequest(probe_ok)

    def cookies(self, url=None):
        return [{"name": "z_c0", "value": self.token}] if self.token else []


class _LoginFakePage:
    """登录引导用假页面：每次 goto 从脚本里取一个落点（取完就用请求 URL）。"""

    def __init__(self, urls):
        self._urls = list(urls)
        self.url = "about:blank"
        self.gotos = []

    def goto(self, url, **kw):
        self.gotos.append(url)
        self.url = self._urls.pop(0) if self._urls else url
        return None


class _LoginFakeBrowser:
    """假的可见 Edge：页面落点、凭证 cookie、探测结果都由测试给。"""

    def __init__(self, urls, token="tok-old", probe_ok=False, pages=None):
        self.page = _LoginFakePage(urls)
        self.context = _LoginFakeContext(
            pages if pages is not None else [self.page],
            token=token, probe_ok=probe_ok)
        self.saved = False
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def is_logged_in(self):
        return bool(self.context.token)

    def save_storage_state(self, path=None):
        self.saved = True


class TestZhihuLoginFlow(unittest.TestCase):
    """登录引导回归（2026-09-23 两次事故）。

    事故一：知乎把会话登出后 z_c0 仍留在 profile 里，登录引导一进函数就判定
    「已登录」→ 窗口闪一下就被关闭，用户怎么点都登不上（只看 cookie）。
    事故二：登录在别处完成（新标签页/页面没跳转）时判定不出来 → 等满 5 分钟
    也不保存、不关窗（只看当前页）。
    """

    def _run(self, urls, token="tok-old", probe_ok=False, timeout=0,
             flip_after_sleep=0, retoken_after_sleep=0):
        """跑一次登录引导，返回 (结果, 假浏览器)。

        flip_after_sleep：第 N 次 sleep 后把页面落点改成知乎首页（模拟扫码
        登录后页面跳转）；retoken_after_sleep：第 N 次 sleep 后换发新凭证
        （模拟「登录接口换了 token 但页面还没跳」）。
        """
        holder = {}

        def _factory(**kw):
            b = _LoginFakeBrowser(urls, token=token, probe_ok=probe_ok)
            holder["b"] = b
            return b

        steps = {"n": 0}

        def _sleep(_sec):
            steps["n"] += 1
            b = holder["b"]
            if flip_after_sleep and steps["n"] >= flip_after_sleep:
                b.page.url = "https://www.zhihu.com/"
            if retoken_after_sleep and steps["n"] >= retoken_after_sleep:
                b.context.token = "tok-new"

        import applications.zhihu_story.browser_adapter as ba
        with mock.patch.object(ba, "ZhihuBrowser", _factory):
            with mock.patch.object(ba.time, "sleep", _sleep):
                result = ba.login_zhihu_flow(timeout=timeout)
        return result, holder["b"]

    # ---- 事故一的回归：陈旧 cookie 不能秒回成功 ----

    def test_stale_cookie_does_not_short_circuit(self):
        """cookie 还在但首页被 302 到登录页 → 走等待流程，绝不秒回「已登录」。"""
        urls = ["https://www.zhihu.com/signin?next=%2F"]
        (ok, msg), b = self._run(urls, timeout=0)
        self.assertFalse(ok)
        self.assertIn("超时", msg)
        self.assertFalse(b.saved)                    # 没有假成功、没有覆盖登录态
        self.assertTrue(b.closed)
        self.assertIn("https://www.zhihu.com/signin", b.page.gotos)  # 真开了登录页

    def test_already_logged_in_still_saves_and_closes(self):
        """首页没跳登录页 = 确实登录着 → 保存登录态后正常收工。"""
        (ok, msg), b = self._run(["https://www.zhihu.com/"])
        self.assertTrue(ok)
        self.assertIn("已登录", msg)
        self.assertTrue(b.saved)
        self.assertEqual(b.page.gotos, ["https://www.zhihu.com/"])

    # ---- 事故二的回归：登录在别处完成也要能判定 ----

    def test_login_completed_while_waiting(self):
        """等待期间页面跳到首页 → 判成功并保存。"""
        urls = ["https://www.zhihu.com/signin?next=%2F"]
        (ok, msg), b = self._run(urls, timeout=9, flip_after_sleep=2)
        self.assertTrue(ok)
        self.assertIn("检测到登录成功", msg)
        self.assertTrue(b.saved)

    def test_session_probe_catches_login_on_another_tab(self):
        """页面一直停在登录页，但会话探测显示已登录（用户在新标签页登录）。

        这正是用户 2026-09-23 实测的现象：登完了，窗口却一直不关。判据三
        （用共享 cookie 的 HTTP 客户端问首页）就是为它准备的。
        """
        urls = ["https://www.zhihu.com/signin?next=%2F"]
        (ok, msg), b = self._run(urls, timeout=9, probe_ok=True)
        self.assertTrue(ok)
        self.assertIn("检测到登录成功", msg)
        self.assertTrue(b.saved)
        self.assertTrue(b.context.request.calls > 0)

    def test_new_token_counts_as_login(self):
        """凭证被换新（登录接口已发新 token，页面还没跳）= 登录成功。"""
        urls = ["https://www.zhihu.com/signin?next=%2F"]
        (ok, msg), b = self._run(urls, timeout=9, retoken_after_sleep=2)
        self.assertTrue(ok)
        self.assertIn("检测到登录成功", msg)
        self.assertTrue(b.saved)

    def test_page_leaves_signin_without_cookie_is_not_success(self):
        """没有 z_c0 时一律不算成功（页面变了、探测通过也不认）。"""
        urls = ["https://www.zhihu.com/signin?next=%2F"]
        # timeout 取小值：这一轮必然等满整个等待窗口，别让单测干等 10 秒
        (ok, _msg), b = self._run(urls, token="", timeout=1,
                                  flip_after_sleep=2, probe_ok=True)
        self.assertFalse(ok)
        self.assertFalse(b.saved)

    def test_stale_token_with_signin_page_and_bad_probe_waits(self):
        """三条判据都不成立时只能等（宁可不关窗，也不能假成功）。"""
        urls = ["https://www.zhihu.com/signin?next=%2F"]
        (ok, _msg), b = self._run(urls, token="tok-old", timeout=0,
                                  probe_ok=False)
        self.assertFalse(ok)
        self.assertFalse(b.saved)

    # ---- 判据本身 ----

    def test_confirm_rules(self):
        """zhihu_login_confirmed 三条判据（都要先有凭证 cookie）。"""
        from applications.zhihu_story.browser_adapter import zhihu_login_confirmed
        signin = "https://www.zhihu.com/signin"
        home = "https://www.zhihu.com/"

        def _b(page_url, token, probe_ok=False, token_before=""):
            page = _LoginFakePage([])
            page.url = page_url
            fake = _LoginFakeBrowser([], token=token, probe_ok=probe_ok,
                                     pages=[page])
            return zhihu_login_confirmed(fake, token_before=token_before)

        self.assertFalse(_b(signin, ""))                       # 无凭证 cookie
        self.assertFalse(_b(home, ""))                         # 无凭证 cookie
        self.assertFalse(_b(signin, "tok-old"))                # 页面在登录页 + 探测不过
        self.assertTrue(_b(home, "tok-old"))                   # 判据二：页面离开登录页
        self.assertTrue(_b(signin, "tok-old", probe_ok=True))  # 判据三：探测通过
        self.assertTrue(_b(signin, "tok-new", token_before="tok-old"))  # 判据一：换新
        self.assertTrue(_b(home, "tok-new", token_before="tok-old"))

    def test_last_second_login_is_not_lost(self):
        """刚好在等待窗口结束前登完（页面没跳、探测这时才通）→ 也要保存。

        2026-09-23 实录：登录窗口 20:56:06 关闭，z_c0 20:56:07 才落盘——
        差一两秒就白登一次，所以判失败前必须再权威探测一次。
        """
        urls = ["https://www.zhihu.com/signin?next=%2F"]
        (ok, msg), b = self._run(urls, timeout=0, probe_ok=True)
        self.assertTrue(ok)
        self.assertIn("检测到登录成功", msg)
        self.assertTrue(b.saved)

    def test_source_no_longer_trusts_cookie_only(self):
        """源码锚点：登录判定必须走页面证据，别退回「只看 cookie」。"""
        import inspect
        from applications.zhihu_story import browser_adapter as ba
        src = inspect.getsource(ba.login_zhihu_flow)
        self.assertIn("page_needs_login", src)
        self.assertIn("zhihu_login_confirmed", src)
        self.assertNotIn("if browser.is_logged_in():", src)


class TestLoadStorageStateMerge(unittest.TestCase):
    """登录态文件只补缺、绝不覆盖（2026-09-23 修「登了等于没登」）。

    事故：profile 里刚登录出的新 z_c0，被这份陈旧文件里的旧 z_c0 盖掉——
    登录窗口 20:56:06 一关，20:56:07 网页版登录检查启动浏览器时又把旧
    cookie 写回，用户白登一次。
    """

    def setUp(self):
        import json
        import os
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="aq_state_")
        self.path = os.path.join(self.tmp, "browser_state.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"cookies": [
                {"name": "z_c0", "domain": ".zhihu.com", "path": "/",
                 "value": "old-token"},
                {"name": "d_c0", "domain": ".zhihu.com", "path": "/",
                 "value": "d"},
            ]}, f)

    class _Ctx:
        def __init__(self, live):
            self.live = live
            self.added = []

        def cookies(self, url=None):
            return [{"name": n, "domain": d, "path": p,
                     "value": "new-token"} for n, d, p in self.live]

        def add_cookies(self, cks):
            self.added.extend(cks)

    def _browser(self, live):
        from applications.zhihu_story.browser_adapter import ZhihuBrowser
        b = ZhihuBrowser.__new__(ZhihuBrowser)       # 不启动真实浏览器
        b.context = self._Ctx(live)
        b.storage_state = self.path
        return b

    def test_existing_cookie_is_never_overwritten(self):
        """profile 里已有 z_c0（新登录的）→ 文件里的旧 z_c0 不许覆盖它。"""
        b = self._browser([("z_c0", ".zhihu.com", "/")])
        self.assertTrue(b.load_storage_state())
        names = [c["name"] for c in b.context.added]
        self.assertNotIn("z_c0", names)              # 关键：不覆盖新登录态
        self.assertIn("d_c0", names)                 # 缺的照样补

    def test_empty_profile_gets_everything(self):
        """空 profile（首次启动/换机器）→ 全量补，行为与以前一致。"""
        b = self._browser([])
        self.assertTrue(b.load_storage_state())
        self.assertEqual(sorted(c["name"] for c in b.context.added),
                         ["d_c0", "z_c0"])

    def test_missing_file_is_graceful(self):
        b = self._browser([])
        b.storage_state = self.path + ".nope"
        self.assertFalse(b.load_storage_state())
        self.assertEqual(b.context.added, [])


if __name__ == "__main__":
    unittest.main()
