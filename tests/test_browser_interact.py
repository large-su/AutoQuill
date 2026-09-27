# -*- coding: utf-8 -*-
# ============================================================
# tests/test_browser_interact.py — 互动 DOM 原语的纯逻辑测试
#
# JS 本身跑在真实浏览器里（探针覆盖），这里守两件事：
#   1) 纯 Python 解析函数（按钮态 / 打卡任务 / 评论时间 / 去重键）；
#   2) JS 常量里的关键选择器不丢（前端改版时这些断言先响）。
#
# 运行：.venv/Scripts/python -m unittest tests.test_browser_interact -v
# ============================================================

import unittest
from datetime import datetime

from applications.zhihu_story import browser_interact as bi

ZW = chr(8203)


class TestButtonState(unittest.TestCase):
    '''按钮文案 → 语义（真机实测三态：关注 / 已关注 / 互相关注）。'''

    def test_follow_states(self):
        self.assertEqual(bi.follow_state('关注'), 'none')
        self.assertEqual(bi.follow_state('已关注'), 'followed')
        self.assertEqual(bi.follow_state('互相关注'), 'followed')
        self.assertEqual(bi.follow_state(''), 'unknown')
        self.assertEqual(bi.follow_state('啥也不是'), 'unknown')

    def test_vote_states(self):
        self.assertEqual(bi.vote_state('赞同 746'), 'none')
        self.assertEqual(bi.vote_state('赞同'), 'none')
        self.assertEqual(bi.vote_state('已赞同 747'), 'voted')
        self.assertEqual(bi.vote_state(''), 'unknown')

    def test_zero_width_and_whitespace_stripped(self):
        # 知乎按钮文本常是 零宽字符 + 换行 + 文本，trim 去不掉零宽
        self.assertEqual(bi.follow_state(ZW + chr(10) + '已关注'), 'followed')
        self.assertEqual(bi.vote_state(ZW + ' ' + '赞同 3'), 'none')
        self.assertEqual(bi.normalize_button_text(ZW + ' 已关注 '), '已关注')


class TestCheckinTasks(unittest.TestCase):
    '''打卡页任务标题 → key / 状态（实测七项的标题固定）。'''

    def test_task_keys(self):
        self.assertEqual(bi.checkin_task_key('发布 1 篇回答'), 'answer')
        self.assertEqual(bi.checkin_task_key('发布 1 条想法'), 'pin')
        self.assertEqual(bi.checkin_task_key('关注 1 位知友'), 'follow')
        self.assertEqual(bi.checkin_task_key('发布 1 个问题'), 'question')
        self.assertEqual(bi.checkin_task_key('发布 1 条评论'), 'comment')
        self.assertEqual(bi.checkin_task_key('收听Morning Call'), 'morning_call')
        self.assertEqual(bi.checkin_task_key('送出 1 个赞同'), 'vote')
        self.assertEqual(bi.checkin_task_key('莫名其妙的第八项'), '')

    def test_parse_tasks_done_flag(self):
        raw = [
            {'title': '发布 1 篇回答', 'desc': '回答需至少 100 字以上', 'action': '已完成'},
            {'title': '关注 1 位知友', 'desc': '关注 1 位志同道合的知友', 'action': '已完成'},
            {'title': '发布 1 条评论', 'desc': '发布 1 条 10 字以上有效评论', 'action': '去评论'},
            {'title': '送出 1 个赞同', 'desc': '打卡当天送出 1 个赞同', 'action': '去赞同'},
            {'title': '', 'desc': '', 'action': '已完成'},
        ]
        tasks = bi.parse_checkin_tasks(raw)
        self.assertTrue(tasks['answer']['done'])
        self.assertTrue(tasks['follow']['done'])
        self.assertFalse(tasks['comment']['done'])
        self.assertFalse(tasks['vote']['done'])
        self.assertEqual(tasks['comment']['desc'], '发布 1 条 10 字以上有效评论')
        self.assertNotIn('', tasks)

    def test_parse_tasks_tolerates_garbage(self):
        self.assertEqual(bi.parse_checkin_tasks(None), {})
        self.assertEqual(bi.parse_checkin_tasks([None, 'x']), {})


class TestCommentTime(unittest.TestCase):
    '''评论时间文案 → datetime（实测格式 09-26 13:32 / 昨天 / 20 小时前）。'''

    def setUp(self):
        self.now = datetime(2026, 9, 27, 9, 30)

    def test_month_day(self):
        self.assertEqual(bi.parse_comment_time('09-26 13:32', now=self.now),
                         datetime(2026, 9, 26, 13, 32))

    def test_yesterday_and_today(self):
        self.assertEqual(bi.parse_comment_time('昨天 10:00', now=self.now),
                         datetime(2026, 9, 26, 10, 0))
        self.assertEqual(bi.parse_comment_time('今天 08:15', now=self.now),
                         datetime(2026, 9, 27, 8, 15))

    def test_relative(self):
        self.assertEqual(bi.parse_comment_time('20 小时前', now=self.now),
                         datetime(2026, 9, 26, 13, 30))
        self.assertEqual(bi.parse_comment_time('3 天前', now=self.now),
                         datetime(2026, 9, 24, 9, 30))

    def test_full_date(self):
        self.assertEqual(bi.parse_comment_time('2026-09-20 17:09', now=self.now),
                         datetime(2026, 9, 20, 17, 9))

    def test_cross_year_rolls_back(self):
        jan = datetime(2026, 1, 2, 10, 0)
        self.assertEqual(bi.parse_comment_time('12-30 10:00', now=jan),
                         datetime(2025, 12, 30, 10, 0))

    def test_unparseable(self):
        self.assertIsNone(bi.parse_comment_time('前几天吧', now=self.now))
        self.assertIsNone(bi.parse_comment_time('', now=self.now))


class TestManageCard(unittest.TestCase):
    '''评论管理页卡片 → 归一化 dict（实测字段：作者 / 回答 / 时间 / 正文）。'''

    def card(self):
        return {
            'index': 3,
            'author': '伊蜻蜓',
            'author_href': '//www.zhihu.com/people/yi-qing-ting-46',
            'answer_href': 'https://www.zhihu.com/answer/2087106680509166319',
            'answer_title': '《追妻火葬场的女主不回头了怎么办？》',
            'text': '被抢走的提成得上百万了吧，不走劳动仲裁？',
            'lines': ['伊蜻蜓', '评论了你的回答', '《追妻火葬场的女主不回头了怎么办？》',
                      '09-26 13:32', '被抢走的提成得上百万了吧，不走劳动仲裁？',
                      '喜欢', '回复', '推荐'],
        }

    def test_parse(self):
        now = datetime(2026, 9, 27, 9, 30)
        c = bi.parse_manage_card(self.card(), now=now)
        self.assertEqual(c['author'], '伊蜻蜓')
        self.assertEqual(c['time_text'], '09-26 13:32')
        self.assertEqual(c['time'], datetime(2026, 9, 26, 13, 32))
        self.assertEqual(c['answer_url'],
                         'https://www.zhihu.com/answer/2087106680509166319')
        self.assertIn('劳动仲裁', c['text'])
        self.assertTrue(c['key'])

    def test_answer_url_normalized(self):
        card = self.card()
        card['answer_href'] = '//www.zhihu.com/answer/123'
        c = bi.parse_manage_card(card)
        self.assertTrue(c['answer_url'].startswith('https://'))

    def test_key_stable_and_text_sensitive(self):
        a = bi.comment_key('https://www.zhihu.com/answer/1', '某人', '好看')
        b = bi.comment_key('https://www.zhihu.com/answer/1', '某人', '好看')
        c = bi.comment_key('https://www.zhihu.com/answer/1', '某人', '好看！')
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        # 归一化：空白差异不算不同评论
        d = bi.comment_key('https://www.zhihu.com/answer/1', '某人', '好 看')
        self.assertEqual(a, d)


class TestTargetFilter(unittest.TestCase):
    def test_followable(self):
        self.assertTrue(bi.is_target_followable(
            {'has_follow': True, 'follow_text': '关注'}))
        self.assertFalse(bi.is_target_followable(
            {'has_follow': True, 'follow_text': '已关注'}))
        self.assertFalse(bi.is_target_followable({'has_follow': False}))

    def test_votable(self):
        self.assertTrue(bi.is_target_votable(
            {'has_vote': True, 'vote_text': '赞同 12', 'vote_disabled': False}))
        self.assertFalse(bi.is_target_votable(
            {'has_vote': True, 'vote_text': '赞同 2', 'vote_disabled': True}))
        self.assertFalse(bi.is_target_votable(
            {'has_vote': True, 'vote_text': '已赞同 13', 'vote_disabled': False}))


class TestJsAnchors(unittest.TestCase):
    '''JS 选择器锚点：前端改版时这些断言先响（探针脚本同步复核）。'''

    def test_zw_helpers_avoid_escape_traps(self):
        # 零宽字符一律用 String.fromCharCode 生成（Python 侧转义曾被吞过）
        self.assertIn('String.fromCharCode', bi._ZW_HELPERS)
        for js in (bi._ZW_HELPERS, bi._CAMPAIGN_ENTRY_JS, bi._CHECKIN_TASKS_JS,
                   bi._LIST_TARGETS_JS, bi._CLICK_INTERACT_JS,
                   bi._MANAGE_COMMENTS_JS, bi._ANSWER_COMMENTS_JS):
            self.assertNotIn(chr(92) + 'u', js, 'JS 里不应出现 Python 风格的转义')

    def test_campaign_entry_selector(self):
        self.assertIn('/parker/campaign/', bi._CAMPAIGN_ENTRY_JS)
        self.assertIn('打卡', bi._CAMPAIGN_ENTRY_JS)

    def test_checkin_task_selector(self):
        self.assertIn('tasktree-item-top', bi._CHECKIN_TASKS_JS)   # 分组容器要排除
        self.assertIn('tasktree-btn', bi._CHECKIN_TASKS_JS)

    def test_interact_selectors(self):
        self.assertIn('FollowButton', bi._LIST_TARGETS_JS)
        self.assertIn('VoteButton', bi._LIST_TARGETS_JS)
        self.assertIn('AnswerItem', bi._ZW_HELPERS)
        self.assertIn('CommentManage-CommentCard', bi._MANAGE_COMMENTS_JS)
        self.assertIn('CommentContent', bi._ANSWER_COMMENTS_JS)

    def test_functions_are_callable_expressions(self):
        for js in (bi._CAMPAIGN_ENTRY_JS, bi._CHECKIN_TASKS_JS,
                   bi._LIST_TARGETS_JS, bi._MANAGE_COMMENTS_JS,
                   bi._ANSWER_COMMENTS_JS):
            self.assertTrue(js.startswith('('), js[:20])
            self.assertTrue(js.rstrip().endswith('}'), js[-20:])


if __name__ == '__main__':
    unittest.main()