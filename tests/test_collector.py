# ============================================================
# tests/test_collector.py — 作者故事采集编排（collector.py）
#
# FakeBrowser duck typing（不碰真实浏览器）：验证滚动加载、
# answer_url 去重（断点续采只补新的）、数量上限、过短跳过、
# 记录格式与 JSONL 追加写入。
# ============================================================

import json
import os
import tempfile
import unittest
from unittest import mock

from applications.zhihu_story import collector


class FakeBrowser:
    """collector 依赖的 browser_adapter 公开原语的最小替身。

    links: 当前列表页链接（测试可直接改，模拟滚动后列表增长）；
    answers: href → {title, answer, footer}（缺失 = 提取失败/过短）；
    name: eval_js(_AUTHOR_NAME_JS) 返回的昵称（空 = 未识别）。
    """

    def __init__(self, links=None, answers=None, name=""):
        self.links = list(links or [])
        self.answers = dict(answers or {})
        self.name = name
        self.link_calls = 0
        self.answer_calls = []
        self.scrolls = 0

    def get_author_answer_links(self, url):
        self.link_calls += 1
        return [dict(l) for l in self.links]

    def eval_js(self, js, *args):
        if "scrollTo" in js:
            self.scrolls += 1
            return True
        if "ProfileHeader" in js:
            return self.name
        if "ContentItem-title" in js:   # _AUTHOR_LINKS_JS
            return [dict(l) for l in self.links]
        return None

    def get_author_answer(self, href, author, min_length=100):
        self.answer_calls.append(href)
        data = self.answers.get(href)
        if not data:
            return None
        return {"title": data["title"], "answer": data["answer"],
                "footer": dict(data.get("footer") or {})}


def _link(i, href=None):
    return {"title": f"故事{i}", "href": href or f"https://www.zhihu.com/question/1/answer/{i}",
            "likes": 100 + i, "comments": 5}


def _answer(i):
    return {"title": f"故事{i}",
            "answer": f"正文内容{i}，" + "长" * 150,
            "footer": {"likes": 100 + i, "comments": 5,
                       "answer_url": f"https://www.zhihu.com/question/1/answer/{i}",
                       "publish_time": "2026-08-01"}}


class CollectBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="collect_")
        self.out = os.path.join(self._tmp, "collected.jsonl")
        self._orig_lib = collector.STORY_LIB
        self._real_size = os.path.getsize(self._orig_lib) \
            if os.path.exists(self._orig_lib) else -1
        collector.STORY_LIB = self.out
        # 滚动轮的 sleep(1.5) 加速，不让测试空等
        self._sleep = mock.patch("applications.zhihu_story.collector.time.sleep")
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()
        collector.STORY_LIB = self._orig_lib
        # 防泄漏：真实采集库在本测试期间不得被写入（曾因临时路径
        # 失效把 FakeBrowser 的假故事写进真实库，污染文风提炼）
        self.assertEqual(os.path.getsize(self._orig_lib)
                         if os.path.exists(self._orig_lib) else -1,
                         self._real_size,
                         "测试不得写入真实采集库（data/collected_stories.jsonl）")

    def _records(self):
        out = []
        with open(self.out, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out


class TestCollectFlow(CollectBase):
    def test_collect_basic_writes_records(self):
        browser = FakeBrowser(
            links=[_link(1), _link(2), _link(3)],
            answers={_link(1)["href"]: _answer(1),
                     _link(2)["href"]: _answer(2),
                     _link(3)["href"]: _answer(3)},
            name="测试作者")
        result = collector.collect_author_stories(
            "https://www.zhihu.com/people/token/answers",
            count=10, browser=browser)
        self.assertEqual(len(result["collected"]), 3)
        self.assertEqual(result["author"], "测试作者")
        self.assertEqual(result["existing"], 0, "新作者：库中无样本")
        recs = self._records()
        self.assertEqual(len(recs), 3)
        for r in recs:
            self.assertEqual(r["source"], "author_page_dom")
            self.assertEqual(r["author"], "测试作者")
            self.assertTrue(r["title"])
            self.assertTrue(r["answer"])
            self.assertIn("answer_url", r["footer"])
            self.assertTrue(r["collected_at"])

    def test_collect_dedup_existing_only_appends_new(self):
        # 已有库里 2 篇（断点续采）：只采新链接，不重复写
        with open(self.out, "a", encoding="utf-8") as f:
            f.write(json.dumps({"source": "uia", "author": "测试作者",
                                "title": "旧1", "answer": "x" * 200,
                                "footer": {"answer_url": _link(1)["href"]},
                                "collected_at": "2026-08-01 00:00:00"}) + "\n")
            f.write(json.dumps({"source": "author_page", "author": "测试作者",
                                "title": "旧2", "answer": "y" * 200,
                                "footer": {"answer_url": _link(2)["href"]},
                                "collected_at": "2026-08-01 00:00:00"}) + "\n")
        browser = FakeBrowser(
            links=[_link(1), _link(2), _link(3)],
            answers={_link(1)["href"]: _answer(1),
                     _link(2)["href"]: _answer(2),
                     _link(3)["href"]: _answer(3)},
            name="测试作者")
        result = collector.collect_author_stories(
            "https://www.zhihu.com/people/token/answers",
            count=10, browser=browser)
        self.assertEqual(len(result["collected"]), 1)
        self.assertEqual(result["collected"][0]["title"], "故事3")
        self.assertEqual(result["existing"], 2, "已有 2 篇 → 追加新样本")
        # 只打开了新链接（1、2 已在库中，不开）
        self.assertEqual(browser.answer_calls, [_link(3)["href"]])
        recs = self._records()
        self.assertEqual(len(recs), 3, "旧记录保留，只追加新记录")
        self.assertEqual(recs[-1]["title"], "故事3")

    def test_collect_count_limit_stops_early(self):
        browser = FakeBrowser(
            links=[_link(1), _link(2), _link(3), _link(4), _link(5)],
            answers={_link(i)["href"]: _answer(i) for i in range(1, 6)},
            name="限数作者")
        result = collector.collect_author_stories(
            "https://www.zhihu.com/people/token/answers",
            count=3, browser=browser)
        self.assertEqual(len(result["collected"]), 3)
        self.assertEqual(len(self._records()), 3)
        self.assertEqual(len(browser.answer_calls), 3, "第 4、5 篇不打开")

    def test_collect_short_answer_skipped(self):
        # answers 里缺 2 → get_author_answer 返回 None → 跳过继续
        browser = FakeBrowser(
            links=[_link(1), _link(2), _link(3)],
            answers={_link(1)["href"]: _answer(1),
                     _link(3)["href"]: _answer(3)},
            name="跳过作者")
        result = collector.collect_author_stories(
            "https://www.zhihu.com/people/token/answers",
            count=10, browser=browser)
        self.assertEqual(len(result["collected"]), 2)
        self.assertEqual([r["title"] for r in result["collected"]],
                         ["故事1", "故事3"])

    def test_collect_scroll_loads_more(self):
        # 首屏 2 篇采完 → 列表增长（模拟滚动加载）→ 补采到 count
        browser = FakeBrowser(
            links=[_link(1), _link(2)],
            answers={_link(1)["href"]: _answer(1),
                     _link(2)["href"]: _answer(2),
                     _link(3)["href"]: _answer(3)},
            name="滚动作者")

        def grow(*a, **k):
            # 滚动后列表变长（get_author_answer_links 与 eval_js 共用）
            if browser.scrolls and len(browser.links) == 2:
                browser.links = [_link(1), _link(2), _link(3)]
            return [dict(l) for l in browser.links]

        browser.get_author_answer_links = grow

        def fake_eval(js, *a):
            if "scrollTo" in js:
                browser.scrolls += 1
                return True
            if "ProfileHeader" in js:
                return "滚动作者"
            return grow()
        browser.eval_js = fake_eval
        result = collector.collect_author_stories(
            "https://www.zhihu.com/people/token/answers",
            count=3, browser=browser)
        self.assertEqual(len(result["collected"]), 3)
        self.assertGreaterEqual(browser.scrolls, 1, "必须触发滚动加载")
        self.assertEqual(len(self._records()), 3)

    def test_scroll_load_more_polls_for_new_links(self):
        # ★ 回归：滚动后固定 1.5s 等待在无头/慢渲染下常读不到新链接
        #   ——V4.2.2 用户反馈无头采集 0 篇（切前台同作者立即可采）。
        #   改为轮询直到新链接出现（10s deadline，0.8s 间隔）
        import inspect
        src = inspect.getsource(collector._scroll_load_more)
        self.assertIn("deadline = time.time() + 10", src)
        self.assertIn("while time.time() < deadline", src)
        self.assertIn("time.sleep(0.8)", src)
        self.assertIn("_AUTHOR_LINKS_JS", src)
        self.assertNotIn("sleep(1.5)", src)   # 固定等待窗口已移除

    def test_collect_exhausted_stops(self):
        # 列表读尽且滚动无新增 → 停止，不挂死
        browser = FakeBrowser(
            links=[_link(1), _link(2)],
            answers={_link(1)["href"]: _answer(1),
                     _link(2)["href"]: _answer(2)},
            name="读尽作者")
        result = collector.collect_author_stories(
            "https://www.zhihu.com/people/token/answers",
            count=10, browser=browser)
        self.assertEqual(len(result["collected"]), 2)
        self.assertGreaterEqual(browser.scrolls, 1)
        self.assertLessEqual(browser.scrolls, 4, "滚动 4 轮无新链接即停")

    def test_author_name_zero_width_cleaned(self):
        # 页面昵称常带零宽空格（U+200B 等），会污染作者名/建档文件名
        browser = FakeBrowser(
            links=[_link(1)], answers={_link(1)["href"]: _answer(1)},
            name="闲得无聊的仙女​")
        result = collector.collect_author_stories(
            "https://www.zhihu.com/people/token/answers",
            count=5, browser=browser)
        self.assertEqual(result["author"], "闲得无聊的仙女")
        self.assertEqual(self._records()[0]["author"], "闲得无聊的仙女")

    def test_author_name_detected_from_page(self):
        browser = FakeBrowser(
            links=[_link(1)], answers={_link(1)["href"]: _answer(1)},
            name="页面昵称作者")
        result = collector.collect_author_stories(
            "https://www.zhihu.com/people/token/answers",
            count=5, browser=browser)
        self.assertEqual(result["author"], "页面昵称作者")
        self.assertEqual(self._records()[0]["author"], "页面昵称作者")

    def test_author_name_url_fallback(self):
        # 页面识别失败 → 用 URL token 兜底
        browser = FakeBrowser(
            links=[_link(1)], answers={_link(1)["href"]: _answer(1)},
            name="")

        def boom(js, *a):
            if "ProfileHeader" in js:
                raise RuntimeError("evaluate 失败")
            return None
        browser.eval_js = boom
        result = collector.collect_author_stories(
            "https://www.zhihu.com/people/zhang-san/answers",
            count=5, browser=browser)
        self.assertEqual(result["author"], "zhang-san")
        self.assertEqual(self._records()[0]["author"], "zhang-san")

    def test_author_name_explicit_wins(self):
        # 已无手动指定参数：即使库中已有同名异源记录，也按页面
        # 识别的作者名采集（全自动，不存在用户起名冲突）
        browser = FakeBrowser(
            links=[_link(1)], answers={_link(1)["href"]: _answer(1)},
            name="页面昵称")
        result = collector.collect_author_stories(
            "https://www.zhihu.com/people/token/answers",
            count=5, browser=browser)
        self.assertEqual(result["author"], "页面昵称")
        self.assertEqual(self._records()[0]["author"], "页面昵称")

    def test_collect_cancel_aborts(self):
        # 取消钩子生效：滚动轮抛 WorkflowCancelled
        from applications.zhihu_story import browser_adapter as ba
        with mock.patch.object(ba, "_check_cancel",
                               side_effect=ba.WorkflowCancelled("cancel")):
            browser = FakeBrowser(
                links=[_link(1)], answers={_link(1)["href"]: _answer(1)})
            # 首轮 fresh 为空才会进滚动轮（取消检查点）
            browser.links = []
            with self.assertRaises(ba.WorkflowCancelled):
                collector.collect_author_stories(
                    "https://www.zhihu.com/people/token/answers",
                    count=5, browser=browser)


class TestLoadDoneUrls(unittest.TestCase):
    def test_parses_footer_and_top_level(self):
        tmp = tempfile.mkdtemp(prefix="done_")
        path = os.path.join(tmp, "lib.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"footer": {"answer_url": "https://x/q/1/a/1?utm=1"},'
                    ' "title": "t"}\n')
            f.write('{"answer_url": "https://x/q/2/a/2#anchor"}\n')
            f.write("不是 JSON\n")
            f.write('{"footer": {}}\n')
        done = collector.load_done_urls(path)
        # 规范化：去 query/hash
        self.assertIn("https://x/q/1/a/1", done)
        self.assertIn("https://x/q/2/a/2", done)
        self.assertEqual(len(done), 2)

    def test_missing_file_empty(self):
        self.assertEqual(
            collector.load_done_urls(os.path.join(tempfile.mkdtemp(), "x")),
            set())


if __name__ == "__main__":
    unittest.main()
