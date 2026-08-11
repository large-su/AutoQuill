# ============================================================
# applications/zhihu_story/extractors.py — 回答提取接缝
#
# 统一回答提取接口：任何提取器都返回 (title, answer, footer)
# 三元组。UIA（无障碍树）与 OCR（视觉滚屏）两条通道各自
# 适配为 AnswerExtractor，组合器负责主通道失败时的回退决策。
#
# 架构位置：Layer 3 (Adapters) — 感知通道
# ============================================================

import json
import logging

log = logging.getLogger(__name__)


class AnswerExtractor:
    """回答提取器接口。extract() 返回 (title, answer, footer)。"""

    name = "base"

    def extract(self):
        """返回 (title: str, answer: str, footer: dict | None)。"""
        raise NotImplementedError


class UiaAnswerExtractor(AnswerExtractor):
    """UIA 无障碍树通道：读取已渲染的首答（快、准、无滚屏）。

    失败（异常 / 超时 / 正文过短）统一降级为 ('', '', None)，
    具体原因写入日志，供组合器决定是否回退。
    """

    name = "UIA"

    def __init__(self, min_length=500, wait_timeout=4.0, poll_interval=0.25):
        self.min_length = min_length
        self.wait_timeout = wait_timeout
        self.poll_interval = poll_interval

    def extract(self):
        try:
            from applications.zhihu_story.a11y_probe import (
                extract_live_primary_answer,
            )
            title, answer, footer, reason = extract_live_primary_answer(
                min_length=self.min_length,
                wait_timeout=self.wait_timeout,
                poll_interval=self.poll_interval,
            )
            if title and answer:
                log.info(
                    "  UIA 首答采集成功：%s 字符，赞同=%s",
                    len(answer), footer.get("likes") if footer else None,
                )
                return title, answer, footer
            log.info("  UIA 首答未采用：%s", reason)
        except Exception as exc:
            log.warning("  UIA 首答采集异常：%s", exc)
        return "", "", None


class PlaywrightAnswerExtractor(AnswerExtractor):
    """Playwright MCP 通道：读取当前标签页的首答全文。

    通过 MCP 的 browser_evaluate 工具在当前激活的知乎回答页执行
    JS，提取 (title, answer, footer)。与 UIA 通道同构，供作者页
    批量采集复用同一套编排逻辑。

    footer 字段与 UIA 通道对齐：
    {likes, comments, collects, hearts, publish_time, answer_url}
    """

    name = "Playwright"

    def __init__(self, min_length=200):
        self.min_length = min_length

    # 供工具脚本传入的 evaluate 函数（避免在模块内硬依赖 MCP client）
    _evaluate = None

    @classmethod
    def bind_evaluate(cls, evaluate):
        """注入 browser_evaluate 函数（MCP 工具）。"""
        cls._evaluate = evaluate

    def extract(self):
        if self._evaluate is None:
            log.warning("  Playwright 通道未绑定 evaluate（跳过）")
            return "", "", None
        try:
            result = self._evaluate(_EXTRACT_JS)
            title = (result.get("title") or "").strip()
            answer = (result.get("answer") or "").strip()
            if not (title and answer):
                log.info("  Playwright 未读到有效内容")
                return "", "", None
            footer = result.get("footer") or {}
            if len(answer) < self.min_length:
                log.info("  Playwright 正文过短（%d 字）", len(answer))
                return "", "", None
            log.info("  Playwright 采集成功：%d 字符，赞同=%s",
                     len(answer), footer.get("likes"))
            return title, answer, footer
        except Exception as exc:
            log.warning("  Playwright 采集异常：%s", exc)
            return "", "", None


# 提取 JS：读取当前知乎回答页的题目、正文、互动数据
# 只取主回答（QuestionAnswer-content 容器），避免串到页面里其他回答
_EXTRACT_JS = r"""
() => {
  // 定位主回答容器（详情页的答案区块）
  const main = document.querySelector('.QuestionAnswer-content, .AnswerItem');
  const scope = main || document;

  // 题目：h1 标题（回答页一般为 QuestionHeader-title）
  const titleEl = document.querySelector('h1.QuestionHeader-title, h1.PostIndex-title, h1');
  const title = titleEl ? titleEl.textContent.trim() : '';

  // 正文：主容器内的 .RichContent-inner（答案富文本容器）
  const answerEl = scope.querySelector('.RichContent-inner, .RichText');
  let answer = '';
  if (answerEl) {
    answer = answerEl.textContent.trim();
    // 去首尾换行、压缩多余空行
    answer = answer.replace(/\n{3,}/g, '\n\n').trim();
  }

  // 互动数据：只取主容器内的按钮（去图标字符）
  const clean = s => (s || '').replace(/[^\w一-龥 .\-]/g, ' ').replace(/\s+/g, ' ').trim();
  const allBtns = Array.from(scope.querySelectorAll('.ContentItem-actions button, .ContentItem-actions span'));
  const labels = allBtns.map(el => clean(el.textContent)).filter(Boolean);

  // 解析赞同数（"赞同 681" 或 "681"）
  const likesEl = scope.querySelector('.VoteButton');
  const likesText = likesEl ? clean(likesEl.textContent) : '';
  const likesMatch = likesText.match(/(\d[\d,]*)/);
  const likes = likesMatch ? parseInt(likesMatch[1].replace(/,/g, ''), 10) : null;

  // 评论数（"100 条评论"）
  const commentsMatch = labels.find(l => l.includes('条评论'));
  const comments = commentsMatch ? parseInt(commentsMatch.match(/\d+/)?.[0] || '0', 10) : null;

  // 收藏数 / 喜欢：正文下面通常两个纯数字（收藏、喜欢）
  // "收藏 580" 或纯 "580"（紧跟在评论后的数字是收藏）
  const numeric = labels.filter(l => /^\d[\d,]*$/.test(l)).map(l => parseInt(l.replace(/,/g, ''), 10));
  // 移除赞同（首个）和评论，剩下的第一个是收藏，第二个是喜欢
  const others = numeric.filter(n => n !== likes);
  const collects = others.length > 0 ? others[0] : null;
  const hearts = others.length > 1 ? others[1] : null;

  // 发布时间
  const timeEl = scope.querySelector('.ContentItem-time, .AnswerItem time, .QuestionAnswer-content time');
  const publishTime = timeEl ? timeEl.textContent.trim().replace(/^发布于/, '').trim() : '';

  return {
    title,
    answer,
    footer: {
      likes, comments, collects, hearts,
      publish_time: publishTime,
      answer_url: location.href,
    },
  };
}
"""


class OcrAnswerExtractor(AnswerExtractor):
    """OCR 视觉通道：滚屏截图 + 文字识别提取首答。

    保底通道，任何情况下都可用（代价是慢、依赖窗口焦点）。
    """

    name = "OCR"

    def __init__(self, left_x, right_x, top_y, bottom_y,
                 min_length=500, max_retries=3):
        self.left_x = left_x
        self.right_x = right_x
        self.top_y = top_y
        self.bottom_y = bottom_y
        self.min_length = min_length
        self.max_retries = max_retries

    def extract(self):
        from ocr_utils import extract_zhihu_question_and_answer
        return extract_zhihu_question_and_answer(
            self.left_x, self.right_x, self.top_y, self.bottom_y,
            min_length=self.min_length,
            max_retries=self.max_retries,
        )


class FallbackAnswerExtractor(AnswerExtractor):
    """主通道优先、失败回退的组合提取器。

    - primary 为 None 时直接走 fallback（纯 OCR 模式）
    - require_likes=True 时，主通道结果缺赞同数也视为失败
      （素材质量门槛：无互动数据的故事不值得生成）
    """

    name = "UIA→OCR"

    def __init__(self, primary, fallback, require_likes=False):
        self.primary = primary
        self.fallback = fallback
        self.require_likes = require_likes

    def extract(self):
        if self.primary is not None:
            try:
                title, answer, footer = self.primary.extract()
            except Exception as exc:
                log.warning("  %s 通道异常，转 %s 保底：%s",
                            self.primary.name, self.fallback.name, exc)
                return self.fallback.extract()
            if title and answer:
                likes_missing = (
                    self.require_likes
                    and (not footer or footer.get("likes") is None)
                )
                if not likes_missing:
                    return title, answer, footer
                log.info("  UIA 未读取到赞同数，转 OCR 保底")
        return self.fallback.extract()
