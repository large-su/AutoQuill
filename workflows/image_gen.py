# ============================================================
# workflows/image_gen.py — 图像生成工作流
#
# 生命周期（与故事工作流不同）：
#   提取绘图提示词（API） → Web 绘图（Aizex） → 下载保存
#
# 测试阶段：从 output/ 中随机选故事，LLM 提炼场景生成提示词
# 正式阶段：读取独立的提示词 md 文档
# ============================================================

import os
import random
import time
import logging
from datetime import datetime

log = logging.getLogger(__name__)


class ImageGenWorkflow:
    """
    图像生成工作流。

    用法：
        wf = ImageGenWorkflow()
        wf.run()                      # 从 output/ 随机选故事
        wf.run(source_story_path=...) # 指定故事文件
    """

    name = "image_gen"

    def _pick_random_story(self):
        """从 output/ 随机选一篇故事 .md 文件"""
        output_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "output"
        )
        if not os.path.isdir(output_dir):
            raise FileNotFoundError(f"output 目录不存在：{output_dir}")

        # 只挑 story_N_*.md，排除 retry 版本和非 story 文件
        candidates = [
            f for f in os.listdir(output_dir)
            if f.startswith("story_") and f.endswith(".md")
            and "_retry_" not in f
        ]
        if not candidates:
            raise FileNotFoundError(f"output 目录下没有找到 story_*.md 文件")

        picked = random.choice(candidates)
        log.info(f"随机选中故事：{picked}")
        return os.path.join(output_dir, picked)

    def _extract_image_prompt(self, story_path):
        """阶段 0：LLM 从故事中提取绘图提示词"""
        log.info("=" * 50)
        log.info("阶段 0：提取绘图提示词")

        with open(story_path, 'r', encoding='utf-8') as f:
            story_text = f.read()

        log.info(f"  故事长度：{len(story_text)} 字符")

        if len(story_text) > 8000:
            story_text = story_text[:6000] + "\n\n...\n\n" + story_text[-2000:]
            log.info(f"  截取后长度：{len(story_text)} 字符")

        from applications.image_gen.prompts import IMAGE_PROMPT_EXTRACT
        prompt = IMAGE_PROMPT_EXTRACT.format(story_text=story_text)

        log.info("  调用 LLM 提取图像提示词...")
        from llm_api import _call_llm_streaming
        result, elapsed, error = _call_llm_streaming(
            prompt, max_tokens=500, temperature=0.8,
            label="图像提示词提取"
        )

        if error:
            raise RuntimeError(f"LLM 提示词提取失败：{error}")

        prompt_text = result.strip()

        if len(prompt_text) < 10:
            raise RuntimeError(
                f"LLM 返回了过短的提示词（{len(prompt_text)} 字符），"
                f"内容：{repr(prompt_text[:200])}"
            )

        log.info(f"  ✓ 提示词（{len(prompt_text)} 字符）：{prompt_text[:120]}...")
        log.info(f"  耗时：{elapsed:.1f}s")
        return prompt_text

    def _generate_and_download(self, prompt, save_dir):
        """阶段 1-2：Aizex Web 绘图 + 下载保存"""
        from config import WEB_DRIVERS
        from web_drivers.aizex import AizexDriver

        # 图像生成必须使用 Aizex（GPT-5.5 Thinking Extended），
        # 不受 WEB_DRIVER_NAME 影响
        driver = AizexDriver(WEB_DRIVERS["Aizex"])
        log.info("阶段 1：Aizex Web 绘图...")
        filepath = driver.generate_image(prompt, save_dir)
        log.info(f"阶段 2：图片已保存 → {filepath}")
        return filepath

    def run(self, source_story_path=None):
        """
        主入口：提取提示词 → Web 绘图 → 下载。

        参数：
            source_story_path: 故事 .md 路径（None 则随机选择）
        """
        from applications.image_gen.config import IMAGE_OUTPUT_DIR

        # 阶段 0：提取提示词
        story_path = source_story_path or self._pick_random_story()
        prompt = self._extract_image_prompt(story_path)

        # 阶段 1-2：生成 + 下载
        save_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            IMAGE_OUTPUT_DIR
        )
        filepath = self._generate_and_download(prompt, save_dir)

        log.info(f"\n{'=' * 50}")
        log.info(f"图像生成完成 → {filepath}")
        log.info(f"{'=' * 50}")
        return filepath
