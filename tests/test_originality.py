# ============================================================
# tests/test_originality.py — 纯净模式「洗稿/抄袭」审核 + 纯净链路
#
# 覆盖：
#   - core.originality 本地相似度信号（原文照搬 / 无关文本）
#   - audit_originality 无 LLM 时的保守判定
#   - build_clean_prompt 只保留风格学习 + 原创禁令
#   - ZhihuWorkflow.select_topic_clean 流量优先选题（飙升 > 关注量）
#   - ZhihuWorkflow.extract_content_clean 只卡点赞门槛
#   - WorkflowBase.run_clean 契约（审核不过不发布）
#
# 运行：python -m unittest discover -s tests -v
# ============================================================

import contextlib
import inspect
import unittest
from types import SimpleNamespace


@contextlib.contextmanager
def _min_answer_len(value):
    """临时改写 MIN_ANSWER_LENGTH（纯净提取测试用）。"""
    from config import story as story_cfg
    orig = story_cfg.MIN_ANSWER_LENGTH
    story_cfg.MIN_ANSWER_LENGTH = value
    try:
        yield
    finally:
        story_cfg.MIN_ANSWER_LENGTH = orig
class TestAuditFeedbackUpgrade(unittest.TestCase):
    """洗稿重试反馈升级：给模型可执行的结构性大改指令。"""

    def test_feedback_contains_structural_directives(self):
        from core.originality import audit_feedback_text
        text = audit_feedback_text(
            {"verdict": "洗稿",
             "reasons": ["情节主线完全一致"]})
        self.assertIn("结构性大改", text)
        self.assertIn("更换故事设定背景", text)
        self.assertIn("台词必须全新创作", text)


class TestAnsweredElsewhereLedger(unittest.TestCase):
    """「已答过」拒绝写入台账 → 后续选题跳过（跨轮/跨天）。"""

    def test_record_and_load_roundtrip(self):
        import pathlib
        import tempfile
        from unittest import mock
        from core import topic_ledger

        with tempfile.TemporaryDirectory() as tmp:
            fake = pathlib.Path(tmp) / "published_topics.jsonl"
            with mock.patch.object(topic_ledger, "_ledger_path",
                                   return_value=fake):
                topic_ledger.record_answered_elsewhere(
                    "https://www.zhihu.com/question/999", "旧题")
                seen = topic_ledger.load_seen_urls()
                self.assertIn("https://www.zhihu.com/question/999", seen)
            lines = fake.read_text(encoding="utf-8").splitlines()
            self.assertIn('"source": "manual"', lines[0])


_REF_TEXT = (
    "那年我二十三岁，第一次独自坐火车去南方。车窗外的稻田连成一片，"
    "我攥着硬座票根，心里盘算着到了之后怎么开口。对面坐着一个穿灰衬衫"
    "的中年男人，一直在看一本卷了边的旧书。列车员推着小车经过，他头也"
    "不抬地让了一句，声音低得像在自言自语。我忽然想起父亲也是这样的"
    "坐姿，也是这样用书挡住半张脸。到站的时候天已经黑了，站台上的灯"
    "光把人影拉得很长。我拖着行李箱走了很远，才在一家还没打烊的面馆里"
    "坐下来，要了一碗热汤面。老板娘问我要不要加蛋，我说好，她就在「锅边」"
    "敲开一只，蛋液在汤里慢慢散开，像一朵没来得及开的花。"
)

_ORIG_TEXT = (
    "宿舍楼下的便利店总是亮到很晚。凌晨两点，我买了瓶汽水坐在门口"
    "台阶上，听自动门一次次开合。收银的小姑娘趴在柜台上打瞌睡，头顶的"
    "监控器红灯一明一灭。隔壁桌的「外卖员」打了一整晚的游戏，桌上堆着"
    "三个空烟盒。我把汽水喝完，又把瓶盖拧回去，放在台阶边沿。第二天"
    "早上路过，那瓶汽水还在，瓶盖不见了。我忽然觉得这座城市里，总有人"
    "比你更晚睡，也总有人比你更早醒，而他们之间可能一辈子都不会说一句话。"
)


class TestLocalSignals(unittest.TestCase):
    """本地相似度信号（不依赖 LLM）。"""

    def test_identical_text_max_signals(self):
        from core.originality import local_signals
        sig = local_signals(_REF_TEXT, _REF_TEXT)
        self.assertGreaterEqual(sig["lcs_ratio"], 0.99)
        self.assertGreaterEqual(sig["bigram_dice"], 0.99)

    def test_unrelated_text_low_signals(self):
        from core.originality import local_signals
        sig = local_signals(_REF_TEXT, _ORIG_TEXT)
        self.assertLess(sig["lcs_ratio"], 0.55)
        self.assertLess(sig["bigram_dice"], 0.65)

    def test_sentence_reuse_detected(self):
        from core.originality import local_signals, SENT_DUP_RATIO_FAIL
        half = _REF_TEXT[: len(_REF_TEXT) // 2]
        mixed = half + "。\n\n" + _ORIG_TEXT
        sig = local_signals(mixed, _REF_TEXT)
        self.assertGreaterEqual(sig["sent_dup_ratio"], SENT_DUP_RATIO_FAIL)


class TestOriginalityAudit(unittest.TestCase):
    """审核入口（无 LLM 环境下的保守判定）。"""

    def test_identical_fails_without_llm(self):
        from core.originality import audit_originality
        result = audit_originality("测试问题", _REF_TEXT, _REF_TEXT,
                                   enable_llm=False)
        self.assertFalse(result["passed"])
        self.assertIn("疑似", result["verdict"])

    def test_unrelated_passes_without_llm(self):
        from core.originality import audit_originality
        result = audit_originality("测试问题", _ORIG_TEXT, _REF_TEXT,
                                   enable_llm=False)
        self.assertTrue(result["passed"])
        self.assertEqual(result["verdict"], "原创")

    def test_feedback_text_refers_to_reasons(self):
        from core.originality import audit_feedback_text
        text = audit_feedback_text(
            {"verdict": "洗稿", "reasons": ["结构顺序完全一致"]})
        self.assertIn("洗稿", text)
        self.assertIn("结构顺序完全一致", text)


class TestCleanPrompt(unittest.TestCase):
    """纯净模式 prompt：只有风格学习 + 原创禁令。"""

    def test_build_clean_prompt_contains_core_rules(self):
        from story_prompt import build_clean_prompt
        msg, mode_str = build_clean_prompt("有没有让你瞬间破防的瞬间？",
                                           _REF_TEXT)
        self.assertEqual(mode_str, "纯净模式")
        self.assertIn("知乎问题", msg)
        self.assertIn(_REF_TEXT[:12], msg)
        self.assertIn("严禁抄袭", msg)
        self.assertIn("严禁洗稿", msg)
        self.assertIn("学习参考高赞回答的风格", msg)
        # 刻意不注入格式硬约束
        self.assertNotIn("## **N**", msg)

    def test_feedback_appended(self):
        from story_prompt import build_clean_prompt
        msg, _ = build_clean_prompt("题目", None,
                                    feedback="审核判定：洗稿。请重写。")
        self.assertIn("原创审核未通过", msg)
        self.assertIn("审核判定：洗稿", msg)

    def test_paragraph_stats_learned_from_reference(self):
        # 参考是短句成段 → prompt 注入对应段落特征，引导学习段落长度
        from story_prompt import build_clean_prompt
        ref = "第一段。\n\n第二段。\n\n第三段。\n\n第四段。\n\n第五段。"
        msg, _ = build_clean_prompt("题目", ref)
        self.assertIn("参考回答的段落特征", msg)
        self.assertIn("短段", msg)          # 全部 <50 字 → 以短段为主
        self.assertIn("平均每段 4 字", msg)
        self.assertIn("段落不要", msg)

    def test_paragraph_stats_skip_when_no_reference(self):
        from story_prompt import build_clean_prompt
        msg, _ = build_clean_prompt("题目", None)
        # 系统守则里提到该概念是常驻的；校验的是没有注入具体统计段
        self.assertNotIn("## 参考回答的段落特征（请同样学习）", msg)
        self.assertNotIn("平均每段", msg)

    def test_paragraph_stats_ignores_zhihu_headings(self):
        from story_prompt import _reference_paragraph_stats
        ref = "## **1**\n\n正文一段话。\n\n---\n\n正文二段话。"
        stats = _reference_paragraph_stats(ref)
        self.assertIsNotNone(stats)
        self.assertIn("2 段", stats)        # 章节标题与分隔线不计入
        self.assertIn("6 字", stats)        # 两段正文各 6 字


class TestCleanConfig(unittest.TestCase):
    def test_clean_constants_exported(self):
        from config.story import (CLEAN_MAX_GEN_ATTEMPTS,
                                  CLEAN_AUDIT_ENABLE, CLEAN_MIN_LIKES_FLOOR,
                                  CLEAN_LIKES_RELAX_FACTORS, __all__)
        self.assertGreaterEqual(CLEAN_MAX_GEN_ATTEMPTS, 1)
        self.assertTrue(CLEAN_AUDIT_ENABLE)
        self.assertGreaterEqual(CLEAN_MIN_LIKES_FLOOR, 1)
        self.assertEqual(len(CLEAN_LIKES_RELAX_FACTORS), 3)
        self.assertIn("CLEAN_MIN_LIKES_FLOOR", __all__)
        # 纯净模式不再有独立门槛常量——须跟随 UI 的 MATERIAL_MIN_LIKES
        self.assertNotIn("CLEAN_MATERIAL_MIN_LIKES", __all__)


class _FakeBrowser:
    """select/extract 纯净模式测试用的最小浏览器替身。"""

    def __init__(self):
        self.opened = []
        self.page = SimpleNamespace(
            url="https://www.zhihu.com/question/12345")

    def is_logged_in(self):
        return True

    def open_question(self, url, force=False):
        self.opened.append(url)

    def scroll_feed(self):
        pass

    def get_recommend_questions(self, max_cards=60):
        return []


class TestCleanSelect(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from workflows.zhihu import ZhihuWorkflow
        cls.wf = ZhihuWorkflow()

    def _make_wf(self, questions):
        wf = self._make_instance()
        wf._browser = lambda: self._browser
        hot = [q for q in questions if q.get("is_hot")]
        normal = [q for q in questions if not q.get("is_hot")]
        wf._scan_recommend = lambda browser, url=None: (questions, hot, normal)
        return wf

    def _make_instance(self):
        # 每次新建实例，避免类级状态污染
        from workflows.zhihu import ZhihuWorkflow
        return ZhihuWorkflow()

    def test_hot_preferred(self):
        wf = self._make_instance()
        self._browser = _FakeBrowser()
        wf._browser = lambda: self._browser
        qs = [
            {"title": "普通题", "href": "/a", "followers": 1000},
            {"title": "飙升题", "href": "/b", "followers": 5,
             "is_hot": True},
            {"title": "高关注普通", "href": "/c", "followers": 8000},
        ]
        wf._scan_recommend = lambda browser, url=None: (
            qs, [qs[1]], [qs[0], qs[2]])
        got = wf.select_topic_clean(avoid=set())
        self.assertEqual(got, "/b")
        self.assertEqual(self._browser.opened, ["/b"])

    def test_no_hot_picks_highest_followers(self):
        wf = self._make_instance()
        self._browser = _FakeBrowser()
        wf._browser = lambda: self._browser
        qs = [
            {"title": "关注少", "href": "/a", "followers": 10},
            {"title": "关注多", "href": "/b", "followers": 9000},
            {"title": "中间", "href": "/c", "followers": 500},
        ]
        wf._scan_recommend = lambda browser, url=None: (qs, [], qs)
        got = wf.select_topic_clean(avoid=set())
        self.assertEqual(got, "/b")

    def test_avoid_excluded(self):
        wf = self._make_instance()
        self._browser = _FakeBrowser()
        wf._browser = lambda: self._browser
        qs = [
            {"title": "已试", "href": "/a", "followers": 9000},
            {"title": "备选", "href": "/b", "followers": 100},
            {"title": "飙升", "href": "/c", "followers": 1,
             "is_hot": True},
        ]
        wf._scan_recommend = lambda browser, url=None: (
            qs, [qs[2]], [qs[0], qs[1]])
        got = wf.select_topic_clean(avoid={"/c"})
        self.assertEqual(got, "/a")  # 飙升被排除后按关注量选最高者

    def test_clean_select_no_story_keyword_filter(self):
        # 纯净选题不得调用故事体裁规则筛选
        src = inspect.getsource(self.wf.select_topic_clean)
        self.assertNotIn("_apply_story_filter", src)
        self.assertNotIn("_pick_best", src)


class TestCleanExtract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from workflows.zhihu import ZhihuWorkflow
        cls.wf = ZhihuWorkflow()

    def _fake_browser(self, likes):
        b = _FakeBrowser()
        b.check_answerable = lambda: (True, "")
        b.get_primary_answer = lambda min_length=1: {
            "title": "测试问题",
            "answer": _REF_TEXT,
            "footer": {"likes": likes},
        }
        return b

    def test_likes_pass_returns_extracted(self):
        wf = type(self.wf)()
        b = self._fake_browser(300)
        wf._browser = lambda: b
        wf.select_topic_clean = lambda avoid=None: (
            "https://www.zhihu.com/question/1")
        with _min_answer_len(50):
            title, answer, footer, url = wf.extract_content_clean()
        self.assertEqual(title, "测试问题")
        self.assertEqual(footer["likes"], 300)
        self.assertIn("question/1", url)

    def test_short_answer_rejected_by_length_gate(self):
        # 最短回答底线：首答过短 → 整轮被拒后报长度原因
        wf = type(self.wf)()
        b = self._fake_browser(999)
        b.get_primary_answer = lambda min_length=1: {
            "title": "太离谱的题",
            "answer": "太短了。",
            "footer": {"likes": 999},
        }
        wf._browser = lambda: b
        wf.select_topic_clean = lambda avoid=None: (
            "https://www.zhihu.com/question/1")
        with self.assertRaises(RuntimeError) as ctx:
            wf.extract_content_clean()
        self.assertIn("长度", str(ctx.exception))
        self.assertIn("500", str(ctx.exception))

    def test_likes_below_floor_falls_back_to_best(self):
        # 整轮都低于门槛 → 不报错，回退取所见最高赞（点赞是优选信号而非死门）
        wf = type(self.wf)()
        b = self._fake_browser(3)   # 远低于 200，也低于放宽后的地板 20
        wf._browser = lambda: b
        wf.select_topic_clean = lambda avoid=None: (
            "https://www.zhihu.com/question/1")
        with _min_answer_len(50):
            title, answer, footer, url = wf.extract_content_clean()
        self.assertEqual(title, "测试问题")
        self.assertEqual(footer["likes"], 3)
        self.assertIn("question/1", url)

    def test_adaptive_min_likes_progression(self):
        # 基准跟随 MATERIAL_MIN_LIKES（UI 设置可改）；这里临时置 200 验证放宽序列
        from config import story as story_cfg
        wf = type(self.wf)()
        orig = story_cfg.MATERIAL_MIN_LIKES
        try:
            story_cfg.MATERIAL_MIN_LIKES = 200
            self.assertEqual(wf._clean_adaptive_min_likes(0), 200)
            self.assertEqual(wf._clean_adaptive_min_likes(1), 120)
            self.assertEqual(wf._clean_adaptive_min_likes(2), 60)
            self.assertEqual(wf._clean_adaptive_min_likes(9), 60)  # 收敛到 0.3 档
        finally:
            story_cfg.MATERIAL_MIN_LIKES = orig

    def test_adaptive_min_likes_follows_user_setting(self):
        # 用户把「最低点赞」设为 20 → 纯净模式首轮就是 20，不再出现 200
        from config import story as story_cfg
        wf = type(self.wf)()
        orig = story_cfg.MATERIAL_MIN_LIKES
        try:
            story_cfg.MATERIAL_MIN_LIKES = 20
            self.assertEqual(wf._clean_adaptive_min_likes(0), 20)
            self.assertEqual(wf._clean_adaptive_min_likes(1), 20)
            self.assertEqual(wf._clean_adaptive_min_likes(2), 20)
        finally:
            story_cfg.MATERIAL_MIN_LIKES = orig

    def test_fallback_prefers_highest_likes_seen(self):
        # 兜底应取整轮最高赞而非最后一次的
        wf = type(self.wf)()
        seq = iter([3, 37, 10])
        b = _FakeBrowser()
        b.check_answerable = lambda: (True, "")
        b.get_primary_answer = lambda min_length=1: {
            "title": "题",
            "answer": _REF_TEXT,
            "footer": {"likes": next(seq)},
        }
        wf._browser = lambda: b
        wf.select_topic_clean = lambda avoid=None: (
            "https://www.zhihu.com/question/%d" % (len(avoid) + 1))
        with _min_answer_len(50):
            title, answer, footer, url = wf.extract_content_clean()
        self.assertEqual(footer["likes"], 37)


class TestCleanRunContract(unittest.TestCase):
    def test_run_clean_exists_and_guards_publish(self):
        from workflows.base import WorkflowBase
        src = inspect.getsource(WorkflowBase.run_clean)
        self.assertIn("select_topic_clean", src)
        self.assertIn("extract_content_clean", src)
        self.assertIn("generate_clean_with_retry", src)
        self.assertIn("audit", src)
        # 审核不通过 → 不发布
        publish_call = src.index("self.publish(")
        audit_check = src.index('not audit.get("passed")')
        self.assertLess(audit_check, publish_call)

    def test_clean_generation_in_mixin(self):
        from workflows.workflow_generation import GenerationMixin
        src = inspect.getsource(GenerationMixin.generate_clean_with_retry)
        self.assertIn("audit_originality", src)
        self.assertIn("CLEAN_MAX_GEN_ATTEMPTS", src)



class TestStaleProcessCleanup(unittest.TestCase):
    """残留 Edge 进程清理（profile 锁误报「未登录」的防线）。"""

    def test_noop_on_empty_dir(self):
        from applications.zhihu_story.browser_session import (
            _kill_stale_profile_processes)
        _kill_stale_profile_processes("")   # 不应抛异常

    def test_survives_subprocess_failure(self):
        from unittest import mock
        from applications.zhihu_story import browser_session
        with mock.patch("subprocess.run",
                        side_effect=RuntimeError("boom")):
            browser_session._kill_stale_profile_processes(
                r"C:\x\browser_profile")  # 清理失败不阻断启动

    def test_no_pids_is_noop(self):
        from unittest import mock
        from types import SimpleNamespace
        from applications.zhihu_story import browser_session
        with mock.patch("subprocess.run",
                        return_value=SimpleNamespace(stdout="", stderr="")):
            browser_session._kill_stale_profile_processes(
                r"C:\x\browser_profile")  # 无残留进程 → 直接返回


class TestSilentProcessRun(unittest.TestCase):
    """后台命令无声运行（消除 PowerShell/taskkill 黑框一闪）。"""

    def test_returns_stdout_and_rc(self):
        import sys
        from desktop_utils import run_process_silent
        r = run_process_silent([sys.executable, "-c", "print(41)"],
                               text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("41", r.stdout or "")

class TestParagraphSimilarity(unittest.TestCase):
    """段落长度分布对比（纯数学，不依赖 LLM）。"""

    _REF_SHORT = "短段一。\n\n短段二。\n\n短段三。\n\n短段四。\n\n短段五。"
    _LONG_PARA = "这是一段特别长的铺陈文字，用来模拟生成故事里的大长段。" * 6

    def test_ref_short_vs_new_long_fails(self):
        from core.originality import paragraph_similarity
        new_long = self._LONG_PARA + "\n\n" + self._LONG_PARA + "\n\n" + self._LONG_PARA
        r = paragraph_similarity(self._REF_SHORT, new_long)
        self.assertFalse(r["ok"])
        self.assertGreater(r["bucket_diff"], 0.55)
        self.assertLess(r["avg_ratio"], 0.3)

    def test_similar_short_style_passes(self):
        from core.originality import paragraph_similarity
        new_short = "另一段。\n\n又一个。\n\n还一段。\n\n再加一段。\n\n最后一段。"
        r = paragraph_similarity(self._REF_SHORT, new_short)
        self.assertTrue(r["ok"])
        self.assertLessEqual(r["bucket_diff"], 0.2)

    def test_disabled_by_config_passes(self):
        from config import story as story_cfg
        from core.originality import paragraph_similarity
        orig = story_cfg.CLEAN_PARAGRAPH_AUDIT_ENABLE
        try:
            story_cfg.CLEAN_PARAGRAPH_AUDIT_ENABLE = False
            new_long = self._LONG_PARA + "\n\n" + self._LONG_PARA
            r = paragraph_similarity(self._REF_SHORT, new_long)
            self.assertTrue(r["ok"])
        finally:
            story_cfg.CLEAN_PARAGRAPH_AUDIT_ENABLE = orig

    def test_audit_paragraph_only_fail(self):
        from core.originality import audit_originality
        new_long = self._LONG_PARA + "\n\n" + self._LONG_PARA + "\n\n" + self._LONG_PARA
        r = audit_originality("题目", new_long, self._REF_SHORT, enable_llm=False)
        self.assertFalse(r["passed"])
        self.assertEqual(r["verdict"], "段落长度不符")
        self.assertTrue(any("段落" in x for x in r["reasons"]))




class TestCleanMultiRound(unittest.TestCase):
    """纯净模式多轮（默认 1，>1 时循环完整链路多次）。"""

    def test_clean_rounds_default_one_in_ui(self):
        # UI 层纯净模式默认 1 轮；后端逻辑按 rounds 循环
        src = open("webui/static/index.html", encoding="utf-8").read()
        self.assertIn('id="cleanRounds" min="1" max="20" value="1"', src)
        js = open("webui/static/app.js", encoding="utf-8").read()
        self.assertIn(
            'body.rounds = parseInt($("cleanRounds").value, 10) || 1;', js)

    def test_classic_mode_renamed_and_rounds_in_ui(self):
        # 经典模式（原单轮）改名 + 轮数输入 + 参数透传
        src = open("webui/static/index.html", encoding="utf-8").read()
        self.assertIn("经典模式 · 完整链路", src)
        self.assertIn('id="classicRounds" min="1" max="20" value="1"', src)
        self.assertNotIn("单轮（完整链路）", src)
        js = open("webui/static/app.js", encoding="utf-8").read()
        self.assertIn(
            'body.rounds = parseInt($("classicRounds").value, 10) || 1;', js)

    def test_dispatch_loops_over_rounds(self):
        # 后端 clean/single 分支都按 spec.rounds 循环执行完整链路
        import inspect
        from webui.run_manager import TaskRunner
        src = inspect.getsource(TaskRunner._dispatch)
        clean_src = src[src.index('if mode == "clean"'):
                         src.index('if mode == "single"')]
        self.assertIn('rounds = max(1, int(spec.rounds or 1))', clean_src)
        self.assertIn("wf.run_clean(", clean_src)
        self.assertIn("成功 {success}/{rounds}", clean_src)
        single_src = src[src.index('if mode == "single"'):
                          src.index('if mode == "batch"')]
        self.assertIn('rounds = max(1, int(spec.rounds or 1))', single_src)
        self.assertIn("wf.run_single(", single_src)
        self.assertIn("成功 {success}/{rounds}", single_src)

    def test_extract_clean_uses_min_answer_length(self):
        # 纯净提取必须遵守最短回答底线（跟随设置 MIN_ANSWER_LENGTH）
        import inspect
        from workflows.zhihu import ZhihuWorkflow
        src = inspect.getsource(ZhihuWorkflow.extract_content_clean)
        self.assertIn("MIN_ANSWER_LENGTH", src)
        self.assertIn("len(answer) < min_len", src)

if __name__ == "__main__":
    unittest.main()
