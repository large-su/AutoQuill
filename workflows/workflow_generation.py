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
            # Web 通道带断路器与 API 自动降级（P1：8/29 DeepSeek 前端
            # 改版 3 连败教训；同任务内连续失败 N 次后跳过 Web 直走 API）
            return self._generate_web_with_failover(
                question_title, top_answer, recipe, feedback=feedback)

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

    _WEB_BREAK_HINTS = (
        "找不到", "输入框", "前端可能改版", "站点打开失败",
        "发送失败", "页面状态", "可能需要人工介入",
    )

    def _generate_web_with_failover(self, question_title, top_answer,
                                    recipe=None, feedback=None):
        """Web 生成，带「前端改版/输入框丢失」类故障的自动降级。

        - 单次失败：告警并立即用 API 通道补跑本轮（需已配置 API Key）
        - 连续失败 >= WEB_FAILOVER_MAX_CONSECUTIVE：本轮剩余尝试跳过
          Web 直走 API（断路器），避免每次尝试都白等页面重试
        - 非故障类异常（风控/超时等）照旧抛出，不吞错误
        """
        from config.story import (WEB_FAILOVER_TO_API,
                                  WEB_FAILOVER_MAX_CONSECUTIVE)
        fails = getattr(self, "_web_fail_consecutive", 0)
        if fails >= WEB_FAILOVER_MAX_CONSECUTIVE:
            log.warning("Web 通道已连续失败 %d 次，本轮剩余尝试"
                        "自动降级到 API 通道", fails)
            return self._generate_api(question_title, top_answer, recipe,
                                      feedback=feedback)
        try:
            story = self._generate_web(question_title, top_answer, recipe,
                                       feedback=feedback)
            self._web_fail_consecutive = 0
            return story
        except RuntimeError as exc:
            msg = str(exc)
            is_break = any(h in msg for h in self._WEB_BREAK_HINTS)
            if WEB_FAILOVER_TO_API and is_break:
                self._web_fail_consecutive = fails + 1
                log.warning(
                    "Web 通道故障（%s…），自动降级到 API 通道"
                    "（连续第 %d 次）", msg[:70], self._web_fail_consecutive)
                return self._generate_api(question_title, top_answer,
                                          recipe, feedback=feedback)
            raise

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
        self._web_fail_consecutive = 0  # 每轮任务重置 Web 断路器计数

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
    # 纯净模式生成（工作台 · 完整链路）
    # ============================================================

    def generate_clean_story(self, question_title, reference_answer,
                             feedback=None):
        """纯净模式：极简 prompt（风格学习 + 原创禁令），API/Web 分发。

        与 generate_story 的区别：不做格式/字数硬约束，也不注入
        去AI味/命名/章节守则。原创底线由 prompt 禁令 + 生成后的
        generate_clean_with_retry 审核环节共同把关。
        """
        from config import LLM_MODE
        from story_prompt import build_clean_prompt

        log.info("=" * 50)
        log.info(f"步骤 3：纯净模式生成（{LLM_MODE}）")
        log.info("=" * 50)

        if LLM_MODE == "api":
            from story_generation import generate_story_clean
            return generate_story_clean(question_title, reference_answer,
                                        feedback=feedback)

        # Web 模式：网页版大模型（无 API Key 依赖）
        try:
            from web_drivers import get_driver
            full_prompt, mode_str = build_clean_prompt(
                question_title, reference_answer, feedback=feedback)
            log.info(f"  Prompt 模式：{mode_str}")
            driver = get_driver()
            story = driver.generate(full_prompt)
            if story and story.strip():
                from core.story_text import clean_story_output
                story = clean_story_output(story)
            return story or None
        except Exception as exc:
            log.warning("纯净模式 Web 生成失败：%s", exc)
            return None

    def generate_clean_with_retry(self, title, answer, max_attempts=None):
        """纯净模式生成 + 洗稿/抄袭审核重试。

        每轮生成后调用 core.originality.audit_originality，把新回答与
        参考高赞回答对比；不通过时把审核意见注入重试 prompt 重新创作，
        最多 CLEAN_MAX_GEN_ATTEMPTS 次。

        返回 (story, audit)：audit 为最后一次审核的 dict（含 passed）；
        story 为最后一次生成文本（可能 None）。
        """
        from config.story import CLEAN_AUDIT_ENABLE, CLEAN_MAX_GEN_ATTEMPTS
        max_attempts = max_attempts or CLEAN_MAX_GEN_ATTEMPTS
        last_audit = None
        best = None

        wash_streak = 0
        for attempt in range(max_attempts):
            feedback = None
            if attempt > 0 and last_audit and not last_audit.get("passed"):
                from core.originality import audit_feedback_text
                feedback = audit_feedback_text(last_audit)
                if "洗稿" in str(last_audit.get("verdict") or ""):
                    wash_streak += 1
                    if wash_streak >= 2:
                        feedback = (
                            "【注意：连续两版都与参考回答结构重合】这次必须把设定放到"
                            "与参考完全不同的背景（如换成现代/校园/职场/科幻/都市等），"
                            "人物关系、关键事件、推进顺序全部从头构思，不要借鉴参考的"
                            "任何情节骨架，只保留风格上的借鉴。\n\n" + feedback)
            story = self.generate_clean_story(title, answer, feedback=feedback)
            if not story or not str(story).strip():
                last_reason = "生成失败（模型无输出）"
                log.warning("纯净模式生成失败（无输出），第 %d/%d 次重试…",
                            attempt + 1, max_attempts)
                if last_audit is None:
                    last_audit = {"passed": False, "verdict": "无输出",
                                  "reasons": [last_reason], "signals": {},
                                  "llm_detail": None, "originality": None}
                continue
            best = story

            if not CLEAN_AUDIT_ENABLE:
                last_audit = {"passed": True, "verdict": "原创（审核已关闭）",
                              "reasons": [], "signals": {},
                              "llm_detail": None, "originality": None}
                log.info("纯净模式审核已关闭（CLEAN_AUDIT_ENABLE=False），直接通过")
                return story, last_audit

            from core.originality import audit_originality
            last_audit = audit_originality(title, story, answer)
            reasons = last_audit.get("reasons") or []
            if last_audit.get("passed"):
                log.info(f"  原创审核通过：{last_audit.get('verdict')}")
                if reasons:
                    for r in reasons:
                        log.info(f"    · {r}")
                return story, last_audit
            log.warning("  原创审核未通过：%s（第 %d/%d 次）",
                        last_audit.get("verdict"), attempt + 1, max_attempts)
            for r in reasons[:5]:
                log.warning(f"    - {r}")

        if best is None:
            log.error("纯净模式：%d 次尝试均无输出，本轮未完成", max_attempts)
        else:
            log.warning("纯净模式：%d 次尝试后仍未通过原创审核，最高分版已存盘供人工核对",
                        max_attempts)
        return best, last_audit
    # ============================================================
    # 保存故事文件（通用）
    # ============================================================
