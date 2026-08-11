# ============================================================
# tests/test_author_inject.py — 作者技能注入生成链路测试
#
# 运行：python -m unittest discover -s tests -v
# ============================================================

import tempfile
import unittest

from applications.zhihu_story.author_profiler import (
    render_style_section,
    save_profile,
)

FAKE_PROFILE = {
    "author": "测试作者",
    "signature": {
        "style": "短句白描，节奏快",
        "opening_patterns": ["全剧透式梗概开场", "离奇设定第一句抛出"],
        "narrative_techniques": ["信息差驱动", "编号小节硬切"],
        "character_patterns": ["利己型第一人称叙述者"],
        "dialogue_style": "单句台词炸弹",
        "tone": "热闹沙雕",
        "signature_phrases": ["这么帅的男人，肯定和我有关系。", "我：……"],
        "avoid": ["回避环境描写", "回避多轮对话"],
        "excerpts": {
            "opening": "车祸失忆后，老公非常紧张我。",
            "ending": "我瞪大了眼睛。",
        },
    },
}


class TestRenderStyleSection(unittest.TestCase):
    def test_renders_all_fields(self):
        section = render_style_section(FAKE_PROFILE)
        self.assertIn("测试作者", section)
        self.assertIn("短句白描，节奏快", section)
        self.assertIn("- 全剧透式梗概开场", section)
        self.assertIn("- 信息差驱动", section)
        self.assertIn("- 利己型第一人称叙述者", section)
        self.assertIn("单句台词炸弹", section)
        self.assertIn("热闹沙雕", section)
        self.assertIn("- 回避环境描写", section)
        self.assertIn("**开头**：车祸失忆后", section)
        self.assertIn("**结尾**：我瞪大了眼睛。", section)

    def test_missing_excerpts(self):
        profile = {"author": "甲", "signature": {"style": "x"}}
        section = render_style_section(profile)
        self.assertIn("（未提炼）", section)
        self.assertNotIn("**开头**", section)


class TestBuildStoryPromptInjection(unittest.TestCase):
    def _build(self, with_profile=True):
        from llm_api import build_story_prompt
        profile = FAKE_PROFILE if with_profile else None
        return build_story_prompt(
            "测试问题标题？",
            recipe={"genre": "甜宠文", "hook": "反套路", "perspective": "第一人称"},
            author_profile=profile,
        )

    def test_recipe_mode_appends_author_section(self):
        user_message, mode_str = self._build()
        self.assertIn("作者风格签名", user_message)
        self.assertIn("模仿对象：测试作者", user_message)
        self.assertIn("短句白描，节奏快", user_message)
        self.assertIn("+作者:测试作者", mode_str)
        # 问题标题仍在 prompt 中
        self.assertIn("测试问题标题", user_message)

    def test_no_profile_no_section(self):
        user_message, mode_str = self._build(with_profile=False)
        self.assertNotIn("作者风格签名", user_message)
        self.assertNotIn("+作者", mode_str)

    def test_reference_mode_appends_author_section(self):
        from unittest import mock
        from llm_api import build_story_prompt
        with mock.patch("applications.zhihu_story.config.STORY_MATERIAL_MODE",
                        "reference"):
            user_message, mode_str = build_story_prompt(
                "测试问题标题？",
                reference_answer="参考回答文本" * 20,
                author_profile=FAKE_PROFILE,
            )
        self.assertIn("作者风格签名", user_message)
        self.assertIn("参考文章模式", mode_str)

    def test_sample_mode_uses_sampled_reference(self):
        from llm_api import build_story_prompt
        # 长参考文章 → 只注入开头 max_chars 字（默认 3000）
        ans = ("参考段0号的内容。" + "\n\n" +
               ("填充内容。" * 400))
        user_message, mode_str = build_story_prompt(
            "测试问题标题？",
            reference_answer=ans,
            author_profile=FAKE_PROFILE,
        )
        self.assertIn("采样模式", mode_str)
        self.assertIn("仅供感受语感与节奏", user_message)
        self.assertIn("严禁搬运", user_message)
        self.assertIn("作者风格签名", user_message)
        self.assertIn("测试问题标题", user_message)
        # 注入的是参考文章开头，且截断在 3000 字内
        self.assertIn("参考段0号的内容。", user_message)
        self.assertIn("参考文章（高赞回答开头", user_message)
        idx = user_message.find("参考段0号的内容。")
        end = user_message.find("请根据以上要求", idx)
        self.assertLessEqual(end - idx, 3000)

    def test_sample_mode_without_reference(self):
        from llm_api import build_story_prompt
        user_message, mode_str = build_story_prompt(
            "测试问题标题？",
            reference_answer=None,
            author_profile=None,
        )
        self.assertIn("采样模式", mode_str)
        self.assertIn("无参考文章", mode_str)
        self.assertNotIn("参考文章（高赞回答片段采样", user_message)


class TestLoadAuthorProfileHelper(unittest.TestCase):
    def test_unknown_author_returns_none(self):
        from llm_api import _load_author_profile_or_none
        self.assertIsNone(_load_author_profile_or_none("不存在的作者"))

    def test_roundtrip_load(self):
        from applications.zhihu_story.author_profiler import load_author_profile
        from llm_api import _load_author_profile_or_none

        with tempfile.TemporaryDirectory() as tmp:
            import applications.zhihu_story.author_profiler as ap
            original_dir = ap.AUTHORS_DIR
            ap.AUTHORS_DIR = tmp
            try:
                save_profile(FAKE_PROFILE, out_dir=tmp)
                profile = _load_author_profile_or_none("测试作者")
                self.assertIsNotNone(profile)
                self.assertEqual(profile["author"], "测试作者")
                self.assertIn("signature", profile)
            finally:
                ap.AUTHORS_DIR = original_dir


if __name__ == "__main__":
    unittest.main()
