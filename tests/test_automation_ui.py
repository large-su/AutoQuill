# -*- coding: utf-8 -*-
"""Regression tests for the read-only single-task automation cards."""

import json
import os
import re
import shutil
import subprocess
import unittest


class AutomationUiRenderTests(unittest.TestCase):
    def setUp(self):
        self.node = shutil.which("node")
        if not self.node:
            self.skipTest("node is not installed")
        self.script = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "webui", "static", "automation.js")
        )

    def render(self, task, local_done, stale_page=False, pending=()):
        data = {
            "single": {
                "axis": [],
                "checkin": {
                    "line": "打卡页已读取",
                    "tasks": {
                        "follow": {"done": True},
                        "vote": {"done": True},
                        "comment": dict(task, stale_page=stale_page),
                    },
                    "done": {"comment": local_done},
                    "pending": list(pending),
                    "notes": [{"kind": "comment", "at": "2026-10-01T09:15:00"}],
                },
                "replies": {"today": []},
            }
        }
        runner = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const input = JSON.parse(process.argv[2]);
const elements = {autoSingleCards: {innerHTML: ''}, autoStartBtn: null};
const context = {
  console, fetch: () => { throw new Error('network disabled'); },
  $: id => elements[id] || null, input,
  esc: value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  applyLeftMode: () => {}, document: {body: {classList: {toggle: () => {}}}},
};
vm.createContext(context);
vm.runInContext(source + '\nautoData = input; renderAutoSingleCards();', context);
process.stdout.write(elements.autoSingleCards.innerHTML);
"""
        proc = subprocess.run(
            [self.node, "-e", runner, self.script, json.dumps(data, ensure_ascii=False)],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        if proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, proc.args, proc.stdout, proc.stderr)
        return proc.stdout

    def assert_comment_row(self, html, done, explanation):
        rows = [row for row in re.findall(r'<div class="sa-item">(.*?)</div>', html)
                if "发布 1 条评论" in row]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIn('<span class="ok">√</span>' if done else '<span class="no">×</span>', row)
        self.assertIn(explanation, row)

    def test_platform_false_local_true_is_pending_sync(self):
        html = self.render({"done": False, "action": "去评论"}, True)
        self.assert_comment_row(html, True, "自动化 09:15（平台待同步）")
        self.assertIn("今日已达标", html)

    def test_platform_true_local_false_is_done(self):
        html = self.render({"done": True, "action": "待评论"}, False)
        self.assert_comment_row(html, True, "已完成")
        self.assertIn("今日已达标", html)

    def test_both_false_keeps_action_and_cross(self):
        html = self.render({"done": False, "action": "去评论"}, False, pending=["comment"])
        self.assert_comment_row(html, False, "去评论")
        self.assertIn("还差 1 项", html)

    def test_stale_platform_done_local_true_is_pending_sync(self):
        html = self.render({"done": True, "action": "待评论"}, True, stale_page=True)
        self.assert_comment_row(html, True, "自动化 09:15（平台待同步）")


if __name__ == "__main__":
    unittest.main()
