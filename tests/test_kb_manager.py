# kb_manager 防回归：配方提炼路径的 token 配额一致性
import inspect
import unittest

import kb_manager


class TestRecipeTokenAlignment(unittest.TestCase):
    """同步/异步配方提炼的 max_tokens 必须对齐。

    回归背景：同步路径（extract_recipes，单轮 run_single 使用）曾用
    250/900 tokens，8 字段配方 JSON 被截断导致解析全部失败（日志
    「首次提炼成功率低 0/1」）；异步路径（extract_single_recipe）
    已修为 1000/2400。两处必须保持同一组值。
    """

    def test_sync_path_matches_async_path(self):
        sync_src = inspect.getsource(kb_manager.extract_recipes)
        async_src = inspect.getsource(kb_manager.extract_single_recipe)
        self.assertIn("2400 if RECIPE_VERBOSE_MODE else 1000", sync_src)
        # 异步路径的同款表达式也必须存在（防止只改一边）
        self.assertIn("2400 if RECIPE_VERBOSE_MODE else 1000", async_src)


if __name__ == "__main__":
    unittest.main()
