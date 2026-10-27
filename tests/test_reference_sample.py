import unittest

from core.reference_sample import sample_reference_narrative


class ReferenceSampleTest(unittest.TestCase):
    def test_empty_short_and_invalid_budget(self):
        self.assertEqual(sample_reference_narrative(""), "")
        self.assertEqual(sample_reference_narrative("  短文  "), "短文")
        self.assertEqual(sample_reference_narrative("x" * 20, 20), "x" * 20)
        for value in (0, -1, True, 1.5, "20"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sample_reference_narrative("abc", value)

    def test_every_budget_is_bounded(self):
        text = "开头。" + ("中间内容。" * 120) + "真实末尾。"
        for budget in range(1, 201):
            with self.subTest(budget=budget):
                self.assertLessEqual(len(sample_reference_narrative(text, budget)), budget)

    def test_labeled_excerpts_are_ordered_non_overlapping_source_substrings(self):
        text = ("HEAD-" + "a" * 180 + "。" + "MID-" + "b" * 220 + "。"
                + "TAIL-" + "c" * 180 + "唯一末尾。")
        out = sample_reference_narrative(text, 260)
        self.assertEqual(out.count("【中间省略】"), 2)
        self.assertTrue(out.endswith("唯一末尾。"))
        labels = ("【开头片段】", "【中部片段】", "【原文末段】")
        pieces = []
        for index, label in enumerate(labels):
            start = out.index(label) + len(label) + 2
            end = (out.index("【中间省略】", start) - 2 if index < 2
                   else len(out))
            pieces.append(out[start:end])
        locations = [text.index(piece) for piece in pieces]
        self.assertEqual(locations, sorted(locations))
        self.assertLess(locations[0] + len(pieces[0]), locations[1])
        self.assertLess(locations[1] + len(pieces[1]), locations[2])

    def test_long_unpunctuated_and_newline_text_keep_true_tail(self):
        raw = "开头\n" + "中部\n" * 100 + "末尾"
        self.assertIn("末尾", sample_reference_narrative(raw, 180))
        unpunctuated = "a" * 800 + "Z"
        self.assertTrue(sample_reference_narrative(unpunctuated, 180).endswith("Z"))

    def test_tiny_budget_uses_bounded_head(self):
        self.assertEqual(sample_reference_narrative("abcdefghijklmnopqrstuvwxyz", 10), "abcdefghij")


if __name__ == "__main__":
    unittest.main()
