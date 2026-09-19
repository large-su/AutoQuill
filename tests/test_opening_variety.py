# ============================================================
# tests/test_opening_variety.py — 开篇起手式多样化回归
#
# 2026-09-19：真实产物统计——经典模式最近 22 篇里 20 篇以「我」字开头（最近 20 篇
# 100%）且高度同构（我+强动作/物件/数字）。根因是守则叠加（第一人称声口 + 引言
# 一票否决 + 禁空镜开场 + 首句进动作）把开头挤成了唯一解，而不是模型能力问题。
# 本文件守住修复：按篇轮换指定起手式 + 守则自带「不许默认我起手」的硬条款。
#
# 运行：python -m unittest discover -s tests -v
# ============================================================

import threading
import unittest

import story_prompt as sp


class TestOpeningStyleRotation(unittest.TestCase):
    def setUp(self):
        sp.reset_opening_cursor()

    def test_rotation_covers_all_styles_then_wraps(self):
        got = [sp.next_opening_style()[0]
               for _ in range(len(sp.OPENING_STYLES))]
        self.assertEqual(got, [s[0] for s in sp.OPENING_STYLES])
        # 到末尾回环
        self.assertEqual(sp.next_opening_style()[0], sp.OPENING_STYLES[0][0])

    def test_rotation_is_thread_safe(self):
        # 并行批量生成时多线程同时取号，不能重复也不能漏
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

    def test_render_instruction_carries_style_and_format_floor(self):
        style = sp.OPENING_STYLES[1]
        text = sp.render_opening_instruction(style)
        self.assertIn(style[0], text)
        self.assertIn(style[1][:12], text)
        # 起手式不放宽格式门槛：第一行仍必须是正文、引言仍有字数要求
        self.assertIn("第一句必须是故事正文", text)
        self.assertIn("60-300 字", text)

    def test_render_instruction_without_style_is_empty(self):
        self.assertEqual(sp.render_opening_instruction(None), "")

    def test_styles_all_require_person_or_dialogue(self):
        # 五种起手式都必须把人/对话写进第一段，否则会被 check_scene_dump
        # 判「空镜开场」扣分（守则正文里已写明这条）
        self.assertIn("第一段内都要出现人", sp.OPENING_VARIETY_RULE)
        for label, req in sp.OPENING_STYLES:
            self.assertTrue("人" in req or "对话" in req or "我" in req, label)


class TestPromptInjection(unittest.TestCase):
    REF = "她第一次来咨询的时候，手里攥着一张褪色的车票。" * 30

    def setUp(self):
        sp.reset_opening_cursor()

    def test_prompt_carries_rule_and_assigned_style(self):
        prompt, mode = sp.build_story_prompt("怎么把虐文写到极致？", self.REF)
        self.assertIn("开篇起手式多样化", prompt)
        self.assertIn("## 本篇指定的开篇起手式", prompt)
        # 第 1 篇拿轮换表第一项，日志里也能看到
        self.assertIn(sp.OPENING_STYLES[0][0], mode)
        # 下一批产出换一种，不再篇篇同构
        _, mode2 = sp.build_story_prompt("题", self.REF)
        self.assertIn(sp.OPENING_STYLES[1][0], mode2)
        self.assertNotEqual(mode, mode2)

    def test_explicit_style_overrides_rotation(self):
        prompt, mode = sp.build_story_prompt(
            "题", self.REF, opening_style=sp.OPENING_STYLES[2])
        self.assertIn(sp.OPENING_STYLES[2][0], prompt)
        self.assertIn(sp.OPENING_STYLES[2][0], mode)

    def test_rotate_off_and_config_off(self):
        import config.story as cs
        saved = cs.OPENING_VARIETY
        try:
            p, _ = sp.build_story_prompt("题", self.REF, rotate_opening=False)
            self.assertNotIn("## 本篇指定的开篇起手式", p)
            cs.OPENING_VARIETY = False
            p2, _ = sp.build_story_prompt("题", self.REF)
            self.assertNotIn("## 本篇指定的开篇起手式", p2)
        finally:
            cs.OPENING_VARIETY = saved

    def test_clean_mode_prompt_untouched(self):
        # 纯净模式刻意去限制：不注入起手式（实测它本来就多样：33% 我起手）
        prompt, _ = sp.build_clean_prompt("题", self.REF)
        self.assertNotIn("## 本篇指定的开篇起手式", prompt)
        self.assertNotIn("开篇起手式多样化", prompt)


if __name__ == "__main__":
    unittest.main()
