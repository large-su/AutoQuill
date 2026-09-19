# ============================================================
# tests/test_opening_variety.py — 开篇起手方式回归
#
# 背景（2026-09-19）：经典模式最近 22 篇里 20 篇以「我」字开头（最近 20 篇 100%）
# 且高度同构。第一版修复用「固定轮换」指定起手式；用户口径修正为——**优先模仿
# 本题最受认可那篇参考文章的起手方式**（学手法、不抄句子），轮换只在没有参考时兜底。
# 本文件守住三件事：参考起手识别、注入策略（参考优先/轮换兜底/显式覆盖）、
# 以及「模仿不越界」的抄袭红线检测。
#
# 运行：python -m unittest discover -s tests -v
# ============================================================

import threading
import unittest

import story_prompt as sp
from core.originality import opening_copy_signals


class TestOpeningStyleRotation(unittest.TestCase):
    def setUp(self):
        sp.reset_opening_cursor()

    def test_rotation_covers_all_styles_then_wraps(self):
        got = [sp.next_opening_style()[0]
               for _ in range(len(sp.OPENING_STYLES))]
        self.assertEqual(got, [s[0] for s in sp.OPENING_STYLES])
        self.assertEqual(sp.next_opening_style()[0], sp.OPENING_STYLES[0][0])

    def test_rotation_is_thread_safe(self):
        sp.reset_opening_cursor()
        out = []
        lock = threading.Lock()

        def worker():
            for _ in range(10):
                label = sp.next_opening_style()[0]
                with lock:
                    out.append(label)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(out), 40)
        per = 40 // len(sp.OPENING_STYLES)
        for label, _ in sp.OPENING_STYLES:
            self.assertEqual(out.count(label), per, label)

    def test_styles_all_require_person_or_dialogue(self):
        self.assertIn("第一段内都要出现人", sp.OPENING_VARIETY_RULE)
        for label, req in sp.OPENING_STYLES:
            self.assertTrue("人" in req or "对话" in req or "我" in req, label)

    def test_render_instruction_carries_style_and_format_floor(self):
        style = sp.OPENING_STYLES[1]
        text = sp.render_opening_instruction(style)
        self.assertIn(style[0], text)
        self.assertIn(style[1][:12], text)
        self.assertIn("第一句必须是故事正文", text)
        self.assertIn("60-300 字", text)
        self.assertEqual(sp.render_opening_instruction(None), "")


class TestReferenceOpeningAnalysis(unittest.TestCase):
    def test_first_person_reference_flagged_as_cliche_template(self):
        info = sp.analyze_reference_opening("我撬开丈夫的抽屉，看到一张照片。")
        self.assertEqual(info["key"], "first_person")
        self.assertTrue(info["cliche_tail"])   # 提醒换句式，别把同构学回来
        self.assertIn("第一人称", info["label"])

    def test_other_styles_recognised(self):
        cases = {
            "「你要是敢走，我就把这房子点了。」她说这话时没抬头。": "dialogue",
            "她把离婚协议推过来的时候，手指在抖。": "other_person",
            "第十七年，他还没喝过我送的那杯咖啡。": "time_number",
            "那份病历上写着他的名字，我捏了很久。": "object",
        }
        for text, key in cases.items():
            info = sp.analyze_reference_opening(text)
            self.assertEqual(info["key"], key, text)

    def test_missing_reference_returns_none(self):
        self.assertIsNone(sp.analyze_reference_opening(""))
        self.assertIsNone(sp.analyze_reference_opening(None))
        self.assertIsNone(sp.analyze_reference_opening("   \n  "))

    def test_reference_instruction_warns_about_copying(self):
        info = sp.analyze_reference_opening("她把协议推过来的时候，手指在抖。")
        text = sp.render_reference_opening_instruction(info)
        self.assertIn("模仿参考文章的手法", text)
        self.assertIn("抄袭红线", text)
        self.assertIn("10 字以上连续重合", text)
        self.assertIn(info["sample"][:10], text)


class TestPromptInjectionPolicy(unittest.TestCase):
    REF = "她把离婚协议推过来的时候，手指在抖。那天我们结婚刚满三年。" * 6

    def setUp(self):
        sp.reset_opening_cursor()

    def test_reference_first_by_default(self):
        prompt, mode = sp.build_story_prompt("怎么把虐文写到极致？", self.REF)
        self.assertIn("## 本篇开篇起手方式", prompt)
        self.assertIn("参考起手:", mode)
        # 走参考时不再按固定循环指定
        self.assertNotIn("## 本篇指定的开篇起手式", prompt)

    def test_rotation_only_when_no_reference(self):
        prompt, mode = sp.build_story_prompt("题", None)
        self.assertIn("## 本篇指定的开篇起手式", prompt)
        self.assertIn(sp.OPENING_STYLES[0][0], mode)
        _, mode2 = sp.build_story_prompt("题", None)
        self.assertIn(sp.OPENING_STYLES[1][0], mode2)

    def test_mirror_off_falls_back_to_rotation(self):
        import config.story as cs
        saved = cs.OPENING_MIRROR_REFERENCE
        try:
            cs.OPENING_MIRROR_REFERENCE = False
            prompt, mode = sp.build_story_prompt("题", self.REF)
            self.assertIn("## 本篇指定的开篇起手式", prompt)
            self.assertIn(sp.OPENING_STYLES[0][0], mode)
        finally:
            cs.OPENING_MIRROR_REFERENCE = saved

    def test_explicit_style_wins(self):
        prompt, mode = sp.build_story_prompt(
            "题", self.REF, opening_style=sp.OPENING_STYLES[2])
        self.assertIn(sp.OPENING_STYLES[2][0], prompt)
        self.assertIn(sp.OPENING_STYLES[2][0], mode)
        self.assertNotIn("## 本篇开篇起手方式", prompt)

    def test_all_switches_off_injects_nothing(self):
        import config.story as cs
        saved = cs.OPENING_VARIETY
        try:
            cs.OPENING_VARIETY = False
            p, _ = sp.build_story_prompt("题", None)
            self.assertNotIn("## 本篇指定的开篇起手式", p)
            p2, _ = sp.build_story_prompt("题", self.REF, opening_auto=False)
            self.assertNotIn("## 本篇开篇起手方式", p2)
        finally:
            cs.OPENING_VARIETY = saved

    def test_clean_mode_prompt_untouched(self):
        prompt, _ = sp.build_clean_prompt("题", self.REF)
        self.assertNotIn("## 本篇开篇起手方式", prompt)
        self.assertNotIn("开篇起手式多样化", prompt)


class TestOpeningCopyGuard(unittest.TestCase):
    REF = "她把离婚协议推过来的时候，手指在抖。我说我不签，她说随你。"

    def test_rewritten_opening_flagged(self):
        story = "她把离婚协议推过来的时候，手指没抖。" + "后面是正文。" * 60
        sig = opening_copy_signals(story, self.REF)
        self.assertTrue(sig["risky"])
        self.assertGreaterEqual(sig["run"], 12)
        self.assertTrue(sig["snippet"])

    def test_different_opening_not_flagged(self):
        story = "第十七年，他还没喝过我送的那杯咖啡。" + "后面是正文。" * 60
        sig = opening_copy_signals(story, self.REF)
        self.assertFalse(sig["risky"], sig)

    def test_empty_inputs_safe(self):
        self.assertFalse(opening_copy_signals("", self.REF)["risky"])
        self.assertFalse(opening_copy_signals("正文。" * 80, "")["risky"])

    def test_threshold_is_configurable(self):
        story = "她把离婚协议推过来的时候。" + "正文。" * 80
        self.assertTrue(opening_copy_signals(story, self.REF, min_run=5)["risky"])
        self.assertFalse(opening_copy_signals(story, self.REF, min_run=40)["risky"])


if __name__ == "__main__":
    unittest.main()
