# -*- coding: utf-8 -*-
"""草稿箱数据层单测：归一化 / 加载与回退 / 筛选排序 / 统计。"""
import json
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from webui import drafts


def _rows():
    return [
        {"qid": "1", "url": "u1", "title": "AI测试问题一？",
         "updated": "编辑于 2026-08-20 10:00", "content": "这是草稿内容一，" * 20},
        {"qid": "2", "url": "u2", "title": "AI测试问题二？",
         "updated": "编辑于 2026-08-22 10:00", "content": "短稿"},
        {"qid": "3", "url": "u3", "title": "别的主题",
         "updated": "编辑于 2026-07-01 10:00", "content": "中长度草稿，" * 40},
    ]


class DraftsDataLayerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aq_drafts_t_"))
        drafts._DATA_DIR = self.tmp

    def _seed(self, name, rows):
        (self.tmp / name).write_text(json.dumps(rows), encoding="utf-8")

    def test_normalize_row(self):
        r = drafts._normalize_row(_rows()[0])
        self.assertEqual(r["qid"], "1")
        self.assertEqual(r["updated_date"], "2026-08-20")
        self.assertGreater(r["chars"], 100)
        self.assertEqual(r["title"], "AI测试问题一？")

    def test_load_raw_and_normalized_compat(self):
        self._seed("drafts_2026-08-22.json", _rows())
        self._seed("drafts_2026-08-23.json",
                   [drafts._normalize_row(r) for r in _rows()])
        d = drafts.load()
        self.assertEqual(d["total"], 3)
        self.assertTrue(all("updated_date" in r and "chars" in r
                            for r in d["rows"]))

    def test_load_falls_back_when_newest_empty(self):
        self._seed("drafts_2026-08-22.json", _rows())
        self._seed("drafts_2026-08-23.json", [])
        d = drafts.load()
        self.assertEqual(d["total"], 3)

    def test_filter_rows(self):
        rows = [drafts._normalize_row(r) for r in _rows()]
        fr = drafts.filter_rows(rows, q="测试", start="2026-08-01")
        self.assertEqual([r["qid"] for r in fr], ["2", "1"])
        fr2 = drafts.filter_rows(rows, q="测试", sort="updated", direction="asc")
        self.assertEqual([r["qid"] for r in fr2], ["1", "2"])
        fr3 = drafts.filter_rows(rows, sort="chars", direction="desc")
        self.assertEqual(fr3[0]["qid"], "3")
        fr4 = drafts.filter_rows(rows, min_chars=200, max_chars=500)
        self.assertEqual([r["qid"] for r in fr4], ["3"])

    def test_extract_js_qid_regex_valid(self):
        # 回归（V4.5.0 实测事故）：草稿卡 qid 提取正则曾被写成
        # /question/(d+)/ —— 斜杠未转义且 \d 丢失反斜杠，浏览器 evaluate
        # 直接抛 SyntaxError，safe_evaluate 吞错返回 None → 草稿箱恒空。
        js = drafts._EXTRACT_JS
        self.assertIn('match(/question\\/(\\d+)/)', js)
        self.assertNotIn('/question/(d+)', js)
        hit = re.search(r'/question\/(\d+)',
                         'https://www.zhihu.com/question/88888888#write')
        self.assertIsNotNone(hit)
        self.assertEqual(hit.group(1), '88888888')

        # 知乎改版（2026-08）：标题/时间/正文均为 div，不再用 span 与 data-tooltip
        self.assertIn('.CreationCardTitle-wrapper', js)
        self.assertNotIn('.CreationCardTitle-wrapper span', js)
        self.assertNotIn('[data-tooltip]', js)
        self.assertIn('.CreationCardContent-text', js)
        self.assertNotIn('.CreationCardContent-text span', js)

    def test_rel_to_date(self):
        """相对时间 → 精确日期，覆盖日/月/年边界。

        CI 曾在 UTC 2026-10-01 04:21 运行旧断言：它只要求「5 小时前」
        仍属于当前月份，但生产函数返回 2026-09-30，这是跨日时的正确结果。
        测试固定时钟并断言完整日期，避免依赖运行时区与当前时间。
        """
        fixed = datetime(2026, 10, 1, 12, 0, 0)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

        with mock.patch.object(drafts, "datetime", _FrozenDatetime):
            self.assertEqual(drafts._rel_to_date('昨天'), '2026-09-30')
            self.assertEqual(drafts._rel_to_date('前天'), '2026-09-29')
            self.assertEqual(drafts._rel_to_date('3 天前'), '2026-09-28')
            self.assertEqual(drafts._rel_to_date('5 小时前'), '2026-10-01')
            self.assertEqual(drafts._rel_to_date('昨天 20:21'), '2026-09-30')
            self.assertEqual(drafts._rel_to_date('2026-08-20 10:00'), '2026-08-20')
            self.assertEqual(drafts._rel_to_date(''), '')
            self.assertEqual(drafts._rel_to_date('无法识别'), '')

        # 原 CI 触发条件：月初凌晨的 5 小时前属于上月。
        fixed = datetime(2026, 10, 1, 4, 20, 0)
        with mock.patch.object(drafts, "datetime", _FrozenDatetime):
            self.assertEqual(drafts._rel_to_date('5 小时前'), '2026-09-30')

        # 跨月：非闰年 3 月 1 日的前一小时属于 2 月。
        fixed = datetime(2026, 3, 1, 0, 20, 0)
        with mock.patch.object(drafts, "datetime", _FrozenDatetime):
            self.assertEqual(drafts._rel_to_date('1 小时前'), '2026-02-28')

        # 跨年：元旦前一小时属于上一年的 12 月 31 日。
        fixed = datetime(2025, 1, 1, 0, 20, 0)
        with mock.patch.object(drafts, "datetime", _FrozenDatetime):
            self.assertEqual(drafts._rel_to_date('1 小时前'), '2024-12-31')

    def test_draft_html_text(self):
        self.assertEqual(drafts._draft_html_text('<p>你好</p><p>世界</p>'), '你好世界')
        self.assertEqual(drafts._draft_html_text('&lt;b&gt;标签&lt;/b&gt; &amp; 实体'),
                         '<b>标签</b> & 实体')
        self.assertEqual(drafts._draft_html_text('<br />'), '')
        self.assertEqual(drafts._draft_html_text(''), '')

    def test_summarize(self):
        rows = [drafts._normalize_row(r) for r in _rows()]
        st = drafts.summarize(drafts.filter_rows(rows, q="测试"))
        self.assertEqual(st["total"], 2)
        self.assertEqual(st["date_min"], "2026-08-20")
        self.assertEqual(st["date_max"], "2026-08-22")
        self.assertGreater(st["avg_chars"], 0)


if __name__ == "__main__":
    unittest.main()
