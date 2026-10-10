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
from unittest.mock import Mock, patch

from core import checkin as ck
from core import paths
from applications.zhihu_story import checkin_task as task
from applications.zhihu_story import comment_fallback as cfb


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

    def test_cross_day_clears_reader_check_and_reference_story(self):
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        self.write_raw({
            'date': yesterday,
            'reader_comments': {'checked_at': yesterday + 'T09:00:00',
                                'outcome': 'no_candidate',
                                'dry_run': False, 'result': {}},
            'reference_story': {'title': '旧故事', 'url': 'https://x/old',
                                'text': '旧正文'},
        })
        s = ck.load_state()
        self.assertEqual(s['reader_comments'], {})
        self.assertEqual(s['reference_story'], {})

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


class _FakeCommentBrowser:
    '''只实现评论兜底用到的那几个方法。'''

    def __init__(self, send_ok=True, manage_comments=None, tasks=None,
                 targets=None, send_results=None, answer_url=''):
        self.comments = []
        self.opened = []
        self.send_ok = send_ok
        self.manage_comments = list(manage_comments or [])
        self.collect_manage_comments_calls = 0
        self.tasks = tasks or {}
        self.targets = targets if targets is not None else []
        self.send_results = list(send_results or [])

        class _Page:
            def __init__(self, url):
                self.url = url
                self.gotos = []
                self.fail = False

            def goto(self, url, **kwargs):
                self.gotos.append((url, kwargs))
                if self.fail:
                    raise RuntimeError('导航失败')
                self.url = url

        self.page = _Page(answer_url)

    def open_question(self, url):
        self.opened.append(url)

    def collect_manage_comments(self, limit=20):
        self.collect_manage_comments_calls += 1
        return {'comments': list(self.manage_comments)}

    def read_checkin_tasks(self, _url):
        return {'tasks': dict(self.tasks), 'title': '测试打卡'}

    def list_interact_targets(self):
        return {'items': list(self.targets)}

    def discover_campaign_url(self):
        return {'ok': True, 'url': 'https://example.test/checkin', 'text': '测试'}

    def send_answer_comment(self, text, dry_run=False):
        self.comments.append(text)
        if self.send_results:
            return self.send_results.pop(0)
        if self.send_ok:
            return {'ok': True, 'sent': True, 'detail': '已发送并确认'}
        return {'ok': False, 'sent': False, 'detail': '发布按钮不可用'}

    def _settle_answer_page(self, timeout=15):
        return None


class CommentFallbackTest(unittest.TestCase):
    '''打卡评论兜底：当天最后一班在参考故事下补一条贴题评论。

    用户口径（2026-09-29）：回复读者评论那条链路会「挑不出合适的评论」而整体
    跳过——界面显示完成、实际一条评论都没发出去，打卡的「发布评论」项永远
    达不成。评论别人的回答没有任何限制，所以最后一班就地补一条做保底。
    '''

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='aq_cfb_')
        self._orig_root = paths.DATA_ROOT
        paths.DATA_ROOT = self.tmp

    def test_default_ask_is_lazy_and_closes_its_own_driver(self):
        driver = Mock()
        with patch('web_drivers.create_driver', return_value=driver):
            ask = task._default_ask(None)
            driver_factory = __import__('web_drivers').create_driver
            self.assertEqual(driver_factory.call_count, 0)
            with patch('applications.zhihu_story.reply_task.ask_llm', return_value='答') as call:
                self.assertEqual(ask('题'), '答')
                self.assertIs(call.call_args.kwargs['driver'], driver)
            ask.close()
        driver.delete_current_session.assert_called_once_with()
        driver.close_session.assert_called_once_with()

    def test_default_fallback_ask_closes_after_generation_cancel(self):
        driver = Mock()
        browser = _FakeCommentBrowser()
        state = ck.fresh_state()

        def compose_then_cancel(ask, *args, **kwargs):
            ask('先创建自己的网页会话')
            raise KeyboardInterrupt

        with patch('web_drivers.create_driver', return_value=driver), \
                patch.object(cfb, 'compose', side_effect=compose_then_cancel):
            with self.assertRaises(KeyboardInterrupt):
                task.maybe_comment_fallback(
                    browser,
                    ctx={'is_last': True,
                         'story': {'title': '题', 'text': '正文有具体细节。',
                                   'url': 'https://example.test/q/1'}},
                    state=state)
        driver.delete_current_session.assert_called_once_with()
        driver.close_session.assert_called_once_with()

    def test_injected_ask_is_not_closed_by_fallback_wrapper(self):
        browser = _FakeCommentBrowser()
        injected = Mock(side_effect=lambda *args, **kwargs: '生成内容')

        with patch.object(cfb, 'compose', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                task.maybe_comment_fallback(
                    browser,
                    ctx={'is_last': True,
                         'story': {'title': '题', 'text': '正文有具体细节。',
                                   'url': 'https://example.test/q/1'}},
                    state=ck.fresh_state(), ask=injected)
        injected.close.assert_not_called()

    def test_default_ask_api_mode_does_not_create_driver(self):
        driver_factory = Mock()
        with patch('config.LLM_MODE', 'api'), \
                patch('web_drivers.create_driver', driver_factory), \
                patch('applications.zhihu_story.reply_task.ask_llm', return_value='答'):
            ask = task._default_ask(None)
            self.assertEqual(ask('题'), '答')
            ask.close()
        driver_factory.assert_not_called()

    def tearDown(self):
        paths.DATA_ROOT = self._orig_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 纯逻辑：提示词与校验 ----------

    def test_prompt_feeds_story_and_forbids_empty_praise(self):
        p = cfb.build_prompt('测试标题', '他把伞收起来，水珠落在地砖上。')
        self.assertIn('测试标题', p)
        self.assertIn('水珠落在地砖上', p)          # 正文真的喂进去了
        self.assertIn('贴着这段内容说', p)          # 要求贴题
        self.assertIn('写得好', p)                  # 明确禁止空话
        self.assertIn('%d-%d 字' % (cfb.COMMENT_MIN_CHARS, cfb.COMMENT_MAX_CHARS), p)

    def test_check_comment_accepts_specific_comment(self):
        self.assertEqual(
            cfb.check_comment('那把伞收起来的细节写得好，水珠那段一下就有画面了'), [])

    def test_check_comment_flags_too_short(self):
        issues = cfb.check_comment('写得好')
        self.assertTrue(any('太短' in x for x in issues))

    def test_check_comment_flags_cliche(self):
        issues = cfb.check_comment('感谢您的认可，故事真的很精彩我一直在追更呢')
        self.assertTrue(any('套话' in x for x in issues), issues)

    def test_check_comment_flags_emoji_text(self):
        issues = cfb.check_comment('这段反转挺意外的[微笑]后面还有吗想继续看')
        self.assertTrue(any('表情包' in x for x in issues), issues)

    def test_check_comment_flags_duplicate_of_today(self):
        old = '那把伞收起来的细节写得好，水珠那段一下就有画面了'
        issues = cfb.check_comment(old, avoid_texts=[old])
        self.assertTrue(any('太像' in x for x in issues), issues)

    def test_extract_comment_strips_wrapping(self):
        self.assertEqual(cfb.extract_comment('「他把伞收起来了，这个动作有味道」'),
                         '他把伞收起来了，这个动作有味道')

    # ---------- compose：生成 → 校验 → 重写 ----------

    def test_compose_retries_when_too_short(self):
        answers = iter(['写得好', '那把伞收起来的细节写得好，水珠那段一下就有画面了'])
        got = cfb.compose(lambda *a, **k: next(answers), '题', '正文')
        self.assertTrue(got['ok'])
        self.assertIn('伞', got['comment'])

    def test_compose_retries_when_driver_returns_empty(self):
        answers = iter(['', '这段反转收得干净，最后一句留白很舒服我反复看了两遍'])
        got = cfb.compose(lambda *a, **k: next(answers), '题', '正文')
        self.assertTrue(got['ok'])

    def test_compose_gives_up_after_budget(self):
        got = cfb.compose(lambda *a, **k: '写得好', '题', '正文', max_retry=1)
        self.assertFalse(got['ok'])
        self.assertTrue(got['issues'])

    def test_compose_survives_ask_exception(self):
        def boom(*a, **k):
            raise RuntimeError('驱动炸了')
        got = cfb.compose(boom, '题', '正文')
        self.assertFalse(got['ok'])
        self.assertTrue(any('提问失败' in x for x in got['issues']))

    # ---------- 触发条件（浏览器用替身） ----------

    def test_skips_when_not_last_run(self):
        r = task.maybe_comment_fallback(_FakeCommentBrowser(),
                                        ctx={'is_last': False})
        self.assertTrue(r['skipped'])
        self.assertFalse(r['sent'])

    def test_read_story_falls_back_after_daily_reader_check_without_last_slot(self):
        '''自有评论检查无合适对象后，已读到故事就补评论，不依赖末班成功。'''
        from workflows.base import WorkflowBase

        st = ck.fresh_state()
        st['reader_comments'] = {'checked_at': ck.now_str(),
                                 'outcome': 'no_candidate', 'dry_run': False,
                                 'result': {'ok': True, 'units': 0,
                                            'detail': '没有可回复的新评论',
                                            'replies': [], 'dropped': []}}
        ck.save_state(st)
        browser = _FakeCommentBrowser()
        workflow = WorkflowBase()
        workflow._browser = lambda: browser
        ck.set_context({'is_last': False, 'follow': 'done', 'vote': 'done',
                        'comment': 'do', 'reader_check': True})
        try:
            with patch.object(task, '_default_ask', return_value=lambda *a, **k:
                              '那把伞收起来的细节写得好，水珠那段一下就有画面了'):
                workflow.maybe_checkin_interact(
                    'https://www.zhihu.com/question/1',
                    story={'title': '题', 'text': '他把伞收起来，水珠落在地砖上。',
                           'url': 'https://www.zhihu.com/question/1'})
        finally:
            ck.clear_context()
        self.assertEqual(len(browser.comments), 1,
                         '已检查自有评论却等末班，会在末班失败时漏掉整日评论')
        self.assertTrue(ck.load_state()['done']['comment'])

    def test_reader_scan_once_then_fallback_on_later_stories(self):
        '''自有评论每日只扫描一次；无候选后每个后续故事仍可兜底。'''
        from applications.zhihu_story import reply_task

        cards = [
            {'key': 'ad-1', 'author': '广告号', 'text': '加微信互关',
             'answer_url': 'https://example.test/a/1'},
        ]
        browser = _FakeCommentBrowser(
            manage_comments=cards,
            send_results=[
                {'ok': False, 'sent': False, 'detail': 'no-comment-entry'},
                {'ok': True, 'sent': True, 'detail': '已发送并确认'},
            ])
        st = ck.fresh_state()
        ctx = {'reader_check': True, 'comment': 'do', 'is_last': False,
               'follow': 'done', 'vote': 'done'}
        stories = [
            {'title': '故事一', 'text': '第一段细节很清楚。',
             'url': 'https://example.test/q/1'},
            {'title': '故事二', 'text': '第二段细节很清楚。',
             'url': 'https://example.test/q/2'},
            {'title': '故事三', 'text': '第三段细节很清楚。',
             'url': 'https://example.test/q/3'},
        ]
        fallback_calls = [0]

        def fallback_ask(*args, **kwargs):
            fallback_calls[0] += 1
            return '这段细节很清楚，节奏收得很稳，读起来很有画面。'

        for story in stories:
            one_ctx = dict(ctx, story=story)
            task.run_interaction(browser, story['url'], ctx=one_ctx,
                                 state=st, ask=fallback_ask)
            st = ck.load_state()
            # 每轮传入的是同一份逻辑上下文，真实流程依靠当日缓存避免重扫。
        self.assertEqual(browser.collect_manage_comments_calls, 1)
        self.assertEqual(len(browser.comments), 2,
                         '首个故事发送失败后，后续故事应重试一次')
        self.assertEqual(fallback_calls[0], 2)
        self.assertTrue(ck.load_state()['done']['comment'])
        sent_rows = [r for r in ck.load_replies()
                     if r.get('source') == 'checkin-fallback' and r.get('sent')]
        self.assertEqual(len(sent_rows), 1)
        self.assertIsInstance(ck.load_state()['reader_comments'].get('result'), dict)

    def test_successful_reader_reply_heals_and_fallback_does_not_send(self):
        '''自有故事评论已真实发送后，回到参考故事也不应再补一条评论。'''
        from applications.zhihu_story import reply_task

        browser = _FakeCommentBrowser()
        st = ck.fresh_state()
        story_url = 'https://example.test/q/reference'

        def reader_success(*args, **kwargs):
            ck.append_reply({'key': 'reader-1', 'reply': '已回复读者',
                             'sent': True, 'dry_run': False})
            return {'ok': True, 'units': 1, 'detail': '回复 1 条',
                    'replies': ['已回复读者'], 'dropped': []}

        with patch.object(reply_task, 'run_reply_job', side_effect=reader_success):
            r = task.run_interaction(
                browser, story_url,
                ctx={'reader_check': True, 'comment': 'do', 'is_last': False,
                     'follow': 'done', 'vote': 'done',
                     'story': {'title': '参考', 'text': '参考故事正文。',
                               'url': story_url}},
                state=st, ask=lambda *a, **k: '不应生成')
        self.assertEqual(browser.comments, [])
        self.assertIn(story_url, browser.opened)
        self.assertTrue(ck.load_state()['done']['comment'])
        self.assertNotIn('comment', r['handled'])

    def test_no_follow_vote_target_does_not_block_comment(self):
        '''关注/赞同没有可操作目标时，独立的评论兜底仍应执行。'''
        browser = _FakeCommentBrowser(targets=[])
        st = ck.fresh_state()
        r = task.run_interaction(
            browser, 'https://example.test/q/1',
            ctx={'follow': 'do', 'vote': 'do', 'comment': 'do',
                 'is_last': False,
                 'story': {'title': '题目', 'text': '这是一段足够具体的正文。',
                           'url': 'https://example.test/q/1'}},
            state=st,
            ask=lambda *a, **k: '这段具体细节很有画面，节奏也处理得很自然。')
        self.assertEqual(len(browser.comments), 1)
        self.assertIn('comment', r['handled'])
        self.assertTrue(st['done']['comment'])

    def test_checkin_without_reference_story_reports_comment_pending(self):
        '''晚间关注/赞同已完成但评论欠账、又没有参考故事时，结果必须失败。'''
        tasks = {
            'follow': {'title': '关注', 'done': True},
            'vote': {'title': '赞同', 'done': True},
            'comment': {'title': '评论', 'done': False},
        }
        browser = _FakeCommentBrowser(tasks=tasks)
        r = task.run_checkin_job(browser, url='https://example.test/checkin',
                                 is_last=True)
        self.assertFalse(r['ok'])
        self.assertIn('评论仍未达成', r['detail'])

    def test_checkin_uses_saved_story_to_complete_comment_and_summary(self):
        '''晚间巡检可使用已保存故事补评论，并将最终摘要记为完成。'''
        tasks = {
            'follow': {'title': '关注', 'done': True},
            'vote': {'title': '赞同', 'done': True},
            'comment': {'title': '评论', 'done': False},
        }
        browser = _FakeCommentBrowser(tasks=tasks)
        st = ck.fresh_state()
        st['reference_story'] = {
            'title': '已保存故事', 'text': '这是一段具体的已保存正文。',
            'url': 'https://example.test/q/saved'}
        ck.save_state(st)
        r = task.run_checkin_job(browser, url='https://example.test/checkin',
                                 is_last=True,
                                 ask=lambda *a, **k:
                                 '已保存故事中的细节很清楚，结尾的留白也很舒服。')
        self.assertTrue(r['ok'], r)
        self.assertTrue(r['summary']['ok'])
        self.assertTrue(ck.load_state()['done']['comment'])

    def test_dry_run_success_does_not_mark_comment_done(self):
        '''dry-run 生成成功但未发送，不能把评论打卡置为完成。'''
        browser = _FakeCommentBrowser()
        st = ck.fresh_state()
        r = task.maybe_comment_fallback(
            browser,
            ctx={'comment': 'do', 'story': {'title': '题',
                                            'text': '正文有具体细节。',
                                            'url': 'https://example.test/q/1'},
                 'comment_dry_run': True},
            state=st,
            ask=lambda *a, **k: '正文里的具体细节很有画面，节奏也很自然。')
        self.assertTrue(r['ok'])
        self.assertFalse(r['sent'])
        self.assertFalse(st['done']['comment'])
        self.assertEqual(len(ck.replied_keys()), 0)

    def test_sent_ledger_prevents_duplicate_when_state_lost(self):
        '''本地状态丢失时，真实发送台账仍应阻止重复评论。'''
        ck.append_reply({'key': '', 'reply': '以前已经发过', 'sent': True,
                         'dry_run': False, 'source': 'checkin-fallback'})
        browser = _FakeCommentBrowser()
        st = ck.fresh_state()
        r = task.maybe_comment_fallback(
            browser,
            ctx={'comment': 'do', 'story': {'title': '题',
                                            'text': '正文有具体细节。',
                                            'url': 'https://example.test/q/1'}},
            state=st,
            ask=lambda *a, **k: '新的评论不应生成')
        self.assertTrue(r['skipped'])
        self.assertEqual(browser.comments, [])
        self.assertTrue(st['done']['comment'])

    def test_answer_story_url_uses_direct_navigation(self):
        '''回答链接必须原样导航，不能退化成丢失 answer id 的问题页。'''
        answer_url = 'https://www.zhihu.com/question/1/answer/2'
        browser = _FakeCommentBrowser(answer_url='https://www.zhihu.com/question/1')
        st = ck.fresh_state()
        r = task.maybe_comment_fallback(
            browser,
            ctx={'comment': 'do', 'story': {'title': '题',
                                            'text': '正文包含具体细节。',
                                            'url': answer_url}},
            state=st,
            ask=lambda *a, **k: '正文的具体细节很有画面，节奏也很自然。')
        self.assertTrue(r['sent'], r)
        self.assertEqual(browser.page.gotos[0][0], answer_url)
        self.assertEqual(browser.opened, [])

    def test_answer_story_navigation_failure_does_not_send(self):
        browser = _FakeCommentBrowser(
            answer_url='https://www.zhihu.com/question/1', send_ok=True)
        browser.page.fail = True
        st = ck.fresh_state()
        answer_url = 'https://www.zhihu.com/question/1/answer/2'
        r = task.maybe_comment_fallback(
            browser,
            ctx={'comment': 'do', 'story': {'title': '题',
                                            'text': '正文包含具体细节。',
                                            'url': answer_url}},
            state=st,
            ask=lambda *a, **k: '导航失败时不应发送评论内容。')
        self.assertFalse(r['sent'])
        self.assertEqual(browser.comments, [])
        self.assertFalse(st['done']['comment'])

    def test_skips_when_comment_already_done(self):
        st = ck.fresh_state()
        ck.mark_done(st, 'comment', detail='今天已经评论过')
        r = task.maybe_comment_fallback(
            _FakeCommentBrowser(),
            ctx={'is_last': True, 'story': {'text': '正文',
                                            'url': 'https://x/question/1'}},
            state=st)
        self.assertTrue(r['skipped'])
        self.assertIn('已达成', r['detail'])

    def test_skips_without_story(self):
        r = task.maybe_comment_fallback(_FakeCommentBrowser(),
                                        ctx={'is_last': True},
                                        state=ck.fresh_state())
        self.assertTrue(r['skipped'])
        self.assertIn('参考故事', r['detail'])

    def test_sends_when_last_run_and_pending(self):
        '''末班 + 评论未达成 + 有故事 → 真的发送，并把打卡记账为完成。'''
        b = _FakeCommentBrowser()
        st = ck.fresh_state()
        r = task.maybe_comment_fallback(
            b, ctx={'is_last': True,
                    'story': {'title': '题', 'text': '他把伞收起来，水珠落在地砖上。',
                              'url': 'https://www.zhihu.com/question/1'}},
            state=st,
            ask=lambda *a, **k: '那把伞收起来的细节写得好，水珠那段一下就有画面了')
        self.assertTrue(r['sent'], r)
        self.assertEqual(len(b.comments), 1)                 # 只发一条
        self.assertTrue(st['done']['comment'])               # 打卡记账完成
        rows = [x for x in ck.load_replies() if x.get('source') == 'checkin-fallback']
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].get('sent'))

    def test_does_not_send_when_generated_text_fails_check(self):
        '''生成不达标 → 宁可不评论，也不留一条废评论。'''
        b = _FakeCommentBrowser()
        st = ck.fresh_state()
        r = task.maybe_comment_fallback(
            b, ctx={'is_last': True,
                    'story': {'text': '正文',
                              'url': 'https://www.zhihu.com/question/1'}},
            state=st, ask=lambda *a, **k: '写得好')
        self.assertFalse(r['sent'])
        self.assertEqual(b.comments, [])                     # 一条都没发
        self.assertFalse(st['done']['comment'])

    def test_send_failure_is_recorded_not_marked_done(self):
        b = _FakeCommentBrowser(send_ok=False)
        st = ck.fresh_state()
        r = task.maybe_comment_fallback(
            b, ctx={'is_last': True,
                    'story': {'text': '正文',
                              'url': 'https://www.zhihu.com/question/1'}},
            state=st,
            ask=lambda *a, **k: '那把伞收起来的细节写得好，水珠那段一下就有画面了')
        self.assertFalse(r['sent'])
        self.assertFalse(st['done']['comment'])
        self.assertTrue(st['tried'])                         # 记「试过」便于排查

    def test_no_duplicate_when_today_has_same_text(self):
        '''今天已经发过一模一样的评论 → 不再重复发（去重）。'''
        same = '那把伞收起来的细节写得好，水珠那段一下就有画面了'
        ck.append_reply({'reply': same, 'sent': True, 'source': 'reader'})
        b = _FakeCommentBrowser()
        st = ck.fresh_state()
        r = task.maybe_comment_fallback(
            b, ctx={'is_last': True,
                    'story': {'text': '正文',
                              'url': 'https://www.zhihu.com/question/1'}},
            state=st, ask=lambda *a, **k: same, now=None)
        # 生成的都是同一句 → 校验判「太像」→ 不发
        self.assertFalse(r['sent'], r)


class CheckinLedgerHealTest(unittest.TestCase):
    '''评论发出去了，打卡「发布评论」必须显示达成。

    用户反馈（2026-10-01）：评论发出去了、打卡其实已经完成，界面却一直显示
    「发布评论✗」。根因有二：
      ① 回复评论成功后**本地从没记账**，✓ 完全依赖读打卡页；
      ② 打卡页有自己的统计延迟，刚发完就读往往还是「去评论」，而旧的
         update_tasks 会用页面数据**覆盖**掉本地认知。
    修法：回复成功即记账 + 台账追溯补记 + update_tasks 只升不降。
    '''

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='aq_heal_')
        self._orig_root = paths.DATA_ROOT
        paths.DATA_ROOT = self.tmp

    def tearDown(self):
        paths.DATA_ROOT = self._orig_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_heal_marks_comment_done_from_sent_reply(self):
        ck.append_reply({'reply': '说得对，我改。', 'sent': True})
        st = ck.fresh_state()
        self.assertTrue(ck.needs(st, 'comment'))
        healed = ck.heal_from_ledger(st)
        self.assertEqual(healed, ['comment'])
        self.assertFalse(ck.needs(st, 'comment'))
        self.assertIn('发布评论✓', ck.summary(st)['line'])

    def test_heal_ignores_dry_run_replies(self):
        '''演练（dry_run）没真的发出去，绝不能算达成。'''
        ck.append_reply({'reply': '演练内容', 'sent': True, 'dry_run': True})
        st = ck.fresh_state()
        self.assertEqual(ck.heal_from_ledger(st), [])
        self.assertTrue(ck.needs(st, 'comment'))

    def test_heal_ignores_unsent_replies(self):
        ck.append_reply({'reply': '没发出去', 'sent': False})
        st = ck.fresh_state()
        self.assertEqual(ck.heal_from_ledger(st), [])

    def test_heal_is_idempotent(self):
        ck.append_reply({'reply': '发出去了', 'sent': True})
        st = ck.fresh_state()
        self.assertEqual(ck.heal_from_ledger(st), ['comment'])
        self.assertEqual(ck.heal_from_ledger(st), [])   # 第二次不重复补

    def test_page_read_never_downgrades_local_done(self):
        '''打卡页统计滞后（还显示「去评论」）不能抹掉本地达成。'''
        st = ck.fresh_state()
        ck.mark_done(st, 'comment', detail='回复读者评论 1 条')
        stale_page = {'comment': {'title': '发布 1 条评论',
                                  'action': '去评论', 'done': False}}
        ck.update_tasks(st, stale_page)
        self.assertFalse(ck.needs(st, 'comment'))
        self.assertIn('发布评论✓', ck.summary(st)['line'])
        self.assertTrue(st['tasks']['comment'].get('stale_page'))

    def test_page_read_can_still_upgrade_to_done(self):
        st = ck.fresh_state()
        ck.update_tasks(st, {'comment': {'done': False}})
        self.assertTrue(ck.needs(st, 'comment'))
        ck.update_tasks(st, {'comment': {'done': True}})
        self.assertFalse(ck.needs(st, 'comment'))

    def test_reply_task_marks_comment_done_on_success(self):
        '''源码级契约：回复成功后必须记账（防回归到「从不记账」）。'''
        import inspect
        from applications.zhihu_story import reply_task
        src = inspect.getsource(reply_task)
        self.assertIn('checkin.mark_done', src)
        self.assertIn("'comment'", src)


if __name__ == '__main__':
    unittest.main()

