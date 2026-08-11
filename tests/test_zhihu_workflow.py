# ============================================================
# tests/test_zhihu_workflow.py — 知乎工作流 DOM 化回归测试
#
# 核心约束：workflows/zhihu.py 必须完全走 browser_adapter 的
# DOM 语义接口，不得出现 pyautogui / 坐标 / OCR 主通道调用。
# （浏览器内的真实行为由浏览器测试覆盖，这里防 Python 侧退化。）
#
# 运行：python -m unittest discover -s tests -v
# ============================================================

import inspect
import unittest


class TestZhihuWorkflowDomOnly(unittest.TestCase):
    """workflow 必须与物理鼠标/坐标/OCR 解绑。"""

    def test_no_pyautogui_in_workflow(self):
        with open("workflows/zhihu.py", encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("pyautogui", src)
        self.assertNotIn("import pyautogui", src)

    def test_no_coordinate_clicks(self):
        with open("workflows/zhihu.py", encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("click_x", src)
        self.assertNotIn("click_y", src)

    def test_imports_browser_adapter_only(self):
        with open("workflows/zhihu.py", encoding="utf-8") as f:
            src = f.read()
        # 允许 get_browser；桌面通道只允许出现在 UIA/OCR 降级函数里
        self.assertIn("get_browser", src)
        main_part = src.split("def _extract_answer_with_fallback")[0]
        self.assertNotIn("desktop_utils", main_part)


class TestZhihuWorkflowSemantics(unittest.TestCase):
    """workflow 各步骤的 DOM 语义接线。"""

    @classmethod
    def setUpClass(cls):
        from workflows.zhihu import ZhihuWorkflow
        cls.wf = ZhihuWorkflow()

    def _src(self, method_name):
        return inspect.getsource(getattr(self.wf, method_name))

    def test_init_reads_author_profile_config(self):
        from applications.zhihu_story import config
        # self.author 必须来自 config.AUTHOR_PROFILE（作者技能注入开关）
        self.assertEqual(self.wf.author, config.AUTHOR_PROFILE or None)

    def test_select_topic_requires_login_and_scans(self):
        src = self._src("select_topic")
        self.assertIn("_require_login", src)
        self.assertIn("_select_auto", src)
        self.assertIn("_select_manual", src)
        login_src = self._src("_require_login")
        self.assertIn("is_logged_in", login_src)

    def test_auto_select_uses_dom_score_and_open_question(self):
        src = self._src("_select_auto")
        self.assertIn("open_question", src)
        scan_src = self._src("_scan_recommend")
        self.assertIn("_dom_score", scan_src)
        self.assertIn("get_recommend_questions", scan_src)

    def test_publish_forces_fresh_navigation(self):
        # 发布前必须强制 goto（生成耗时长，页面可能漂移）；幂等跳过
        # 只属于提取环节的重进
        src = self._src("publish")
        self.assertIn('open_question(url, force=True)', src)

    def test_pick_best_never_falls_back_to_unfiltered(self):
        # ★ 线上翻车回归：筛选为空时曾回退未筛选列表按分数选，
        #   导致选到「美伊战争」这类非故事话题。现在必须返回 None。
        src = self._src("_pick_best")
        self.assertIn("return None", src)
        self.assertNotIn("candidates = all_questions", src)
        self.assertNotIn("best = all_questions[0]", src)
        self.assertNotIn("best = hot_questions[0]", src)

    def test_auto_select_scrolls_when_filter_empty_then_raises(self):
        # 筛选为空 → 滚动扩池重扫（MAX_SELECT_SCREENS 屏）→ 仍空报错
        src = self._src("_select_auto")
        self.assertIn("MAX_SELECT_SCREENS", src)
        self.assertIn("scroll_feed", src)
        self.assertIn("RuntimeError", src)
        self.assertIn("manual", src)   # 报错信息指引手动模式

    def test_dom_score_formula(self):
        from workflows.zhihu import ZhihuWorkflow
        # likes×(comments+1)，热度 ×2
        self.assertEqual(ZhihuWorkflow._dom_score({"likes": 100, "comments": 4}),
                         500)
        self.assertEqual(
            ZhihuWorkflow._dom_score({"likes": 100, "comments": 4, "is_hot": True}),
            1000)

    def test_dom_score_supports_creator_page_signals(self):
        # 创作中心推荐页无赞/评，用 关注×（回答+1） 自适应评分
        from workflows.zhihu import ZhihuWorkflow
        self.assertEqual(
            ZhihuWorkflow._dom_score({"followers": 11000, "answers": 3988}),
            11000 * 3989)
        self.assertEqual(ZhihuWorkflow._dom_score({}), 0)

    def test_extract_uses_check_answerable_and_dom_extract(self):
        src = self._src("extract_content")
        self.assertIn("check_answerable", src)     # DOM 检测「撤销删除」
        self.assertIn("get_primary_answer", src)   # DOM 提取首答
        self.assertIn("normalize_question_url", src)

    def test_extract_has_fallback_but_dom_is_first(self):
        src = self._src("extract_content")
        # DOM 主通道在前，UIA/OCR 降级在后
        self.assertLess(src.index("get_primary_answer"),
                        src.index("_extract_answer_with_fallback"))

    def test_extract_retopics_on_short_answer(self):
        # 首答过短/不可回答时重新选题再试，而不是直接降级 OCR
        # （本机 OCR 未校准，降级即崩溃）
        src = self._src("extract_content")
        self.assertIn("select_topic", src)
        self.assertIn("MAX_TOPIC_RETRY", src)
        self.assertIn("MIN_ANSWER_LENGTH", src)

    def test_extract_returns_final_url_for_publish(self):
        # ★ 回归：不可回答重选题后，必须返回最终实际提取问题的 URL。
        #   曾沿用首次选题 URL → 发布导航到被「撤销删除」跳过的旧题
        #   而失败（找不到「写回答」按钮，E2E 线上事故）。
        src = self._src("extract_content")
        self.assertIn("final_url", src)
        self.assertIn("normalize_question_url(browser.page.url) or url", src)
        # 两条成功路径（DOM 成功 / 降级成功）都必须带 URL 返回
        self.assertIn("return title, answer, footer, final_url", src)

    def test_run_single_uses_extracted_url_not_initial(self):
        # ★ 回归：run_single 的发布 URL 必须来自 extract_content 的
        #   最终返回值，而不是首次 select_topic 的 URL（重选题场景）。
        from workflows.base import WorkflowBase
        src = inspect.getsource(WorkflowBase.run_single)
        self.assertIn(
            "title, answer, _footer, url = self.extract_content()", src)

    def test_publish_uses_editor_write_channel(self):
        src = self._src("publish")
        # 主通道：编辑器直接写入故事全文（可验证的可靠通道）
        self.assertIn("publish_story", src)
        # 不做导入上传：上传 API 全 200 但服务端草稿不更新，不可靠
        self.assertNotIn("import_document", src)
        self.assertNotIn("set_input_files", src)
        # 保存确认走服务端草稿 API（前端 toast 在程序化写入后不可靠）
        self.assertNotIn("wait_draft_save", src)
        self.assertIn("save_story_file", src)

    def test_publish_has_no_trailing_reload(self):
        # 发布成功后不再 reload 收尾：多余页面加载对用户观感就是
        # 「又跳了一下」，且破坏验收时的编辑器态
        src = self._src("publish")
        self.assertNotIn("page.reload", src)

    def test_collect_uses_tabs_not_middle_click(self):
        src = self._src("collect_materials_batch")
        self.assertIn("open_new_page", src)    # 新开 tab
        self.assertIn("switch_page", src)
        self.assertIn("scroll_feed", src)      # JS 滚动
        self.assertNotIn("pagedown", src.lower())
        self.assertNotIn("hotkey", src.lower())
        self.assertNotIn("pyautogui", src)


class TestAuthorInjectionInBase(unittest.TestCase):
    """作者技能注入必须贯穿基类全部生成路径。"""

    def _src(self, method_name):
        from workflows.base import WorkflowBase
        return inspect.getsource(getattr(WorkflowBase, method_name))

    def test_generate_api_passes_author(self):
        src = self._src("_generate_api")
        self.assertIn('author = getattr(self, "author", None)', src)
        self.assertIn("author=author", src)

    def test_web_short_form_injects_author_profile(self):
        src = self._src("_generate_web_short_form")
        self.assertIn("author_profile", src)
        self.assertIn("_load_author_profile_or_none", src)

    def test_batch_generate_api_passes_author(self):
        src = self._src("_batch_generate_api")
        self.assertIn('author=getattr(self, "author", None)', src)

    def test_batch_retry_api_passes_author(self):
        src = self._src("_batch_retry_api")
        self.assertIn('author=getattr(self, "author", None)', src)
        self.assertIn("mat.get('recipe')", src)  # 防配方参数被误改

    def test_batch_web_parallel_injects_author_profile(self):
        src = self._src("_batch_generate_web_parallel")
        self.assertIn("author_profile", src)

    def test_batch_retry_web_parallel_injects_author_profile(self):
        src = self._src("_batch_retry_web_parallel")
        self.assertIn("author_profile", src)


if __name__ == "__main__":
    unittest.main()
