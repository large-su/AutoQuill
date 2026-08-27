# ============================================================
# browser_utils.py — 浏览器适配层纯工具/常量（P0 拆分叶子模块）
#
# 仅标准库依赖，被 browser_adapter 与三个 mixin 共享；放叶子是
# 为了让 mixin -> utils 单向引用、adapter 组合类再收敛三方，杜绝环。
# ============================================================

import json
import logging
import os
import re
import time

log = logging.getLogger(__name__)

from core.paths import data as _data_path
def _find_edge():
    """定位系统 Edge 可执行文件：AQ_EDGE_PATH 环境变量 → x86 → x64 →
    注册表 App Paths（用户级/便携安装兜底）。找不到返回 None。"""
    cand = os.environ.get("AQ_EDGE_PATH", "").strip()
    if cand and os.path.isfile(cand):
        return cand
    for p in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if os.path.isfile(p):
            return p
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(
                        hive, "Software\\Microsoft\\Windows\\"
                              "CurrentVersion\\App Paths\\msedge.exe") as key:
                    path, _ = winreg.QueryValueEx(key, "")
                    if path and os.path.isfile(path):
                        return path
            except OSError:
                continue
    except Exception:
        pass
    return None


EDGE_PATH = _find_edge()
USER_DATA_DIR = _data_path("data", "browser_profile")
STORAGE_STATE_PATH = _data_path("config", "browser_state.json")

# 无头模式下 Playwright 默认 UA 含 "HeadlessChrome"，知乎据此不加载
# 作者内容列表（列表空 → 采集 0 篇、判定失败；前台正常）。用去掉
# Headless 的正常 Edge UA 覆盖。版本号与当前 Edge/Chromium 对齐，
# Edge 升级后仅需同步此处版本号。
_CLEAN_EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"
)

_ZHIHU_HOME = "https://www.zhihu.com/"

# 页面交互超时（毫秒）：所有 evaluate/goto 必须有界。
# 历史事故：风控页/加载中页面会让 evaluate 无限阻塞（进程挂死数
# 分钟无日志），任何交互都不允许无界等待。
_EVAL_TIMEOUT = 15000
_NAV_TIMEOUT = 20000
# 浏览器进程启动超时（毫秒）。启动无日志是历史卡死事故的高发段：
# playwright 驱动或 Edge 拉起失败时无任何输出，必须显式超时。
_LAUNCH_TIMEOUT_MS = 60000


def build_draft_marker(text, limit=60):
    """从故事原文生成草稿确认 marker：剥掉全部空白。

    知乎把 md 导入为 HTML（段落 \n\n → <br><br>），纯文本在两侧的
    空白形态不同；剥空白后双方可逐字匹配。"""
    return re.sub(r"\s+", "", text)[:limit]


def clean_story_markdown(text):
    """故事 md → 纯文本：`## **N**` → `N`、`**x**` → `x`。

    知乎编辑器不识别 md 符号，直接 fill 会把 `## **1**` 原样写进
    草稿。此函数同时供 text/plain 粘贴通道和保存确认 marker 使用。"""
    if not text:
        return ""
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    paras = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    return "\n\n".join(paras)


def story_markdown_to_html(text):
    """故事 md → 粘贴 HTML：`## **N**` → `<p><b>N</b></p>`，`**x**` → `<b>x</b>`。

    知乎编辑器是 Draft.js，粘贴富文本时按块解析并落盘（<p>/<b> 真实
    保存）；fill 纯文本做不到格式。空段丢弃，段落转 <p> 块。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n+", text or "") if p.strip()]
    blocks = []
    for p in paras:
        m = re.match(r"^#{1,6}\s*(.+)$", p)
        inner = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", m.group(1) if m else p)
        blocks.append(f"<p>{inner}</p>")
    return "".join(blocks)


# 展开第一个回答的「阅读全文」：只处理首答容器内的折叠按钮，
# 不能展开所有回答（后面的回答不属于本次提取目标）
_EXPAND_FIRST_COLLAPSED_JS = r"""
() => {
  const scope = document.querySelector('.QuestionAnswer-content, .AnswerItem');
  const btn = scope ? scope.querySelector('.RichContent-collapsedText') : null;
  if (btn) btn.click();
  return true;
}
"""

# 首答提取（问题页）：返回 标题/正文/互动/时间，全部来自 DOM 文本
_PRIMARY_ANSWER_JS = r"""
async () => {
  const containers = Array.from(document.querySelectorAll('.QuestionAnswer-content, .AnswerItem'));
  const scope = containers[0] || document;
  // 发布时间先于展开读取：点击"阅读全文"会触发 DOM 重排，
  // 重排后时间元素可能脱离 scope 导致查询落空（实测问题）
  const timeEl = document.querySelector(
    '.QuestionAnswer-content .ContentItem-time, .AnswerItem .ContentItem-time, .ContentItem-time');
  // 长答案默认折叠：先点开"阅读全文"再提取完整正文
  const expandBtn = scope.querySelector('.RichContent-collapsedText');
  if (expandBtn) {
    expandBtn.click();
    await new Promise(r => setTimeout(r, 800));
  }
  const titleEl = document.querySelector('h1.QuestionHeader-title, h1');
  const title = titleEl ? titleEl.textContent.trim() : '';
  const answerEl = scope.querySelector('.RichContent-inner, .RichText');
  let answer = '';
  // innerText 保留段落级换行（textContent 会拼掉），与采集库格式一致；
  // 知乎正文常带零宽空格（段落级布局符），注入 prompt 前必须剥离
  if (answerEl) answer = answerEl.innerText.trim().replace(/\n{3,}/g, '\n\n').replace(/[\u200b-\u200d\ufeff]/g, '').trim();
  const clean = s => (s || '').replace(/[^\w一-龥 .\-]/g, ' ').replace(/\s+/g, ' ').trim();
  const allBtns = Array.from(scope.querySelectorAll('.ContentItem-actions button, .ContentItem-actions span'));
  const labels = allBtns.map(el => clean(el.textContent)).filter(Boolean);
  const likesEl = scope.querySelector('.VoteButton');
  const likesText = likesEl ? clean(likesEl.textContent) : '';
  const likesMatch = likesText.match(/(\d[\d,]*)/);
  const likes = likesMatch ? parseInt(likesMatch[1].replace(/,/g, ''), 10) : null;
  const commentsMatch = labels.find(l => l.includes('条评论'));
  const comments = commentsMatch ? parseInt(commentsMatch.match(/\d+/)?.[0] || '0', 10) : null;
  const numeric = labels.filter(l => /^\d[\d,]*$/.test(l)).map(l => parseInt(l.replace(/,/g, ''), 10));
  const others = numeric.filter(n => n !== likes);
  const collects = others.length > 0 ? others[0] : null;
  const hearts = others.length > 1 ? others[1] : null;
  const publishTime = timeEl ? timeEl.textContent.trim().replace(/^发布于/, '').trim() : '';
  return { title, answer, footer: { likes, comments, collects, hearts, publish_time: publishTime, answer_url: location.href } };
}
"""

# 作者页答案链接列表：标题 + URL + 互动摘要
_AUTHOR_LINKS_JS = r"""
() => {
  const items = Array.from(document.querySelectorAll('.List-item, .AnswerItem, .ContentItem'));
  const out = [];
  const seen = new Set();
  for (const it of items) {
    const link = it.querySelector('a[href*="/answer/"]');
    if (!link) continue;
    const titleEl = it.querySelector('.ContentItem-title, h2');
    const title = titleEl ? titleEl.textContent.trim() : link.textContent.trim();
    if (seen.has(title) || title.length < 4) continue;
    seen.add(title);
    const text = it.textContent || '';
    const likeMatch = text.match(/([\d,]+)\s*赞同/);
    const commentMatch = text.match(/([\d,]+)\s*条评论/);
    let href = link.getAttribute('href') || '';
    if (href.startsWith('//')) href = 'https:' + href;
    else if (href.startsWith('/')) href = 'https://www.zhihu.com' + href;
    out.push({
      title: title.slice(0, 60),
      href,
      likes: likeMatch ? parseInt(likeMatch[1].replace(/,/g, ''), 10) : null,
      comments: commentMatch ? parseInt(commentMatch[1].replace(/,/g, ''), 10) : null,
    });
  }
  return out;
}
"""

# 推荐页候选：标题 + 纯问题 URL + 互动指标
# 兼容两种候选页：
#   A) 创作中心问题推荐页 /creator/featured-question/recommend
#      （原 workflow 选题入口；行卡片 .ToolsQuestion，问题链接是纯
#       /question/{id}，互动数据形如「N 浏览 · N 回答 · N 万关注」）
#   B) 首页推荐流（.TopstoryItem 卡片，链接指向答案需规整，互动为赞/评论）
# 互动字段统一为 likes/comments/followers，评分公式在 workflow 侧自适应
_RECOMMEND_QUESTIONS_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const clean = s => (s || '').replace(/​-‍﻿/g, '').replace(/\s+/g, ' ');
  const numOf = (text, pat) => {
    const mm = text.match(pat);
    if (!mm) return null;
    // 字符类不含空白：卡片文本「获得过 N 个赞同」中「赞同」后的
    // 换行不能被当作数字（曾因此解析出 NaN 评分）
    const s = mm[1].replace(/[,\s]/g, '');
    const n = parseFloat(s);
    if (isNaN(n)) return null;
    return Math.round(n * (s.includes('万') ? 1e4 : 1));
  };
  const links = Array.from(document.querySelectorAll('a[href*="/question/"]'));
  for (const link of links) {
    const href = (link.getAttribute('href') || '');
    const qm = href.match(/\/question\/(\d+)/);
    if (!qm) continue;
    const qid = qm[1];
    const title = clean(link.textContent);
    if (title.length < 4 || seen.has(qid)) continue;
    seen.add(qid);
    // 容器取文本：首页类卡片用 closest；创作中心行卡片无固定类，
    // 用 link 父级（标题+互动行在同一 div 内）。注意 .ToolsQuestion
    // 是整页容器，绝不能作为文本来源（会把整页数字算到每张卡上）
    let card = link.closest('.TopstoryItem, .List-item, .QuestionItem, .ContentItem');
    if (!card) card = link.parentElement;
    const text = clean(card ? card.innerText : title);
    // 互动数据（创作中心页：浏览/回答/关注；首页：赞同/评论）
    const likes = numOf(text, /(?:赞同|赞)\s*([\d.,万]+)/);
    const comments = numOf(text, /([\d.,万]+)\s*条评论/);
    const answers = numOf(text, /([\d.,万]+)\s*回答/);
    const followers = numOf(text, /([\d.,万]+)\s*万?\s*关注/);
    const isHot = ['飙升', '火爆', '热门'].some(kw => text.includes(kw));
    out.push({
      title: title.slice(0, 60),
      href: 'https://www.zhihu.com/question/' + qid,
      likes, comments, answers, followers, is_hot: isHot,
    });
  }
  return out;
}
"""

def normalize_question_url(href):
    """把知乎链接规整为纯问题 URL（去掉 /answer/ 后缀、协议归一）。
    无法识别时返回 None。"""
    if not href:
        return None
    m = re.search(r"/question/(\d+)", href)
    if not m:
        return None
    return f"https://www.zhihu.com/question/{m.group(1)}"


def normalize_author_url(url):
    """作者页 URL 归一到「回答」列表：/posts（文章）→ /answers（回答）。

    采集管线按回答提取（get_author_answer / extract_answer_id），文章页
    /p/ 链接不在支持范围；用户常粘贴 /posts 链接，自动归一到 /answers
    以免读空、误判为"无文章"。无法识别时原样返回。"""
    if not url:
        return url
    m = re.match(r"^(https?://[^/]+/people/[^/?#]+)/posts(?:[/?#]|$)", url.strip())
    if m:
        return m.group(1) + "/answers"
    return url.strip()


def extract_answer_id(href):
    """从链接提取答案 ID；无法识别（非 /answer/ 链接）返回 None。

    作者采集场景必须用独立回答页 /answer/{aid}：知乎只渲染该作者
    的回答（无排名、无懒加载）。绝不能把链接规整成问题页——那会
    提取到问题下排名第一的回答，而不是该作者的回答。"""
    if not href:
        return None
    m = re.search(r"/answer/(\d+)", href)
    return m.group(1) if m else None


def build_story_record(data, author, source="author_page_dom"):
    """把提取结果构造成采集库记录（补作者/来源/时间戳）。"""
    footer = dict(data.get("footer") or {})
    return {
        "title": (data.get("title") or "").strip(),
        "answer": (data.get("answer") or "").strip(),
        "footer": footer,
        "author": author,
        "source": source,
        "collected_at": time.strftime("%Y-%m-%d"),
    }


