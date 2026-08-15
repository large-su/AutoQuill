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
        # 允许 get_browser；workflow 不允许出现任何桌面/OCR 通道代码
        self.assertIn("get_browser", src)
        self.assertNotIn("desktop_utils", src)
        self.assertNotIn("ocr_utils", src)

    def test_no_uia_ocr_fallback(self):
        # ★ V4.0.2：UIA/OCR 屏幕降级通道已移除，workflow 纯 DOM
        with open("workflows/zhihu.py", encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("_extract_answer_with_fallback", src)
        self.assertNotIn("UiaAnswerExtractor", src)
        self.assertNotIn("OcrAnswerExtractor", src)
        self.assertNotIn("FallbackAnswerExtractor", src)
        self.assertNotIn("load_coords", src)


class TestMaterialLikesGate(unittest.TestCase):
    """点赞门槛共享判定（batch 与 single 同源）。"""

    @classmethod
    def setUpClass(cls):
        from workflows.zhihu import ZhihuWorkflow
        cls.wf = ZhihuWorkflow()

    def test_gate_disabled_passes_all(self):
        from config import story
        orig = story.ENABLE_MATERIAL_LIKES_GATE
        try:
            story.ENABLE_MATERIAL_LIKES_GATE = False
            ok, _ = self.wf._material_likes_pass(0, 200)
            self.assertTrue(ok)
            ok, _ = self.wf._material_likes_pass(None, 200)
            self.assertTrue(ok)
        finally:
            story.ENABLE_MATERIAL_LIKES_GATE = orig

    def test_below_minimum_rejected(self):
        from config import story
        orig = story.ENABLE_MATERIAL_LIKES_GATE
        try:
            story.ENABLE_MATERIAL_LIKES_GATE = True
            ok, reason = self.wf._material_likes_pass(100, 200)
            self.assertFalse(ok)
            self.assertIn("100 < 200", reason)
        finally:
            story.ENABLE_MATERIAL_LIKES_GATE = orig

    def test_at_or_above_minimum_passes(self):
        from config import story
        orig = story.ENABLE_MATERIAL_LIKES_GATE
        try:
            story.ENABLE_MATERIAL_LIKES_GATE = True
            ok, _ = self.wf._material_likes_pass(200, 200)
            self.assertTrue(ok)
            ok, _ = self.wf._material_likes_pass(1089, 200)
            self.assertTrue(ok)
        finally:
            story.ENABLE_MATERIAL_LIKES_GATE = orig

    def test_unknown_likes_follows_policy(self):
        from config import story
        orig_gate = story.ENABLE_MATERIAL_LIKES_GATE
        orig_policy = story.MATERIAL_UNKNOWN_LIKES_POLICY
        try:
            story.ENABLE_MATERIAL_LIKES_GATE = True
            story.MATERIAL_UNKNOWN_LIKES_POLICY = "drop"
            ok, reason = self.wf._material_likes_pass(None, 200)
            self.assertFalse(ok)
            self.assertIn("drop", reason)
            story.MATERIAL_UNKNOWN_LIKES_POLICY = "keep"
            ok, reason = self.wf._material_likes_pass(None, 200)
            self.assertTrue(ok)
            self.assertIn("keep", reason)
        finally:
            story.ENABLE_MATERIAL_LIKES_GATE = orig_gate
            story.MATERIAL_UNKNOWN_LIKES_POLICY = orig_policy

    def test_extract_content_applies_gate(self):
        # single 路径（extract_content）必须应用同一门槛并重新选题
        src = inspect.getsource(self.wf.extract_content)
        self.assertIn("_material_likes_pass", src)
        self.assertIn("MATERIAL_MIN_LIKES", src)
        self.assertIn("重新选题", src)
        # DOM 成功路径在 return 之前判定；降级路径有明确提示
        self.assertLess(src.index("_material_likes_pass"),
                        src.index("return title, answer, footer, final_url"))

    def test_batch_collection_applies_gate(self):
        # batch 路径（collect_materials_batch）必须走同一 helper
        src = inspect.getsource(self.wf.collect_materials_batch)
        self.assertIn("_material_likes_pass", src)
        self.assertIn("点赞门槛未过", src)
        self.assertNotIn("ENABLE_MATERIAL_LIKES_GATE", src)  # 判定已收敛到 helper

    def test_degradation_log_counts_gate_rejects(self):
        # ★ 诊断性回归：重试耗尽报错时，错误信息必须带
        #   点赞门槛拒绝次数（「其中 N 次被点赞门槛拒绝」），
        #   否则用户无法区分「门槛卡死」与「答案质量问题」。
        src = inspect.getsource(self.wf.extract_content)
        self.assertIn("gate_reject_count = 0", src)
        self.assertIn("gate_reject_count += 1", src)
        self.assertIn("gate_reject_count} 次被点赞门槛拒绝", src)
        # 计数在 DOM 重试循环内累加，重试耗尽后随错误信息抛出
        self.assertLess(src.index("gate_reject_count += 1"),
                        src.index("raise RuntimeError"))


class TestZhihuWorkflowSemantics(unittest.TestCase):
    """workflow 各步骤的 DOM 语义接线。"""

    @classmethod
    def setUpClass(cls):
        from workflows.zhihu import ZhihuWorkflow
        cls.wf = ZhihuWorkflow()

    def _src(self, method_name):
        return inspect.getsource(getattr(self.wf, method_name))

    def test_init_reads_author_profile_config(self):
        from config import story
        # self.author 必须来自 config.story.AUTHOR_PROFILE（作者技能注入开关）
        self.assertEqual(self.wf.author, story.AUTHOR_PROFILE or None)

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

    def test_select_topic_branches_three_sources(self):
        # 选题来源三分支：custom → _select_custom；否则 auto/manual
        src = self._src("select_topic")
        self.assertIn('QUESTION_SOURCE == "custom"', src)
        self.assertIn("_select_custom", src)
        self.assertIn("_select_auto", src)
        self.assertIn("_select_manual", src)

    def test_select_custom_validates_url(self):
        # 自选问题：跳过选题，URL 无效则明确报错（绝不静默回退选题）
        src = self._src("_select_custom")
        self.assertIn("normalize_question_url", src)
        self.assertIn("RuntimeError", src)
        self.assertIn("open_question", src)
        self.assertNotIn("_scan_recommend", src)

    def test_source_url_follows_question_source(self):
        # 候选池 URL 跟随设置：invited → 邀请回答页，否则推荐页
        src = self._src("_source_url")
        self.assertIn("ZHIHU_INVITED_URL", src)
        self.assertIn("ZHIHU_RECOMMEND_URL", src)
        self.assertIn('QUESTION_SOURCE == "invited"', src)

    def test_scan_recommend_accepts_source_url(self):
        # 候选页扫描必须支持传入来源 URL（邀请回答页复用同一解析 JS）
        src = self._src("_scan_recommend")
        self.assertIn("url=None", src)
        self.assertIn("open_recommend_page(url)", src)

    def test_auto_manual_select_use_source_url(self):
        # 自动/手动选题都从设置里的候选池扫描（默认推荐话题）
        for method in ("_select_auto", "_select_manual"):
            src = self._src(method)
            self.assertIn("_source_url()", src)

    def test_batch_follows_question_source(self):
        # 批量采集跟随选题来源；custom（自选问题）仅单篇，回退推荐页
        src = self._src("collect_materials_batch")
        self.assertIn("QUESTION_SOURCE", src)
        self.assertIn('QUESTION_SOURCE == "custom"', src)
        self.assertIn("_source_url()", src)
        self.assertIn("ZHIHU_RECOMMEND_URL", src)

    def test_extract_uses_check_answerable_and_dom_extract(self):
        src = self._src("extract_content")
        self.assertIn("check_answerable", src)     # DOM 检测「撤销删除」
        self.assertIn("get_primary_answer", src)   # DOM 提取首答
        self.assertIn("normalize_question_url", src)

    def test_extract_no_fallback_after_retry_exhausted(self):
        src = self._src("extract_content")
        # DOM 主通道在前；重试耗尽直接报错（无任何降级）
        self.assertIn("get_primary_answer", src)
        self.assertNotIn("_extract_answer_with_fallback", src)
        self.assertIn("raise RuntimeError", src)

    def test_extract_retopics_on_short_answer(self):
        # 首答过短/不可回答时重新选题再试（MAX_TOPIC_RETRY 次），
        # 参数来自 config.story（前端可配）
        src = self._src("extract_content")
        self.assertIn("select_topic", src)
        self.assertIn("MAX_TOPIC_RETRY", src)
        self.assertIn("MIN_ANSWER_LENGTH", src)
        # 从 config.story 读取（单一事实来源），不硬编码次数
        self.assertIn("from config.story import", src)

    def test_topic_retry_default_is_five(self):
        # 用户需求：选题重试 3 → 5（总尝试 6 次）
        from config import story
        self.assertEqual(story.MAX_TOPIC_RETRY, 5)

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

    def test_run_single_saves_draft_before_format_check(self):
        # ★ 回归：故事生成后立即存盘 + on_story 回调（在格式校验前）。
        #   曾因格式不合规被跳过时故事既没落盘也不回调 → Web 控制台
        #   看不到生成结果，用户白白等 1-2 分钟。
        from workflows.base import WorkflowBase
        src = inspect.getsource(WorkflowBase.run_single)
        # 存盘必须先于格式合规检测（废稿也要留档）
        self.assertLess(
            src.index("md_path = self.save_story_file(story)"),
            src.index("validate_story_format(story)"))
        # on_story 回调在存盘后立即触发（不依赖发布成败）
        self.assertLess(
            src.index("on_story(story, md_path)"),
            src.index("validate_story_format(story)"))

    def test_run_single_publish_reuses_saved_md_path(self):
        # publish 复用已保存的 md_path，避免废稿/正稿重复落盘
        from workflows.base import WorkflowBase
        src = inspect.getsource(WorkflowBase.run_single)
        self.assertIn("self.publish(story, title, url, md_path)", src)

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

    def test_batch_web_serial_injects_author_profile(self):
        # DOM 化后批量生成统一走串行（旧并行已随 OCR 栈移除）；
        # 作者注入经由 _generate_web → _generate_web_short_form 链路
        src = self._src("_batch_generate_web_serial")
        self.assertIn("_generate_web", src)


if __name__ == "__main__":
    unittest.main()
