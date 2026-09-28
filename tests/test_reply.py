# -*- coding: utf-8 -*-
# ============================================================
# tests/test_reply.py — 评论回复：提示词组装与本地硬校验（纯逻辑）
#
# 运行：.venv/Scripts/python -m unittest tests.test_reply -v
# ============================================================

import unittest

from applications.zhihu_story import reply_prompts as rp
from applications.zhihu_story import reply_task as rt


class TestLengthTarget(unittest.TestCase):
    '''长度跟着对方走（用户口径：对方越短你越短，但平台下限 10 字）。'''

    def test_very_short_comment(self):
        self.assertEqual(rp.length_target('看不下去'), (10, 16))
        self.assertEqual(rp.length_target('蹲'), (10, 16))

    def test_medium_comment(self):
        self.assertEqual(rp.length_target('写得挺好的，我很喜欢这种女主'), (10, 22))

    def test_long_comment(self):
        long_one = '全篇看完我都不知道他想表达什么，作者你自己真的能看得懂吗？' * 2
        lo, hi = rp.length_target(long_one)
        self.assertEqual((lo, hi), (30, 64))

    def test_empty_comment_floor(self):
        self.assertEqual(rp.length_target(''), (10, 16))


class TestPickPrompt(unittest.TestCase):
    def test_numbers_comments_and_hides_username(self):
        p = rp.build_pick_prompt([
            {'author': '张三', 'text': '写得真好，看哭了'}, '谢谢作者',
            {'author': '李四', 'text': '看我主页'}])
        self.assertIn('1. 写得真好，看哭了', p)
        self.assertIn('2. 谢谢作者', p)
        self.assertIn('3. 看我主页', p)
        self.assertNotIn('张三', p)
        self.assertNotIn('李四', p)

    def test_skips_empty(self):
        p = rp.build_pick_prompt([{'text': '  '}, '好看'])
        self.assertIn('1. 好看', p)
        self.assertNotIn('2.', p)

    def test_asks_for_one_number(self):
        self.assertIn('只输出那一条的编号', rp.PICK_PROMPT)
        self.assertIn('只输出 0', rp.PICK_PROMPT)


class TestReplyPrompt(unittest.TestCase):
    def test_contains_length_hint_and_min(self):
        p = rp.build_reply_prompt('看不下去', '题目名', '正文' * 10)
        self.assertIn('目标 10–16 字', p)
        self.assertIn('最少 10 字', p)
        self.assertIn('看不下去', p)
        self.assertIn('题目名', p)

    def test_truncates_long_answer(self):
        p = rp.build_reply_prompt('好看', '题目', '长' * 5000)
        self.assertIn('后略', p)
        self.assertLess(len(p), 1200)

    def test_forbids_username_and_ai_tone(self):
        self.assertIn('不要提对方的名字', rp.REPLY_PROMPT_TMPL)
        self.assertIn('不要说人话' if False else '说人话', rp.REPLY_PROMPT_TMPL)
        self.assertIn('感谢您的认可', rp.REPLY_PROMPT_TMPL)


class TestCheckReply(unittest.TestCase):
    def test_good_reply_passes(self):
        self.assertEqual(rp.check_reply('谢谢您看完，我下次写细一点。', '看不下去'), [])
        self.assertEqual(rp.check_reply('谢谢喜欢，我继续写。', '好看'), [])

    def test_too_short_flagged(self):
        issues = rp.check_reply('谢谢', '好看')
        self.assertTrue(any('太短' in x for x in issues), issues)

    def test_too_long_flagged(self):
        issues = rp.check_reply('谢' * 80, '好看')
        self.assertTrue(any('偏长' in x for x in issues), issues)

    def test_cliche_flagged(self):
        issues = rp.check_reply('感谢您的认可，希望对你有帮助。', '写得真好')
        self.assertTrue(any('套话' in x for x in issues), issues)

    def test_confrontational_flagged(self):
        issues = rp.check_reply('你懂吗，我这写得挺好的。', '看不下去')
        self.assertTrue(any('语气硬' in x for x in issues), issues)

    def test_username_flagged(self):
        issues = rp.check_reply('谢谢张三的喜欢，我继续写。', '好看', author='张三')
        self.assertTrue(any('用户名' in x for x in issues), issues)

    def test_emoji_text_flagged(self):
        issues = rp.check_reply('谢谢您[微笑]我继续努力写。', '好看')
        self.assertTrue(any('表情包' in x for x in issues), issues)

    def test_empty_flagged(self):
        self.assertEqual(rp.check_reply('', '好看'), ['回复为空'])
        self.assertEqual(rp.check_reply('   ', '好看'), ['回复为空'])

    def test_rewrite_feedback(self):
        self.assertEqual(rp.rewrite_feedback([]), '')
        fb = rp.rewrite_feedback(['太短（2 字 < 平台下限 10 字）'])
        self.assertIn('请重写', fb)
        self.assertIn('只输出回复正文', fb)


class TestStripWrapping(unittest.TestCase):
    def test_strips_quotes_and_markdown(self):
        self.assertEqual(rp.strip_wrapping('「谢谢您看完。」'), '谢谢您看完。')
        self.assertEqual(rp.strip_wrapping('**谢谢您看完**'), '谢谢您看完')
        self.assertEqual(rp.strip_wrapping('  谢谢您看完。 '), '谢谢您看完。')

    def test_keeps_inner_content(self):
        self.assertEqual(rp.strip_wrapping('谢谢您，「好看」我记下了。'),
                         '谢谢您，「好看」我记下了。')


class TestComposeReplyEmptyDriverOutput(unittest.TestCase):
    '''网页版驱动偶发读回空内容：要另起一轮重试，而不是白扔一次回复机会。

    2026-09-28 真机日志：「文本稳定 2 轮，判定完成（15.0s，1 字符）」——
    只读到思考态占位符，reply 为空。带原因重写没用（模型没收到反馈），
    必须重新提问。
    '''

    COMMENT = '写得真好，蹲后续'

    def _compose(self, outputs):
        calls = []

        def _ask(prompt, driver=None, reuse_session=True):
            calls.append({'prompt': prompt, 'reuse': reuse_session})
            return outputs[min(len(calls) - 1, len(outputs) - 1)]

        orig = rt.ask_llm
        rt.ask_llm = _ask
        try:
            r = rt.compose_reply(None, self.COMMENT, '题', '答')
        finally:
            rt.ask_llm = orig
        return r, calls

    def test_empty_first_answer_is_retried(self):
        r, calls = self._compose(['', '行，我尽量把这条线写清。'])
        self.assertTrue(r['ok'])
        self.assertEqual(len(calls), 2)          # 空答案触发了一次重试
        self.assertFalse(calls[0]['reuse'])      # 第一次是新会话
        self.assertFalse(calls[1]['reuse'])      # 重试也另起一轮（不复用空会话）

    def test_empty_twice_gives_up_without_crashing(self):
        r, calls = self._compose(['', ''])
        self.assertFalse(r['ok'])
        # 空内容也占重试预算：最多 (max_retry+1)+empty_retry = 4 次，绝不无限重试
        self.assertEqual(len(calls), 4)
        self.assertIn('空', '；'.join(r['issues']))
        self.assertTrue(all(not c['reuse'] for c in calls))   # 每轮都另起会话

    def test_short_but_nonempty_goes_down_the_rewrite_path(self):
        """有内容但不达标 → 走「带原因重写」（复用会话），不是空内容重试。"""
        r, calls = self._compose(['太短', '行，我尽量把这条线写清。'])
        self.assertTrue(r['ok'])
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1]['reuse'])       # 重写复用同一会话


class TestPrefilter(unittest.TestCase):
    '''规则预筛（用户口径：引流、戾气、纯表情、已回过的直接踢掉）。'''

    def card(self, text, **kw):
        c = {'text': text, 'author': '某人', 'key': 'k-' + str(abs(hash(text)) % 9999),
             'answer_url': 'https://www.zhihu.com/answer/1', 'index': 0}
        c.update(kw)
        return c

    def test_keeps_friendly_and_drops_hostile(self):
        cards = [self.card('写得真好看，看哭了'), self.card('贱出天际'),
                 self.card('看我主页'), self.card('')]
        got = rt.prefilter(cards)
        texts = [c['text'] for c in got['candidates']]
        self.assertEqual(texts, ['写得真好看，看哭了'])
        reasons = sorted(d['reason'].split(':')[0] for d in got['dropped'])
        self.assertEqual(reasons, ['hostile', 'no-content', 'spam'])

    def test_drops_already_replied(self):
        c = self.card('谢谢作者')
        got = rt.prefilter([c], replied_keys={c['key']})
        self.assertEqual(got['candidates'], [])
        self.assertEqual(got['dropped'][0]['reason'], 'replied')

    def test_drops_without_answer_url(self):
        got = rt.prefilter([self.card('写得真好', answer_url='')])
        self.assertEqual(got['candidates'], [])
        self.assertEqual(got['dropped'][0]['reason'], 'no-answer-url')

    def test_sorted_by_friendliness_score(self):
        # 单字评论（<2 字）按「没内容」被踢掉，这里用两字中性评论做对照
        cards = [self.card('还行'), self.card('写得真好看，我很喜欢这种女主')]
        got = rt.prefilter(cards)
        self.assertEqual(got['candidates'][0]['text'], '写得真好看，我很喜欢这种女主')
        self.assertGreaterEqual(got['candidates'][0]['score'],
                                got['candidates'][1]['score'])

    def test_emoji_only_dropped(self):
        got = rt.prefilter([self.card('[蹲][哇]'), self.card('。。。')])
        self.assertEqual(got['candidates'], [])

    def test_max_candidates_cap(self):
        cards = [self.card('写得真好第%d条，我很喜欢' % i) for i in range(30)]
        got = rt.prefilter(cards, max_candidates=5)
        self.assertEqual(len(got['candidates']), 5)


class TestPickParsing(unittest.TestCase):
    def test_plain_number(self):
        self.assertEqual(rt.parse_pick('3', 5), 3)
        self.assertEqual(rt.parse_pick('  2。', 5), 2)
        self.assertEqual(rt.parse_pick('编号：4', 5), 4)

    def test_zero_means_no_pick(self):
        self.assertEqual(rt.parse_pick('0', 5), 0)

    def test_out_of_range_or_garbage(self):
        self.assertEqual(rt.parse_pick('9', 5), 0)
        self.assertEqual(rt.parse_pick('都不太好', 5), 0)
        self.assertEqual(rt.parse_pick('', 5), 0)
        self.assertEqual(rt.parse_pick(None, 5), 0)

    def test_pick_from_candidates_matches_prompt_numbering(self):
        cands = [{'text': '  写得真好  '}, {'text': ''}, {'text': '谢谢作者'}]
        # 提示词只给非空评论编号：1=写得真好，2=谢谢作者
        got = rt.pick_from_candidates(cands, '2')
        self.assertEqual(got['text'], '谢谢作者')
        self.assertIsNone(rt.pick_from_candidates(cands, '3'))
        self.assertIsNone(rt.pick_from_candidates(cands, '0'))


class TestFriendlinessScore(unittest.TestCase):
    def test_positive_hint_scores_higher(self):
        self.assertGreater(rt.friendliness_score('写得真好看'),
                           rt.friendliness_score('嗯'))

    def test_question_scores(self):
        self.assertGreater(rt.friendliness_score('女主后来怎么样了？'),
                           rt.friendliness_score('女主后来走了'))

    def test_very_long_penalised(self):
        self.assertLess(rt.friendliness_score('长' * 200),
                        rt.friendliness_score('长' * 30))


if __name__ == '__main__':
    unittest.main()
