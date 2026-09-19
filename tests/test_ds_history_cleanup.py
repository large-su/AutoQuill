# ============================================================
# tests/test_ds_history_cleanup.py — DeepSeek 历史会话清理工具回归
#
# 覆盖：判定纯函数（时间/标题/指纹/三态）、站点响应解析、分页枚举。
# 不碰真实浏览器与网络（分页用桩客户端）。
#
# 运行：python -m unittest tests.test_ds_history_cleanup -v
# ============================================================

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import ds_history_cleanup as C  # noqa: E402


# 真实会话里出现过的 AutoQuill 提示词片段（一旧一新两版）
PROMPT_NEW = (
    "你是一位知乎故事区的爆款创作者，精通短篇故事的架构与写作。\n"
    "## 核心铁律\n1. 参考文章仅供【风格】参考\n"
    "## 发布前自检（收尾前逐条核对，全部满足再输出）\n"
    "2. 章节：全篇用 \"## **N**\" 分节（N 为 1、2、3...），至少 6 节\n"
)
PROMPT_OLD = (
    "你是一位知乎故事区的爆款创作者，精通短篇故事的架构与写作。\n"
    "# 绝对禁止\n## 输出前格式自检（在推理思考中完成，不写入正文）\n"
    "- 对话是否统一用了「」、系统内容是否用了【】？\n"
)
PROMPT_V1 = (
    "**Role**: 请你扮演一位顶尖的故事创作大师！\n"
    "**Background**: 用户是一位内容创作者……\n"
    "2.爆款故事架构能力：精通故事创作原理\n"
    "3.去AI化写作：具备纯熟的写作技巧\n"
)
PROMPT_V23 = (
    "你是一位顶尖的知乎故事创作大师，精通爆款故事的架构与写作。\n"
    "## 开头铁律（最重要！决定读者是否点进来）\n"
    "## 引言的输出格式（严格按示范执行）\n"
    "## 格式规范（硬性要求，输出前逐条核对）\n"
)
PROMPT_SCORE = (
    "你是一位知乎故事区的资深读者，每天阅读大量故事回答……\n"
    "请严格按以下 JSON 格式返回……comment 要像一个毒舌但中肯的读者的一句话评价\n"
)
PROMPT_SCREEN = (
    "你是一位知乎故事区资深编辑，一眼就能判断：这个问题适不适合写成"
    "一个有高赞潜力的知乎故事/短篇小说。\n只返回严格 JSON（不要其他文字）\n"
)
CHAT_UNRELATED = "帮我看看这台电脑的配置行不行，预算 5000 左右，谢谢！"


def _page(items, has_more=False):
    return {"code": 0, "msg": "", "data": {
        "biz_code": 0, "biz_msg": "",
        "biz_data": {"chat_sessions": items, "has_more": has_more}}}


class TestFingerprints(unittest.TestCase):

    def test_real_prompt_is_confirmed(self):
        for text in (PROMPT_NEW, PROMPT_OLD, PROMPT_SCORE, PROMPT_SCREEN,
                     PROMPT_V1, PROMPT_V23):
            hits = C.match_fingerprints(text)
            self.assertTrue(hits, "未命中任何指纹：%s" % text[:40])
            self.assertGreaterEqual(
                C.fingerprint_weight(hits), C.CONFIRM_WEIGHT,
                "权重不足以确认：%s" % hits)

    def test_unrelated_chat_not_matched(self):
        hits = C.match_fingerprints(CHAT_UNRELATED)
        self.assertEqual(C.fingerprint_weight(hits), 0)

    def test_single_weak_hit_is_not_confirmed(self):
        hits = C.match_fingerprints("这篇故事潜力如何")
        self.assertEqual(hits, ["故事潜力"])
        self.assertLess(C.fingerprint_weight(hits), C.CONFIRM_WEIGHT)
        self.assertEqual(
            C.classify(["故事"], C.fingerprint_weight(hits), True),
            C.VERDICT_LIKELY)

    def test_fingerprint_literals_exist_in_repo(self):
        """指纹字面量必须来自仓库真实提示词（防手抖写错字）。

        HISTORICAL_FINGERPRINTS 是历史版本 prompt 的原文（当前工作区已
        改写，取自 git 历史 V1.0.0/V2.3.0），不参与本断言。
        """
        sources = [
            os.path.join(ROOT, "story_prompt.py"),
            os.path.join(ROOT, "applications", "zhihu_story", "prompts.py"),
            os.path.join(ROOT, "core", "detectors.py"),
        ]
        blob = ""
        for path in sources:
            with open(path, encoding="utf-8") as fh:
                blob += fh.read()
        for literal, _weight in C.FINGERPRINTS:
            if literal in C.HISTORICAL_FINGERPRINTS:
                continue
            self.assertIn(literal, blob, "指纹不在提示词源文件里：%s" % literal)


class TestTitleAndAge(unittest.TestCase):

    def test_title_keyword_hits(self):
        self.assertIn("故事", C.title_keyword_hits("故事潜力判断"))
        self.assertIn("穿书", C.title_keyword_hits("官配被穿书女拦截"))
        self.assertEqual(C.title_keyword_hits("45号钢圆盘重量体积"), [])

    def test_age_days(self):
        self.assertAlmostEqual(C.age_days(1000.0, now=1000.0 + 86400 * 5), 5.0)
        self.assertIsNone(C.age_days(None))
        self.assertIsNone(C.age_days("abc"))


class TestNormalizeMessages(unittest.TestCase):
    """★ 2026-09-17 真机踩坑：旧会话正文在 fragments[] 里，不在 content。"""

    OLD_STYLE = [{
        "message_id": 1, "role": "USER", "status": "FINISHED",
        "fragments": [{"id": 1, "type": "REQUEST",
                       "content": "你是一位知乎故事区的爆款创作者……"}],
    }]

    NEW_STYLE = [{"message_id": 1, "role": "USER",
                  "content": "你是一位知乎故事区的爆款创作者……"}]

    def test_fragments_style_is_read(self):
        norm = C.normalize_messages(self.OLD_STYLE)
        self.assertEqual(len(norm), 1)
        self.assertIn("知乎故事区", norm[0]["content"])
        self.assertEqual(C.match_fingerprints(C.session_text(self.OLD_STYLE)),
                         ["知乎故事区"])

    def test_content_style_still_works(self):
        self.assertEqual(C.session_text(self.NEW_STYLE),
                         self.NEW_STYLE[0]["content"])

    def test_role_is_upper_cased(self):
        norm = C.normalize_messages([{"role": "user", "content": "x"}])
        self.assertEqual(norm[0]["role"], "USER")


class TestStoryKind(unittest.TestCase):
    """story_other 再细分：模板式长提示词 vs 用户手写请求。"""

    def test_template_prompt(self):
        self.assertEqual(
            C.story_kind("你是一位故事创作者。\n\n## 格式规范（硬性要求，必须首要严格遵守）"),
            "template")
        self.assertEqual(
            C.story_kind("**Role**: 请你扮演一位顶尖的故事创作大师！"),
            "template")
        self.assertEqual(
            C.story_kind("随便说点什么\n## 创作指引\n- 叙事视角：第三人称"),
            "template")

    def test_manual_request(self):
        self.assertEqual(C.story_kind("写一个 300 字微小说：深夜便利店。"), "manual")
        self.assertEqual(C.story_kind("下面是我的语音输入，请帮我梳理表达"), "manual")
        self.assertEqual(C.story_kind(""), "manual")


class TestStoryIntent(unittest.TestCase):

    def test_manual_story_chat_detected(self):
        self.assertTrue(C.story_intent("帮我写一个故事，主题是深夜便利店"))
        # 标题 + 正文都带「故事/小说」也算（用户自己在聊创作）
        self.assertTrue(C.story_intent("这篇故事我写到一半卡住了", "后宫故事命名规划"))
        self.assertTrue(C.story_intent("我想写小说，帮我规划人物", "人物命名规划"))

    def test_non_story_chat(self):
        self.assertFalse(C.story_intent("帮我看看这台电脑的配置"))
        self.assertFalse(C.story_intent("龙虎榜上这些席位代表什么", "龙虎榜解析"))

    def test_story_other_verdict(self):
        self.assertEqual(C.classify(["故事"], 0, True, story_hint=True),
                         C.VERDICT_STORY_OTHER)
        self.assertEqual(C.classify(["故事"], 0, True, story_hint=False),
                         C.VERDICT_UNRELATED)


class TestClassify(unittest.TestCase):

    def test_confirmed(self):
        self.assertEqual(C.classify(["故事"], 2, True), C.VERDICT_CONFIRMED)

    def test_content_ok_but_no_fingerprint(self):
        self.assertEqual(C.classify(["故事"], 0, True), C.VERDICT_UNRELATED)

    def test_fetch_failed_with_title_hit(self):
        self.assertEqual(C.classify(["故事"], 0, False), C.VERDICT_LIKELY)

    def test_fetch_failed_without_title_hit(self):
        self.assertEqual(C.classify([], 0, False), C.VERDICT_ERROR)


class TestParsing(unittest.TestCase):

    def test_extract_sessions(self):
        items, more = C.extract_sessions(_page([{"id": "a"}], True))
        self.assertEqual([i["id"] for i in items], ["a"])
        self.assertTrue(more)

    def test_extract_messages(self):
        payload = {"code": 0, "data": {"biz_code": 0, "biz_data": {
            "chat_messages": [{"role": "USER", "content": "hi"}]}}}
        self.assertEqual(len(C.extract_messages(payload)), 1)
        self.assertEqual(C.extract_messages({}), [])

    def test_api_ok(self):
        self.assertTrue(C.api_ok(200, {"code": 0, "data": {"biz_code": 0}}))
        self.assertFalse(C.api_ok(401, {"code": 0, "data": {"biz_code": 0}}))
        self.assertFalse(C.api_ok(200, {"code": 40001, "data": {}}))
        self.assertFalse(C.api_ok(200, "<html>"))

    def test_session_text_ignores_assistant(self):
        msgs = [{"role": "USER", "content": "提示词"},
                {"role": "ASSISTANT", "content": "你是一位知乎故事区"}]
        self.assertEqual(C.session_text(msgs), "提示词")


class StubClient(C.DeepSeekClient):
    """桩客户端：按 lte_cursor 参数回放分页响应。"""

    def __init__(self, pages):
        super().__init__(headless=True, delay=0)
        self.pages = pages
        self.calls = []

    def _req(self, method, path, params=None, body=None, _retry=True):
        params = dict(params or {})
        self.calls.append(params)
        if params.get("lte_cursor.pinned") == "true":
            return 200, self.pages["pinned"]
        if "lte_cursor.updated_at" in params:
            return 200, self.pages.get(params["lte_cursor.updated_at"],
                                       _page([], False))
        return 200, self.pages["first"]


class TestSessionPagination(unittest.TestCase):

    def test_pagination_and_dedupe(self):
        first = _page([{"id": "a", "updated_at": 100.0},
                       {"id": "b", "updated_at": 99.0}], True)
        second = _page([{"id": "b", "updated_at": 99.0},   # 重复项
                        {"id": "c", "updated_at": 98.0}], False)
        pinned = _page([{"id": "a", "updated_at": 100.0},  # 重复项
                        {"id": "p", "updated_at": 120.0, "pinned": True}],
                       False)
        client = StubClient({"first": first, "99.000": second,
                             "pinned": pinned})
        ids = [s["id"] for s in client.sessions()]
        self.assertEqual(ids, ["a", "b", "c", "p"])
        # 第二页的游标必须是第一页最后一条的 updated_at
        self.assertIn(99.0, [c.get("lte_cursor.updated_at")
                             and float(c["lte_cursor.updated_at"])
                             for c in client.calls])

    def test_empty_list_stops(self):
        client = StubClient({"first": _page([], False),
                             "pinned": _page([], False)})
        self.assertEqual(client.sessions(), [])


if __name__ == "__main__":
    unittest.main()

class FakeClient:
    """桩客户端：记录删除调用，监控备份/复核流程。

    按用例变化的行为（哪些会话备份失败、删完还剩哪些）走类属性配置，
    由 setUp 统一复位——桩实例是 cmd_delete 内部创建的，用例拿不到
    构造时机。"""

    instances = []
    FAIL_IDS = set()
    ALIVE_AFTER = set()

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.FAIL_IDS = set()
        cls.ALIVE_AFTER = set()

    def __init__(self, **kwargs):
        self.deleted = []
        FakeClient.instances.append(self)

    # 生命周期
    def open(self):
        return {}

    def close(self):
        return None

    # 站点能力
    def history(self, sid):
        if sid in FakeClient.FAIL_IDS:
            return None, "HTTP 500 备份失败"
        return [{"role": "USER", "content": "你是一位知乎故事区的爆款创作者"}], None

    def delete_sessions(self, ids):
        self.deleted.extend(ids)
        return True, "ok"

    def sessions(self):
        return [{"id": i} for i in FakeClient.ALIVE_AFTER]


def _entry(sid, verdict=C.VERDICT_CONFIRMED, pinned=False, title="标题"):
    return {"id": sid, "title": title, "pinned": pinned, "verdict": verdict,
            "updated_at": 1700000000.0, "updated_local": "2026-08-01 00:00",
            "fp_hits": ["知乎故事区"], "age_days": 40.0}


class TestDeleteCommand(unittest.TestCase):

    def setUp(self):
        FakeClient.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.report_path = os.path.join(self.tmp.name, "report.json")
        self.backup_dir = os.path.join(self.tmp.name, "backup")
        patcher = mock.patch.object(C, "DeepSeekClient", FakeClient)
        patcher.start()
        self.addCleanup(patcher.stop)
        dir_patcher = mock.patch.object(C, "DEFAULT_REPORT_DIR", self.tmp.name)
        dir_patcher.start()
        self.addCleanup(dir_patcher.stop)

    def _write_report(self, entries):
        with open(self.report_path, "w", encoding="utf-8") as fh:
            json.dump({"candidates": entries}, fh, ensure_ascii=False)

    def test_dry_run_deletes_nothing(self):
        self._write_report([_entry("a"), _entry("b")])
        rc = C.main(["delete", "--report", self.report_path])
        self.assertEqual(rc, 0)
        self.assertEqual(FakeClient.instances, [])   # 连浏览器都没起

    def test_backup_then_delete_then_verify(self):
        self._write_report([_entry("a"), _entry("b"), _entry("c")])
        rc = C.main(["delete", "--report", self.report_path, "--yes",
                     "--backup-dir", self.backup_dir, "--chunk", "2"])
        self.assertEqual(rc, 0)
        client = FakeClient.instances[-1]
        self.assertEqual(sorted(client.deleted), ["a", "b", "c"])
        for sid in ("a", "b", "c"):
            self.assertTrue(os.path.exists(
                os.path.join(self.backup_dir, "%s.json" % sid)))
        self.assertTrue(os.path.exists(
            os.path.join(self.backup_dir, "index.json")))
        # 删除日志落盘
        logs = [f for f in os.listdir(self.tmp.name) if f.startswith("delete_")]
        self.assertEqual(len(logs), 1)

    def test_backup_failure_skips_delete(self):
        self._write_report([_entry("a"), _entry("b")])
        FakeClient.FAIL_IDS = {"a"}
        C.main(["delete", "--report", self.report_path, "--yes",
                "--backup-dir", self.backup_dir])
        client = FakeClient.instances[-1]
        self.assertEqual(client.deleted, ["b"])      # 备份失败的 a 不删

    def test_pinned_skipped_by_default(self):
        self._write_report([_entry("a"), _entry("p", pinned=True)])
        C.main(["delete", "--report", self.report_path, "--yes",
                "--backup-dir", self.backup_dir])
        self.assertEqual(FakeClient.instances[-1].deleted, ["a"])

    def test_story_other_needs_explicit_flag(self):
        self._write_report([_entry("a"),
                            _entry("s", verdict=C.VERDICT_STORY_OTHER)])
        C.main(["delete", "--report", self.report_path, "--yes",
                "--backup-dir", self.backup_dir])
        self.assertEqual(FakeClient.instances[-1].deleted, ["a"])

        FakeClient.reset()
        C.main(["delete", "--report", self.report_path, "--yes",
                "--include-other", "--backup-dir", self.backup_dir])
        self.assertEqual(sorted(FakeClient.instances[-1].deleted), ["a", "s"])

    def test_include_template_only_takes_template_kind(self):
        self._write_report([
            _entry("a"),
            _entry("t", verdict=C.VERDICT_STORY_OTHER),
            _entry("m", verdict=C.VERDICT_STORY_OTHER),
        ])
        # 给两条 story_other 标上子类型
        with open(self.report_path, encoding="utf-8") as fh:
            data = json.load(fh)
        for e in data["candidates"]:
            if e["id"] == "t":
                e["story_kind"] = "template"
            if e["id"] == "m":
                e["story_kind"] = "manual"
        with open(self.report_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)

        C.main(["delete", "--report", self.report_path, "--yes",
                "--include-template", "--backup-dir", self.backup_dir])
        self.assertEqual(sorted(FakeClient.instances[-1].deleted), ["a", "t"])

    def test_verify_reports_remaining(self):
        self._write_report([_entry("a"), _entry("b")])
        FakeClient.ALIVE_AFTER = {"b"}               # 复核发现 b 还在
        C.main(["delete", "--report", self.report_path, "--yes",
                "--backup-dir", self.backup_dir])
        logs = [f for f in os.listdir(self.tmp.name) if f.startswith("delete_")]
        with open(os.path.join(self.tmp.name, logs[0]), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["still_present_after_verify"], ["b"])


if __name__ == "__main__":
    unittest.main()
