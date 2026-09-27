# -*- coding: utf-8 -*-
# ============================================================
# tests/test_checkin.py — 打卡状态与决策（纯逻辑，不碰浏览器）
#
# 覆盖：跨天重置 / 打卡页状态与本地台账取并集 / decide 四分支 /
#       target_action（直接做 / 翻转 / 跳过）/ 评论回复台账去重。
#
# 运行：.venv/Scripts/python -m unittest tests.test_checkin -v
# ============================================================

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

from core import checkin as ck
from core import paths


class CheckinBase(unittest.TestCase):
    '''把数据目录指到临时目录（绝不碰真实 data/state）。'''

    def setUp(self):
        self._orig_root = paths.DATA_ROOT
        self.tmp = tempfile.mkdtemp(prefix='aq_checkin_')
        paths.DATA_ROOT = self.tmp

    def tearDown(self):
        paths.DATA_ROOT = self._orig_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_raw(self, data):
        with open(ck.state_path(), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)


class TestState(CheckinBase):
    def test_fresh_state_defaults(self):
        s = ck.load_state()
        self.assertEqual(s['date'], ck.today_key())
        self.assertEqual(s['done'], {'follow': False, 'vote': False, 'comment': False})
        self.assertEqual(s['tasks'], {})
        self.assertEqual(s['tried'], [])

    def test_cross_day_resets_but_keeps_campaign(self):
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        self.write_raw({'date': yesterday, 'campaign_url': 'https://x/1',
                        'campaign_title': '第五十三期',
                        'done': {'follow': True}, 'tasks': {'follow': {'done': True}}})
        s = ck.load_state()
        self.assertEqual(s['date'], ck.today_key())
        self.assertFalse(s['done']['follow'])          # 本地台账跨天清零
        self.assertEqual(s['tasks'], {})               # 当日任务状态重新读
        self.assertEqual(s['campaign_url'], 'https://x/1')   # 当期链接沿用
        self.assertEqual(s['campaign_title'], '第五十三期')

    def test_corrupt_file_tolerated(self):
        with open(ck.state_path(), 'w', encoding='utf-8') as f:
            f.write('{不是 json')
        s = ck.load_state()
        self.assertEqual(s['date'], ck.today_key())

    def test_save_and_load_roundtrip(self):
        s = ck.load_state()
        ck.update_tasks(s, {'comment': {'title': '发布 1 条评论', 'done': False}})
        ck.mark_done(s, 'follow', detail='关注了某某')
        ck.save_state(s)
        again = ck.load_state()
        self.assertTrue(again['done']['follow'])
        self.assertFalse(again['tasks']['comment']['done'])
        self.assertTrue(again['checked_at'])


class TestNeedsAndDecide(CheckinBase):
    def test_page_done_wins(self):
        s = ck.load_state()
        ck.update_tasks(s, {'follow': {'title': '关注 1 位知友', 'done': True}})
        self.assertFalse(ck.needs(s, 'follow'))       # 用户手动做过的也算
        self.assertEqual(ck.decide(s, 'follow'), 'done')

    def test_local_done_wins_when_page_stale(self):
        s = ck.load_state()
        ck.update_tasks(s, {'vote': {'title': '送出 1 个赞同', 'done': False}})
        ck.mark_done(s, 'vote')
        self.assertFalse(ck.needs(s, 'vote'))
        self.assertEqual(ck.decide(s, 'vote'), 'done')

    def test_pending_then_do_then_toggle(self):
        s = ck.load_state()
        ck.update_tasks(s, {'follow': {'title': '关注 1 位知友', 'done': False}})
        self.assertTrue(ck.needs(s, 'follow'))
        self.assertEqual(ck.decide(s, 'follow'), 'do')
        self.assertEqual(ck.decide(s, 'follow', is_last=True), 'toggle')

    def test_disabled_or_unknown_kind(self):
        s = ck.load_state()
        self.assertEqual(ck.decide(s, 'follow', enabled=False), 'skip')
        self.assertEqual(ck.decide(s, 'morning_call'), 'skip')
        self.assertFalse(ck.needs(s, 'morning_call'))

    def test_pending_kinds_lists_three(self):
        s = ck.load_state()
        ck.update_tasks(s, {
            'follow': {'title': '关注 1 位知友', 'done': True},
            'vote': {'title': '送出 1 个赞同', 'done': False},
            'comment': {'title': '发布 1 条评论', 'done': False},
        })
        self.assertEqual(ck.pending_kinds(s), ['vote', 'comment'])


class TestTargetAction(CheckinBase):
    def test_follow_targets(self):
        self.assertEqual(ck.target_action(
            'follow', {'has_follow': True, 'follow_text': '关注'}), 'do')
        self.assertEqual(ck.target_action(
            'follow', {'has_follow': True, 'follow_text': '已关注'}), 'skip')
        self.assertEqual(ck.target_action(
            'follow', {'has_follow': True, 'follow_text': '已关注'}, is_last=True),
            'toggle')
        self.assertEqual(ck.target_action(
            'follow', {'has_follow': True, 'follow_text': '互相关注'}, is_last=True),
            'toggle')
        self.assertEqual(ck.target_action('follow', {'has_follow': False}), 'skip')

    def test_vote_targets(self):
        self.assertEqual(ck.target_action('vote',
            {'has_vote': True, 'vote_text': '赞同 12', 'vote_disabled': False}), 'do')
        self.assertEqual(ck.target_action('vote',
            {'has_vote': True, 'vote_text': '已赞同 13', 'vote_disabled': False}, is_last=True),
            'toggle')
        self.assertEqual(ck.target_action('vote',
            {'has_vote': True, 'vote_text': '已赞同 13', 'vote_disabled': False}), 'skip')
        # 自己的回答：赞同按钮 disabled → 永远跳过（实测自己回答是 disabled）
        self.assertEqual(ck.target_action('vote',
            {'has_vote': True, 'vote_text': '赞同 2', 'vote_disabled': True}), 'skip')
        self.assertEqual(ck.target_action('vote', {'has_vote': False}), 'skip')

    def test_unknown_kind(self):
        self.assertEqual(ck.target_action('comment', {}), 'skip')


class TestTriedAndSummary(CheckinBase):
    def test_tried_authors(self):
        s = ck.load_state()
        ck.mark_tried(s, 'follow', author='张三', reason='已关注')
        ck.mark_tried(s, 'follow', author='李四', reason='已关注')
        ck.mark_tried(s, 'vote', author='王五', reason='已赞同')
        self.assertEqual(ck.tried_authors(s, 'follow'), {'张三', '李四'})
        self.assertEqual(ck.tried_authors(s), {'张三', '李四', '王五'})

    def test_summary_pending_and_done(self):
        s = ck.load_state()
        ck.update_tasks(s, {
            'follow': {'title': '关注 1 位知友', 'done': True},
            'vote': {'title': '送出 1 个赞同', 'done': False},
            'comment': {'title': '发布 1 条评论', 'done': False},
        })
        got = ck.summary(s)
        self.assertFalse(got['ok'])
        self.assertEqual(got['pending'], 2)
        self.assertIn('关注知友✓', got['line'])
        self.assertIn('送出赞同✗', got['line'])

    def test_summary_unknown_when_page_missing_task(self):
        # 打卡页没报这两项（活动改版/解析失败）：本地做过的算完成，
        # 没做过的只能记「未知」，不能当成「已完成」把打卡结论做真（宁可不吹）
        s = ck.load_state()
        ck.update_tasks(s, {'follow': {'title': '关注 1 位知友', 'done': True}})
        ck.mark_done(s, 'vote')
        got = ck.summary(s)
        self.assertEqual(got['unknown'], 1)
        self.assertEqual(got['pending'], 0)
        self.assertFalse(got['ok'])
        self.assertIn('发布评论?', got['line'])


class TestReplyLedger(CheckinBase):
    def test_append_and_dedupe(self):
        key = 'abc123'
        self.assertFalse(ck.is_replied(key))
        ck.append_reply({'key': key, 'author': '伊蜻蜓', 'text': '谢谢', 'sent': True})
        self.assertTrue(ck.is_replied(key))
        self.assertEqual(ck.replied_keys(), {key})
        self.assertEqual(len(ck.replies_today()), 1)
        ck.append_reply({'key': 'other', 'sent': True})
        self.assertEqual(len(ck.load_replies()), 2)
        self.assertEqual(len(ck.load_replies(limit=1)), 1)

    def test_replies_today_filters_old(self):
        ck.append_reply({'key': 'k1', 'at': '2020-01-01T10:00:00'})
        self.assertEqual(ck.replies_today(), [])


    def test_dryrun_records_do_not_block_auto_reply(self):
        """演练草稿不占用评论：切自动后那条评论仍可回复（用户口径）。"""
        ck.append_reply({'key': 'k1', 'dry_run': True, 'sent': False},
                        )
        self.assertEqual(ck.dryrun_keys(), {'k1'})
        self.assertEqual(ck.replied_keys(), set(), '演练没发出去，不算已回复')
        ck.append_reply({'key': 'k2', 'dry_run': False, 'sent': True})
        self.assertEqual(ck.replied_keys(), {'k2'})
        self.assertEqual(ck.dryrun_keys(), {'k1'})
        ck.append_reply({'key': 'k3', 'failed': True})
        self.assertIn('k3', ck.replied_keys(), '生成不合格的也不再反复试')

    def test_is_replied_empty_key(self):
        self.assertFalse(ck.is_replied(''))
        self.assertFalse(ck.is_replied(None))


class TestContextIsolation(unittest.TestCase):
    """★ 用户 2026-09-27 的核心要求：手动写作绝不被打卡互动打扰。

    打卡上下文只由自动化执行器写；手动跑完整链路/批量/看板一律没有上下文，
    钩子必须在碰浏览器之前就返回（这里用「一碰就炸」的假浏览器守住）。
    """

    def setUp(self):
        ck.clear_context()

    def tearDown(self):
        ck.clear_context()

    def _workflow(self):
        from workflows.base import WorkflowBase

        class BoomBrowser:
            def __getattr__(self, name):
                raise AssertionError('没有上下文时不该碰浏览器：%s' % name)

        class W(WorkflowBase):
            def _browser(self):
                return BoomBrowser()

        return W()

    def test_no_context_means_no_browser_touch(self):
        self.assertEqual(ck.get_context(), {})
        self.assertIsNone(
            self._workflow().maybe_checkin_interact('https://www.zhihu.com/question/1'))

    def test_both_kinds_settled_means_no_browser_touch(self):
        ck.set_context({'follow': 'done', 'vote': 'skip', 'is_last': False})
        self.assertIsNone(
            self._workflow().maybe_checkin_interact('https://www.zhihu.com/question/1'))

    def test_context_expires_after_ttl(self):
        t0 = datetime(2026, 9, 27, 9, 0)
        ck.set_context({'follow': 'do'}, now=t0)
        self.assertEqual(ck.get_context(now=t0), {'follow': 'do'})
        self.assertAlmostEqual(ck.context_age_minutes(now=t0), 0.0, places=3)
        later = t0 + timedelta(minutes=ck.CONTEXT_TTL_MINUTES + 1)
        self.assertEqual(ck.get_context(now=later), {},
                         '过期上下文必须失效（防泄漏到手动作业）')
        self.assertIsNone(ck.context_age_minutes(now=later))

    def test_clear_context(self):
        ck.set_context({'follow': 'do'})
        self.assertTrue(ck.get_context())
        ck.clear_context()
        self.assertEqual(ck.get_context(), {})


class TestContextWriterIsOnlyAutomation(unittest.TestCase):
    """源码级守护：能写打卡上下文的**只有自动化执行器**。

    用户 2026-09-27 明确要求「手动写作时绝不能被牵着去点关注/赞同」。
    这条测试把「谁能写上下文」钉死在源码上——将来若有人从 webui /
    workflows / applications 里写上下文，这里立刻变红。
    """

    ALLOWED = {'automation/executor.py', 'core/checkin.py'}

    def test_only_automation_writes_context(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        hits = set()
        for pkg in ('applications', 'automation', 'core', 'webui', 'workflows'):
            for dirpath, dirnames, filenames in os.walk(os.path.join(root, pkg)):
                dirnames[:] = [d for d in dirnames if d != '__pycache__']
                for fn in filenames:
                    if not fn.endswith('.py'):
                        continue
                    path = os.path.join(dirpath, fn)
                    with open(path, encoding='utf-8', errors='ignore') as f:
                        text = f.read()
                    if ('_set_checkin_context(' in text
                            or 'checkin.set_context(' in text):
                        hits.add(os.path.relpath(path, root).replace(os.sep, '/'))
        self.assertTrue(hits <= self.ALLOWED,
                        '打卡上下文的写入点只能有 %s，实际 %s' % (self.ALLOWED, hits))
        self.assertIn('automation/executor.py', hits,
                      '自动化执行器仍应是上下文的唯一写入方')


if __name__ == '__main__':
    unittest.main()

