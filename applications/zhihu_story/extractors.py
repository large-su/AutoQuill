# ============================================================
# applications/zhihu_story/extractors.py — 回答提取接缝
#
# 统一回答提取接口：任何提取器都返回 (title, answer, footer)
# 三元组。DOM 通道（Playwright）为唯一提取器，供作者页批量
# 采集复用。UIA/OCR 通道已于 V4.0.4 归档（见 archive/）。
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


class PlaywrightAnswerExtractor(AnswerExtractor):
    """Playwright MCP 通道：读取当前标签页的首答全文。

    在工具脚本注入的 evaluate 函数（浏览器上下文）中执行 JS，
    提取 (title, answer, footer)。DOM 通道为唯一提取器，供作者页
    批量采集复用同一套编排逻辑。

    footer 字段：
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
            # 经类访问避免实例查找把普通函数绑定成方法（self 被预置为实参）
            result = type(self)._evaluate(_EXTRACT_JS)
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

