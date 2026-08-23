# -*- coding: utf-8 -*-
"""AI 味检测器单测：特征统计与指数。"""
import unittest

from tools.ai_flavor_check import check_text, _verdict, _sentences


class AiFlavorTest(unittest.TestCase):
    PLAIN = "今天下班路上遇到一只橘猫，它蹲在台阶上晒太阳。我买了根火腿肠喂它，它吃完就跑了。天气不错。"
    FILLERY = "他仿佛看到了什么，瞬间愣住了，终于深深地吸了一口气，默默地握紧了拳头，似乎一切都很沉重。"
    CONNY = "然而，与此同时，这不仅仅是选择问题，而是原则问题。"

    def test_plain_text_low_score(self):
        got = check_text(self.PLAIN)
        self.assertIsNotNone(got)
        metrics, score = got
        self.assertLess(score, 25)

    def test_filler_heavy_text_high_score(self):
        metrics, score = check_text((self.FILLERY + "\n") * 6)
        self.assertGreaterEqual(metrics["修饰语"], 3)
        self.assertGreaterEqual(score, 20)

    def test_connector_and_parallel_text(self):
        metrics, score = check_text((self.CONNY + "\n") * 4)
        self.assertGreater(metrics["连接词"], 0)
        self.assertGreater(metrics["排比"], 0)

    def test_empty_and_short_text(self):
        self.assertIsNone(check_text(""))
        self.assertIsNone(check_text(None))

    def test_sentences_split(self):
        self.assertEqual(len(_sentences("第一句。第二句！第三句？")), 3)

    def test_verdict_labels(self):
        self.assertEqual(_verdict(10), "低（像人手写）")
        self.assertEqual(_verdict(30), "中（有一定AI味）")
        self.assertEqual(_verdict(60), "高（AI味明显）")


if __name__ == "__main__":
    unittest.main()
