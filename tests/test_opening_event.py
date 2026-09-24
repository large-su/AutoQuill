# -*- coding: utf-8 -*-
"""开篇事件化回归（2026-09-19 用户口径）。

背景：用户反馈"打开第一页，每个字我都认识，但完全抓不住重点，也完全入不了戏；
前五六句既没有情节推进，也没有冲突，单纯在描述环境或情绪"。
回看产物证实：经典模式的引言被写成了"全书简介"——所有人都说…／没人知道…／
只有我清楚…／这场…／可惜晚了——用评价句把人设、身世、牺牲、结局一次讲完。

实测（2026-09-19 批次 19 篇 vs 采集参考 35 篇，都只看引言前 10 句）：
  引言含对话比例 16% vs 100%；抽象评价句占比 0.30 vs 0.00；
  具体名词密度 0.72 vs 2.40（每百字）。

本文件守住四件事：
  1) 检测器认得"总结体开头"，且对采集参考零误报；
  2) 格式校验把总结体开头当否决项（details 带「开篇」）；
  3) prompt 里注入了「开篇事件化守则」，起手式指令不再放行"只有判断没有事件"；
  4) 检测器与提示词小节在 core.detectors 注册表里成对出现。

运行：python -m unittest discover -s tests -v
"""

import json
import os
import unittest

from core.detectors import (
    DETECTORS, PROMPT_RULE_MAP, check_summary_opening,
)
from core.story_text import validate_story_format
import story_prompt as sp

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
COLLECTED = os.path.join(ROOT, "data", "collected_stories.jsonl")

# 采集参考的真实开头（学习目标：微型场景 + 对话）
REF_OPENINGS = [
    "儿子被请家长，班主任是前男友。\n\n他扶了扶眼镜，「侄子。」\n\n"
    "我喝了一口茶，「儿子。」\n\n「冯卿卿，你23，你儿子16，你当我傻子？」\n\n"
    "我皮笑肉不笑，「我老公53，儿子16有什么问题？」",
    "拉黑我的男神突然把我从黑名单里移出来了。\n\n发的第一条消息就是，"
    "「缺个新娘来吗。」\n\n我一秒回复，「来。」",
    "我妈怀了富豪的种。\n\n想带着我嫁进豪门，却被人轰出来。\n\n谈判桌上，\n\n"
    "「一张怀孕报告能说明什么？谁知道你去哪里怀的野种？」\n\n"
    "我妈气的脸色惨白，不敢出声。\n\n我缓缓从包里拿出一张纸，摆好，"
    "「一张不够，那两张呢？」",
    "我在菜市场卖白菜。\n\n我爹突然让我回家继承一个亿和一个瞎子未婚夫。\n\n"
    "「给你一个亿替我女儿嫁给吴氏集团的瞎子儿子，你愿意吗？」\n\n"
    "我桌子一拍，「你在侮辱我。」",
]

# 我方真实产物（2026-09-19 批次，用户点名的"看不懂"开头）
OUR_BAD_OPENINGS = [
    "我不会安慰人，不会心软，更不会因为任何人的付出而愧疚动容。\n\n"
    "所有人都说我冷血寡情，是块捂不热的寒冰。\n\n我从不否认。\n\n"
    "情绪是最没用的累赘，浪费时间，消耗精力。",
    "我彻底消失的那天，他在全城媒体面前，官宣了和别人的终身婚约。\n\n"
    "没人知道，我不是赌气离开，是刚替他顶下了牢狱之灾，赔光了所有身家。\n\n"
    "所有人都骂我偏执纠缠、贪慕虚荣。\n\n只有我清楚，我攒了三年的委屈"
    "从来都换不来他半分偏爱。",
    "我替他挡下致命一剑时，他正抱着白月光。\n\n所有人都在夸他情深义重。\n\n"
    "没人看我一眼。\n\n到头来，我只是他情深戏码里最多余的背景板。\n\n"
    "可惜，晚了。这场轰轰烈烈的火葬场，从始至终都没有圆满结局。",
]

# 我方真实产物（同样第一人称、同样篇幅，但是场景 + 对话）
OUR_GOOD_OPENING = (
    "我妈让我捐肾。\n\n饭桌上，她把配型报告推到我面前。\n\n"
    "「小满，救救你弟。」\n\n我拿起报告。\n\n上面写着，配型成功。\n\n"
    "我放下筷子。\n\n我说：「不捐。」\n\n全家安静了。"
)


def make_body(intro, chapters=8, paras_per_chapter=15):
    """引言 + N 章填充正文（每段 42 字，整体 4000 字以上，段落不超阈值）。"""
    para = "这是正文内容。" * 7
    blocks = ["## **%d**" % i + "\n\n" + (para + "\n\n") * paras_per_chapter
              for i in range(1, chapters + 1)]
    return intro + "\n\n" + "\n\n".join(blocks)


class SummaryOpeningDetectorTest(unittest.TestCase):
    def test_reference_openings_never_flagged(self):
        for text in REF_OPENINGS:
            r = check_summary_opening(text)
            self.assertFalse(r["flagged"], text[:30] + " -> " + r["reason"])

    def test_collected_corpus_zero_false_positive(self):
        """采集库全量零误报（检测器进否决项的前提）。"""
        if not os.path.exists(COLLECTED):
            self.skipTest("采集库不存在（CI 精简环境）")
        n = 0
        with open(COLLECTED, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                answer = row.get("answer") or ""
                if len(answer) < 200:
                    continue
                n += 1
                r = check_summary_opening(answer)
                self.assertFalse(r["flagged"],
                                 "误报：" + str(row.get("title")) + " " + r["reason"])
        self.assertGreater(n, 20)

    def test_our_real_bad_openings_flagged(self):
        for text in OUR_BAD_OPENINGS:
            r = check_summary_opening(text)
            self.assertTrue(r["flagged"], text[:30])
            self.assertEqual(r["quote_paras"], 0)
            self.assertTrue(r["reason"])

    def test_scene_opening_with_dialogue_passes(self):
        r = check_summary_opening(OUR_GOOD_OPENING)
        self.assertFalse(r["flagged"], r["reason"])
        self.assertGreaterEqual(r["quote_paras"], 1)

    def test_concrete_scene_without_dialogue_passes(self):
        text = ("我撬开丈夫的抽屉，看到一张照片。\n\n照片里，一个穿蓝白校服的女生"
                "蹲在地上，给一只流浪猫喂食。\n\n那个校徽我认得，是我的大学。\n\n"
                "照片背面写着一行字：「找了你十年。」\n\n笔迹是我丈夫的。")
        r = check_summary_opening(text)
        self.assertFalse(r["flagged"], r["reason"])

    def test_short_intro_not_judged(self):
        r = check_summary_opening("我死了。")
        self.assertFalse(r["flagged"])

    def test_empty_text_safe(self):
        for t in ("", None, "   "):
            r = check_summary_opening(t)
            self.assertFalse(r["flagged"])


class ValidateStoryFormatGateTest(unittest.TestCase):
    def test_summary_opening_story_is_invalid(self):
        body = make_body(OUR_BAD_OPENINGS[0])
        self.assertGreaterEqual(len(body), 4000)
        score, valid, details = validate_story_format(body)
        self.assertIn("开篇", details)
        self.assertFalse(valid, details)

    def test_scene_opening_story_is_valid(self):
        body = make_body(OUR_GOOD_OPENING)
        score, valid, details = validate_story_format(body)
        self.assertNotIn("开篇", details)
        self.assertTrue(valid, details)

    def test_validator_deduction_documented(self):
        doc = validate_story_format.__doc__ or ""
        self.assertIn("开篇", doc)


class OpeningEventPromptTest(unittest.TestCase):
    def test_rule_injected_into_classic_prompt(self):
        msg, _ = sp.build_story_prompt("测试问题：有什么好看的甜文推荐？")
        self.assertIn("开篇事件化守则", msg)
        for kw in ("正在发生的事", "微型场景", "至少要有 1 句带引号的对话",
                   "总结体开头", "自检"):
            self.assertIn(kw, msg, kw)

    def test_self_check_rule_carries_opening_event(self):
        for kw in ("开篇内容", "带引号的对话", "评价句开场"):
            self.assertIn(kw, sp.FORMAT_SELF_CHECK_RULE, kw)

    def test_opening_style_instruction_keeps_event_floor(self):
        for style in sp.OPENING_STYLES:
            text = sp.render_opening_instruction(style)
            self.assertIn("带引号的对话", text, style[0])
            self.assertIn("看得见的动作", text, style[0])

    def test_judgment_style_requires_event_in_second_sentence(self):
        styles = dict((s[0], s[1]) for s in sp.OPENING_STYLES)
        req = styles["反差断语起手"]
        self.assertIn("第二句", req)
        self.assertIn("只有判断没有事件", req)

    def test_event_statement_reference_not_misread_as_judgment(self):
        # 2026-09-19：参考首句是"情境陈述"（不是「我」起手、也没有物件量词）时，
        # 早先会被误判成「判断/反差断语起手」→ prompt 要求模型"先给一句反常识的
        # 判断"——正是总结体开头的来源之一。这里单列一类并要求它落地成事件。
        info = sp.analyze_reference_opening("儿子被请家长，班主任是前男友。")
        self.assertEqual(info["key"], "event_statement")
        self.assertIn("具体情境", info["technique"])
        self.assertIn("看得见的动作", info["technique"])

    def test_aphorism_reference_still_judgment_with_event_floor(self):
        info = sp.analyze_reference_opening(
            "这世上最狠的报复，是替一个人把烂摊子全背下来。")
        self.assertEqual(info["key"], "judgment")
        text = sp.render_reference_opening_instruction(info)
        self.assertIn("看得见的动作", text)

    def test_disposal_particle_not_read_as_object(self):
        # "把/件/条"兼作介词与量词：量词前没有指示/数量词就不算物件起手
        info = sp.analyze_reference_opening("他把我推下楼的时候，脸上没有表情。")
        self.assertEqual(info["key"], "other_person")
        info2 = sp.analyze_reference_opening("那份病历上写着他的名字，我捏了很久。")
        self.assertEqual(info2["key"], "object")

    def test_reference_opening_instruction_keeps_event_floor(self):
        info = sp.analyze_reference_opening(
            "儿子被请家长，班主任是前男友。\n\n他扶了扶眼镜，「侄子。」")
        text = sp.render_reference_opening_instruction(info)
        self.assertIn("带引号的对话", text)
        self.assertIn("看得见的动作", text)

    def test_retry_feedback_mentions_summary_opening(self):
        text = sp._render_retry_feedback(["开篇：总结体开场(-2)"])
        self.assertIn("不是故事简介", text)
        self.assertIn("带引号的对话", text)


class DetectorRegistryTest(unittest.TestCase):
    def test_detector_registered(self):
        self.assertIs(DETECTORS.get("summary_opening"), check_summary_opening)

    def test_prompt_rule_mapped(self):
        self.assertIn("summary_opening", PROMPT_RULE_MAP.get("开篇事件化守则", []))

    def test_every_prompt_rule_maps_to_known_detector(self):
        for rule, dets in PROMPT_RULE_MAP.items():
            for d in dets:
                self.assertIn(d, DETECTORS, rule + " -> " + d)


if __name__ == "__main__":
    unittest.main()
