# -*- coding: utf-8 -*-
"""本地快照同步剔除 + 知乎登录态失效识别（2026-09-19 两大问题的回归）。

背景（用户实测）：
  1) 看板/草稿箱刷新报「刷新失败」——真实原因是知乎服务端把会话登出，页面被
     重定向到 /signin，抓取拿到 0 条；此前只提示「可能未登录/页面改版」。
  2) 在知乎上删掉已发布回答/草稿后，本地快照没同步，用户必须整页重抓一次。
本文件守住：登录失效的直接识别（page_needs_login）与删除成功后的本地剔除。
"""
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from webui import _snapshot as snap
from webui import drafts, published


def _rows(key, values):
    return [{key: v, "title": "标题" + str(v)} for v in values]


class PruneRowsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_prune_"))

    def _seed(self, name, rows):
        path = self.tmp / name
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return path

    def test_removes_only_matching_rows(self):
        path = self._seed("published_answers_2026-09-19.json",
                          _rows("aid", ["1", "2", "3"]))
        removed = snap.prune_rows(self.tmp, "published_answers_*.json",
                                  "aid", ["2", "3"])
        self.assertEqual(removed, 2)
        left = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([r["aid"] for r in left], ["1"])

    def test_no_snapshot_returns_zero(self):
        self.assertEqual(
            snap.prune_rows(self.tmp, "published_answers_*.json", "aid", ["1"]), 0)

    def test_no_match_keeps_file_untouched(self):
        path = self._seed("drafts_2026-09-19.json", _rows("qid", ["7", "8"]))
        before = path.read_text(encoding="utf-8")
        self.assertEqual(snap.prune_rows(self.tmp, "drafts_*.json", "qid", ["9"]), 0)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_empty_values_is_noop(self):
        path = self._seed("drafts_2026-09-19.json", _rows("qid", ["7"]))
        before = path.read_text(encoding="utf-8")
        self.assertEqual(snap.prune_rows(self.tmp, "drafts_*.json", "qid", []), 0)
        self.assertEqual(snap.prune_rows(self.tmp, "drafts_*.json", "qid",
                                          [None, ""]), 0)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_broken_json_returns_zero(self):
        (self.tmp / "drafts_2026-09-19.json").write_text("{不是数组",
                                                          encoding="utf-8")
        self.assertEqual(snap.prune_rows(self.tmp, "drafts_*.json", "qid", ["7"]), 0)

    def test_newest_snapshot_wins(self):
        import os
        import time
        old = self._seed("drafts_2026-09-01.json", _rows("qid", ["1"]))
        new = self._seed("drafts_2026-09-19.json", _rows("qid", ["1", "2"]))
        os.utime(old, (time.time() - 9999, time.time() - 9999))
        self.assertEqual(drafts_prune(self.tmp, ["2"]), 1)
        self.assertEqual(json.loads(new.read_text(encoding="utf-8"))[0]["qid"], "1")
        self.assertEqual(len(json.loads(old.read_text(encoding="utf-8"))), 1)


def drafts_prune(data_dir, qids):
    """在给定目录上跑 drafts.prune_qids（临时改模块级 _DATA_DIR）。"""
    with mock.patch.object(drafts, "_DATA_DIR", data_dir):
        return drafts.prune_qids(qids)


class ModulePruneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_prune_mod_"))

    def test_published_prune_aids(self):
        (self.tmp / "published_answers_2026-09-19.json").write_text(
            json.dumps(_rows("aid", ["a1", "a2"]), ensure_ascii=False),
            encoding="utf-8")
        with mock.patch.object(published, "_DATA_DIR", self.tmp):
            removed = published.prune_aids(["a1"])
        self.assertEqual(removed, 1)
        left = json.loads((self.tmp / "published_answers_2026-09-19.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual([r["aid"] for r in left], ["a2"])

    def test_drafts_prune_qids(self):
        (self.tmp / "drafts_2026-09-19.json").write_text(
            json.dumps(_rows("qid", ["q1", "q2"]), ensure_ascii=False),
            encoding="utf-8")
        self.assertEqual(drafts_prune(self.tmp, ["q2"]), 1)


class LoginExpiredDetectionTest(unittest.TestCase):
    def _page(self, url):
        return types.SimpleNamespace(url=url)

    def test_signin_url_detected(self):
        from applications.zhihu_story.browser_adapter import page_needs_login
        self.assertTrue(page_needs_login(self._page(
            "https://www.zhihu.com/signin?next=%2Fcreator%2Fmanage%2Fcreation%2Fanswer")))
        self.assertFalse(page_needs_login(self._page(
            "https://www.zhihu.com/creator/manage/creation/answer")))

    def test_page_without_url_is_safe(self):
        from applications.zhihu_story.browser_adapter import page_needs_login

        class _Bad:
            @property
            def url(self):
                raise RuntimeError("page closed")

        self.assertFalse(page_needs_login(_Bad()))
        self.assertFalse(page_needs_login(types.SimpleNamespace(url="")))

    def test_scrapers_and_deletes_guard_on_login(self):
        """两个抓取器与两条删除链路都必须检查「页面是否停在登录页」。"""
        for rel in ("webui/published.py", "webui/drafts.py"):
            with open(rel, encoding="utf-8") as f:
                src = f.read()
            self.assertIn("page_needs_login", src, rel)
            self.assertIn("ZhihuLoginRequired", src, rel)
            self.assertIn("LOGIN_EXPIRED_MSG", src, rel)

    def test_delete_endpoints_sync_local_snapshot(self):
        """知乎删除成功后必须同步剔除本地快照（用户在网页端删完不必重抓）。"""
        with open("webui/dashboard_api.py", encoding="utf-8") as f:
            dash = f.read()
        with open("webui/drafts_api.py", encoding="utf-8") as f:
            drf = f.read()
        self.assertIn("published.prune_aids(deleted)", dash)
        self.assertIn("drafts.prune_qids(deleted)", drf)
        self.assertIn("need_login=True", dash)
        self.assertIn("need_login=True", drf)


if __name__ == "__main__":
    unittest.main()
