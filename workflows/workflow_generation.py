# ============================================================
# workflows/GenerationMixin — 单篇生成编排：API/Web 分发、带失败原因反馈的重试循环
# P0 拆分自 WorkflowBase；方法体逐字搬运未改动。
# 行为守护：tests/test_zhihu_workflow 的源码锚点断言 + 全量回归。
# ============================================================

import os
import logging
import time
from datetime import datetime

log = logging.getLogger(__name__)


class GenerationMixin:
    def generate_story(self, question_title, top_answer, recipe=None,
                       feedback=None):
        """根据 LLM_MODE 分发到 API 或 Web 模式生成故事。

        feedback：重试修正反馈（str 或 str 列表，可选），透传给 prompt
        构建，供模型在新一轮生成中针对性修正上一版的问题。
        """
        from config import LLM_MODE

        log.info("=" * 50)
        log.info(f"步骤 3：生成故事（模式：{LLM_MODE}）")
        if recipe:
            log.info(f"  配方模式：[{recipe.get('genre', '?')}] "
                     f"{recipe.get('hook', '?')[:25]}")
        # 如果已加载了元知识，提示一下
        log.info("=" * 50)

        if LLM_MODE == "api":
            return self._generate_api(question_title, top_answer, recipe,
                                      feedback=feedback)
        else:
            return self._generate_web(question_title, top_answer, recipe,
                                      feedback=feedback)

    def _generate_api(self, question_title, top_answer, recipe=None,
                      feedback=None):
        """API 模式：流式 HTTP 请求"""
        from story_generation import generate_story

        author = getattr(self, "author", None)
        story = generate_story(question_title, top_answer, recipe=recipe,
                               author=author, feedback=feedback)
        if not story:
            log.error("API 生成失败")
            from desktop_utils import focus_edge
            fallback = input("切换到网页模式重试？(y/n) >> ").strip().lower()
            if fallback == 'y':
                focus_edge()
                return self._generate_web(
                    question_title, top_answer, recipe, feedback=feedback
                )
            return None
        return story

    def _generate_web(self, question_title, top_answer, recipe=None,
                      feedback=None):
        """Web 模式：通过 Web Driver 操控 LLM 网站（单轮生成）"""
        return self._generate_web_short_form(
            question_title, top_answer, recipe, feedback=feedback
        )

    def _generate_web_short_form(self, question_title, top_answer, recipe=None,
                                 feedback=None):
        """Web 短文模式：单轮 prompt 直接出正文"""
        from web_drivers import get_driver
        from llm_api import build_story_prompt, _load_author_profile_or_none

        author = getattr(self, "author", None)
        full_prompt, mode_str = build_story_prompt(
            question_title, top_answer, recipe,
            author_profile=_load_author_profile_or_none(author),
            feedback=feedback,
        )
        log.info(f"  Prompt 模式：{mode_str}")

        driver = get_driver()
        return driver.generate(full_prompt)

    @staticmethod
    def _format_format_failure(details):
        """把 validate_story_format 的 details 字典渲染成简短原因串。"""
        if not details:
            return ""
        parts = []
        for k, v in details.items():
            if k == "原因":
                parts.append(f"原因：{v}")
            else:
                parts.append(f"{k}：{v}")
        return "；".join(parts)

    def generate_story_with_retry(self, title, answer, max_attempts=None,
                                  min_length=500):
        """生成故事；无输出 / 过短 / 格式不合规时带失败原因反馈重试。

        ★ 带反馈的重试：每轮把上一版的失败原因（字数/章节/长段/引号等）
        注入重试 prompt，让模型针对性修正——比同 prompt 盲目重试的收敛率
        显著更高，明显提升「单轮完整链路」一次成功概率。

        达到 STORY_GENERATE_MAX_ATTEMPTS（默认 3）次仍未产出合格文本时，
        返回最高分版本（可能非空，调用方存盘供人工核对）且 ok=False。

        Web 通道同样做 clean_story_output + fix_story_format。
        返回 (story, ok)：ok=True 表示最后一次产出格式合规且不少于
        min_length；ok=False 表示多次尝试仍未合格（story 为最高分版本）。
        """
        from config import LLM_MODE
        from config.story import STORY_GENERATE_MAX_ATTEMPTS
        from core.story_text import (
            clean_story_output, fix_story_format, validate_story_format,
        )

        max_attempts = max_attempts or STORY_GENERATE_MAX_ATTEMPTS
        best = None            # 兜底：多次失败时返回更高分版本
        best_score = -1
        last_reason = None     # 上一轮失败原因（重试反馈）

        for attempt in range(max_attempts):
            story = self.generate_story(title, answer, feedback=last_reason)
            if story and LLM_MODE == "web":
                story = fix_story_format(clean_story_output(story))

            if not story:
                last_reason = "生成失败（模型无输出）"
                log.warning("故事生成失败（无输出），第 %d/%d 次重试…",
                            attempt + 1, max_attempts)
                continue

            if len(story) < min_length:
                if best is None or len(story) > len(best):
                    best = story
                last_reason = f"字数过短（仅 {len(story)} 字，要求至少 " \
                              f"{min_length} 字）"
                log.warning("故事过短（%d字），第 %d/%d 次重试…",
                            len(story), attempt + 1, max_attempts)
                continue

            fmt_score, is_valid, details = validate_story_format(story)
            if is_valid:
                return story, True
            if fmt_score > best_score:
                best_score, best = fmt_score, story
            last_reason = (self._format_format_failure(details)
                           or f"格式不合规（{fmt_score}/10）")
            log.warning("故事格式不合规（%d/10），第 %d/%d 次重试…",
                        fmt_score, attempt + 1, max_attempts)

        return best, False

    # ============================================================
    # 保存故事文件（通用）
    # ============================================================
