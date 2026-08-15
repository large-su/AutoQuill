# ============================================================
# applications/zhihu_story/browser_adapter.py — DOM 语义化浏览器适配层
#
# 核心目标：浏览器操作与物理鼠标/屏幕完全解绑。
#   - Python 直连 playwright，launch_persistent_context 启动独立 Edge 实例
#     （独立 user-data-dir，不占用用户日常 Edge；登录态存 storage_state）
#   - 所有交互通过 DOM 指令（evaluate / click selector）触发，
#     与分辨率、缩放、鼠标位置无关；运行期间用户可干其他事
#   - 复用本会话验证过的知乎 DOM 提取逻辑（问题页/作者页/推荐页）
#
# 语义接口（与具体网页结构解耦，供 workflows/zhihu.py 调用）：
#   ZhihuBrowser.open_question(url)          → 打开问题页
#   ZhihuBrowser.get_recommend_questions()   → 推荐页候选列表
#   ZhihuBrowser.get_primary_answer(url)     → 问题页首答（正文+互动数据）
#   ZhihuBrowser.get_author_answer_links()   → 作者页全部答案链接
#   ZhihuBrowser.get_author_answer(url)      → 指定作者某篇答案全文
#   ZhihuBrowser.save_storage_state()        → 保存登录态（敏感，gitignored）
#
# 架构位置：Layer 5 (Applications) — 知乎平台浏览器通道（DOM 主通道）
# ============================================================

import json
import logging
import os
import re
import threading
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

_ZHIHU_HOME = "https://www.zhihu.com/"

# 页面交互超时（毫秒）：所有 evaluate/goto 必须有界。
# 历史事故：风控页/加载中页面会让 evaluate 无限阻塞（进程挂死数
# 分钟无日志），任何交互都不允许无界等待。
_EVAL_TIMEOUT = 15000
_NAV_TIMEOUT = 20000
# 浏览器进程启动超时（毫秒）。启动无日志是历史卡死事故的高发段：
# playwright 驱动或 Edge 拉起失败时无任何输出，必须显式超时。
_LAUNCH_TIMEOUT_MS = 60000


class WorkflowCancelled(Exception):
    """用户取消操作（Web 控制台「停止」按钮）。"""


_cancel_hook = None


def set_cancel_hook(fn):
    """设置取消检查钩子；fn() 返回 True 时后续浏览器操作抛 WorkflowCancelled。

    仅 Web 控制台设置（stop 置标志）；CLI 模式下无 hook，零影响。
    检查只允许在 Python 层（浏览器阻塞调用自带超时），绝不能跨线程
    注入异常——那会破坏 Playwright 协议层导致 close 挂起。"""
    global _cancel_hook
    _cancel_hook = fn


def _check_cancel():
    if _cancel_hook is not None and _cancel_hook():
        raise WorkflowCancelled("已由用户停止")


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


class ZhihuBrowser:
    """知乎 DOM 浏览器通道。启动独立 Edge 实例，复用持久化登录态。"""

    def __init__(self, user_data_dir=USER_DATA_DIR,
                 storage_state=STORAGE_STATE_PATH, headless=False):
        self.user_data_dir = user_data_dir
        self.storage_state = storage_state
        self.headless = headless
        self.context = None
        self.page = None

    # ----------------------------------------------------------
    # 生命周期
    # ----------------------------------------------------------

    def start(self):
        """启动持久化上下文，若存在已保存的登录态则自动恢复。
        （持久化 profile 本身也保留 cookie，这里双保险——
        无状态文件时保持全新会话，供首次手动登录。）"""
        from playwright.sync_api import sync_playwright
        t0 = time.time()
        log.info("browser_adapter: 启动浏览器…（Playwright 驱动）")
        self._pw = sync_playwright().start()
        log.info("browser_adapter: 驱动就绪（%.1fs），拉起 Edge 持久化上下文…",
                 time.time() - t0)
        os.makedirs(self.user_data_dir, exist_ok=True)
        if not EDGE_PATH:
            raise RuntimeError(
                "未找到系统 Microsoft Edge！请安装 Edge 后重试"
                "（或设置 AQ_EDGE_PATH 环境变量指向 msedge.exe）")
        try:
            self.context = self._pw.chromium.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                executable_path=EDGE_PATH,
                headless=self.headless,
                locale="zh-CN",
                timeout=_LAUNCH_TIMEOUT_MS,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            # 启动失败：丢弃半初始化驱动，避免残留进程占住 profile 锁
            self._pw = None
            raise
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.load_storage_state()
        log.info("browser_adapter: 浏览器就绪（共 %.1fs）", time.time() - t0)
        return self

    def close(self):
        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
            self.context = None
        _pw = getattr(self, "_pw", None)
        if _pw is not None:
            # start() 半途失败时 _pw 可能是未初始化对象（无 stop），
            # 用 getattr 防护，close 不能再次抛错掩盖原异常
            stop = getattr(_pw, "stop", None)
            if stop:
                try:
                    stop()
                except Exception:
                    pass
            self._pw = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # ----------------------------------------------------------
    # 登录态
    # ----------------------------------------------------------

    def is_logged_in(self):
        """登录检测：以知乎登录凭证 cookie z_c0 为准（httpOnly，
        DOM 选择器会随改版失效，cookie 检测与页面结构无关）。"""
        cookies = self.context.cookies(_ZHIHU_HOME)
        return any(c["name"] == "z_c0" and c.get("value") for c in cookies)

    def save_storage_state(self, path=None):
        """把当前登录态保存到本地文件（含会话 Cookie，勿提交 git）。"""
        path = path or self.storage_state
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.context.storage_state(), f, ensure_ascii=False)
        log.info("browser_adapter: 登录态已保存 → %s", path)

    def load_storage_state(self, path=None):
        """从本地文件恢复登录态；文件不存在时返回 False（需手动登录一次）。"""
        path = path or self.storage_state
        if not os.path.exists(path):
            log.info("browser_adapter: 无登录态文件 %s，需手动登录一次", path)
            return False
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        self.context.add_cookies(state.get("cookies", []))
        log.info("browser_adapter: 已恢复登录态（%d 条 cookie）",
                 len(state.get("cookies", [])))
        return True

    # ----------------------------------------------------------
    # 底层：有界页面交互
    # ----------------------------------------------------------

    def _safe_evaluate(self, js, *args, timeout=_EVAL_TIMEOUT):
        """执行页面 JS，失败返回 None；JS 内部带自限时哨兵。

        所有页面交互都必须走这里。Playwright 1.62 的 evaluate 不支持
        timeout 参数（协议层无超时），且 sync API 有线程亲和性（不能
        从其他线程调用）。对策：把调用 JS 包进 Promise.race 自限时
        哨兵——页面主线程存活时，任何挂起的 evaluate（fetch 不返回、
        慢导航等）都会在 timeout 后被哨兵截断返回 None，不阻塞流程。
        渲染进程彻底卡死（极端风控）时此层无效，由调用方（E2E runner）
        的进程级看门狗兜底。"""
        wrapped = (
            "async function() {"
            "  const _fn = " + js + ";"
            "  const _timeout = new Promise(_r => setTimeout("
            f"() => _r({{__aq_timeout__: true}}), {int(timeout)}));"
            "  const _result = await Promise.race("
            "    [Promise.resolve(_fn.apply(null, arguments)), _timeout]);"
            "  if (_result && _result.__aq_timeout__) return null;"
            "  return _result;"
            "}"
        )
        _check_cancel()
        try:
            return self.page.evaluate(wrapped, *args)
        except WorkflowCancelled:
            raise
        except Exception as exc:
            log.warning("browser_adapter: evaluate 失败：%s", exc)
            return None

    # ----------------------------------------------------------
    # 语义接口：选题
    # ----------------------------------------------------------

    def open_recommend_page(self, url=None):
        """打开选题候选页：默认创作中心「推荐问题」（原 workflow 入口，
        候选池为「等你来答」的优质问题，对写作选题对口；首页推荐流
        为全品类大杂烩，已弃用为默认）。

        也可传创作中心「邀请回答」页 URL（选题来源 QUESTION_SOURCE
        切换为 invited 时传入），两页同构（.ToolsQuestion 行卡片）。"""
        if url is None:
            from applications.zhihu_story.config import ZHIHU_RECOMMEND_URL
            url = ZHIHU_RECOMMEND_URL
        _check_cancel()
        self.page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
        time.sleep(1.5)
        # 瀑布流首屏常在滚动后补充渲染，滚动一次触发
        try:
            self._safe_evaluate("() => window.scrollBy(0, 800)")
            time.sleep(0.8)
        except WorkflowCancelled:
            raise
        except Exception:
            pass
        return self

    def get_recommend_questions(self, max_cards=30):
        """返回推荐页候选：[{title, href, likes, comments}]，href 为纯问题 URL"""
        items = self._safe_evaluate(_RECOMMEND_QUESTIONS_JS) or []
        cleaned = []
        for it in items:
            url = normalize_question_url(it.get("href"))
            if url:
                it["href"] = url
                cleaned.append(it)
        return cleaned[:max_cards]

    # ----------------------------------------------------------
    # 语义接口：问题页提取
    # ----------------------------------------------------------

    def open_question(self, url, force=False):
        """进入问题页；已在同一问题页时默认跳过重载（真幂等）。

        goto 同一 URL 会整页重载（空白加载 + 触发风控的概率），提取
        流程会多次重进同一问题，幂等跳过避免重复导航。force=True
        强制重新导航——发布前页面已闲置数分钟（生成耗时），强制
        一次定位到目标 URL 更可靠，也符合「发布只跳一次」的预期。"""
        target = normalize_question_url(url) or url
        current = normalize_question_url(self.page.url)
        if not force and current and target and current == target:
            try:
                # 同页幂等：滚动触发懒加载等正文容器（不整页重载）。
                # 固定 8s 干等对冷加载必失败（正文不滚动不渲染）
                self._wait_answer_container(timeout=8)
            except WorkflowCancelled:
                raise
            except Exception:
                pass
            return self
        _check_cancel()
        self.page.goto(target, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
        try:
            if not self._wait_answer_container(timeout=8):
                log.warning("browser_adapter: 问题页正文容器未在 8s 内出现，继续")
        except WorkflowCancelled:
            raise
        except Exception:
            pass
        time.sleep(0.5)
        return self

    def _wait_answer_container(self, timeout=15):
        """轮询等待首答容器出现；每次轮询前向下滚动触发懒加载。

        页面冷加载/慢网络时容器延迟渲染，单次 8s 等待经常落空，
        导致首答被误判为过短而降级 OCR；且知乎问题页不滚动就不
        渲染首答（实测：刚进入只有骨架，下滑后才加载）。

        循环：检测 → 无则下滑触发渲染 → 等渲染完成（轮询检测，
        最多 ~2s）→ 滑回原位 → 再检测。回位是关键：一直下滑会
        触发无限滚动不断加载更多回答（页面越拖越长、首答 scope
        漂移），回位后只保留首屏已渲染的内容。

        渲染窗口：曾固定下滑后 1s 即回位——知乎懒加载渲染需要
        更久，回位时内容还没渲染出来，检测永远落空、15s 超时。
        现在下滑后轮询等容器出现（快的页面几百 ms 即返回），
        渲染成功再回位（已渲染的 DOM 不因回位消失）。"""
        selector = ("'.QuestionAnswer-content, .AnswerItem, "
                    ".RichContent-inner'")
        deadline = time.time() + timeout
        start = time.time()
        last_log = 0.0
        while time.time() < deadline:
            _check_cancel()
            if self._safe_evaluate(
                    f"() => !!document.querySelector({selector})"):
                return True
            # 下滑触发懒加载：分段小步滚动 + 间隔（模拟人手滚轮）。
            # 一次性 scrollBy(0,600) 是瞬间大跳，知乎懒加载有时不
            # 触发（快速滚动被跳过/防抖）；连续小段滚动产生多次
            # scroll 事件，渲染更可靠。6×100px，每段间隔 60ms，
            # 总耗时 ~360ms 的连续下滑过程。
            self._safe_evaluate(
                "async () => {"
                "  const steps = 6, stepPx = 100, delayMs = 60;"
                "  for (let i = 0; i < steps; i++) {"
                "    window.scrollBy(0, stepPx);"
                "    await new Promise(r => setTimeout(r, delayMs));"
                "  }"
                "  return true;"
                "}"
            )
            # 渲染窗口：轮询等容器出现，最多 ~2s（间隔 500ms×4）
            rendered = False
            for _ in range(4):
                _check_cancel()
                if self._safe_evaluate(
                        f"() => !!document.querySelector({selector})"):
                    rendered = True
                    break
                self.page.wait_for_timeout(500)
            # 滑回原位：避免触发无限加载更多回答
            self._safe_evaluate("() => { window.scrollTo(0, 0); return true; }")
            self.page.wait_for_timeout(400)
            if rendered:
                return True
            # 进度日志：此循环可能长达 15s，无日志会让用户误以为卡住
            now = time.time()
            if now - last_log >= 5:
                last_log = now
                log.info("browser_adapter: 等待首答渲染… 已等 %.0fs/%ds"
                         "（下滑触发懒加载）", now - start, timeout)
        return False

    def _answer_text_len(self):
        """首答容器当前正文长度（就绪流程的稳定判据）。"""
        n = self._safe_evaluate(
            "() => { const el = document.querySelector("
            "'.QuestionAnswer-content .RichContent-inner, "
            ".AnswerItem .RichContent-inner, .RichContent-inner');"
            " return el ? el.innerText.length : 0; }")
        return n or 0

    def _settle_answer_page(self, timeout=15):
        """首答就绪流程：展开第一个回答的「阅读全文」→ 等正文稳定。

        首答已由 _wait_answer_container 确认渲染；长回答默认折叠，
        展开后还会渐进加载。循环：只点开第一个回答容器内的展开
        按钮，轮询正文长度连续两轮不变视为就绪。不做任何滚动。
        返回就绪时的正文长度（0 = 始终未见）。"""
        deadline = time.time() + timeout
        start = time.time()
        last_log = 0.0
        last_len, stable = 0, 0
        while time.time() < deadline:
            _check_cancel()
            self._safe_evaluate(_EXPAND_FIRST_COLLAPSED_JS)
            self.page.wait_for_timeout(700)
            cur = self._answer_text_len()
            if cur > 0 and cur == last_len:
                stable += 1
                if stable >= 2:
                    return cur
            else:
                stable = 0
            last_len = cur
            # 进度日志：展开后正文渐进加载可能拖满 15s，无日志易误判卡住
            now = time.time()
            if now - last_log >= 5:
                last_log = now
                log.info("browser_adapter: 首答就绪中… 已等 %.0fs/%ds"
                         "（展开阅读全文，正文稳定检测）", now - start, timeout)
        return last_len

    def get_primary_answer(self, url=None, min_length=100, retries=2):
        """提取问题页首答。返回 {title, answer, footer}；不合格返回 None。

        容器缺失或首答过短时重试（含页面 reload 兜底）：首屏可能
        渲染失败或加载慢，单次判定会把 DOM 主通道误判为不可用而
        降级 OCR。"""
        if url:
            self.open_question(url)
        for attempt in range(retries + 1):
            if self._wait_answer_container(timeout=15):
                # 就绪流程：展开首答阅读全文 → 正文稳定后再提取
                self._settle_answer_page(timeout=15)
                data = self._safe_evaluate(_PRIMARY_ANSWER_JS) or {}
                answer = (data.get("answer") or "").strip()
                if len(answer) >= min_length:
                    return {
                        "title": (data.get("title") or "").strip(),
                        "answer": answer,
                        "footer": data.get("footer") or {},
                    }
                log.warning("browser_adapter: 首答过短（%d 字），重试",
                            len(answer))
            if attempt < retries:
                self.page.reload(wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
                self.page.wait_for_timeout(1500)
        log.warning("browser_adapter: 重试 %d 次后仍无合格首答，放弃",
                    retries)
        return None

    # ----------------------------------------------------------
    # 语义接口：作者页采集
    # ----------------------------------------------------------

    def get_author_answer_links(self, author_page_url):
        """作者主页 → 全部答案链接：[{title, href, likes, comments}]"""
        self.page.goto(author_page_url, wait_until="domcontentloaded",
                       timeout=_NAV_TIMEOUT)
        try:
            self.page.wait_for_selector(".List-item, .AnswerItem", timeout=8000)
        except Exception:
            log.warning("browser_adapter: 作者页答案列表未出现，继续")
        time.sleep(0.5)
        return self._safe_evaluate(_AUTHOR_LINKS_JS) or []

    def get_author_answer(self, answer_url, author, min_length=100):
        """打开该作者某篇答案的独立回答页，提取回答全文。

        链接形如 /question/{qid}/answer/{aid}；独立回答页 /answer/{aid}
        只渲染该作者的回答——不存在问题页「排名第一」问题，正文也
        立即在 DOM（不触发问题页懒加载）。无法识别 aid 时退回原链接。
        返回 {title, answer, footer}；不合格返回 None。"""
        aid = extract_answer_id(answer_url)
        target = f"https://www.zhihu.com/answer/{aid}" if aid else answer_url
        _check_cancel()
        self.page.goto(target, wait_until="domcontentloaded",
                       timeout=_NAV_TIMEOUT)
        try:
            if not self._wait_answer_container(timeout=15):
                log.warning("browser_adapter: 回答页正文容器未出现，继续")
        except WorkflowCancelled:
            raise
        except Exception:
            pass
        self._settle_answer_page(timeout=15)
        data = self._safe_evaluate(_PRIMARY_ANSWER_JS) or {}
        answer = (data.get("answer") or "").strip()
        if len(answer) < min_length:
            log.warning("browser_adapter: 答案过短（%d 字），跳过", len(answer))
            return None
        return {
            "title": (data.get("title") or "").strip(),
            "answer": answer,
            "footer": data.get("footer") or {},
        }

    # ----------------------------------------------------------
    # 语义接口：可回答性检测（替代 OCR 查「撤销删除」）
    # ----------------------------------------------------------

    def check_answerable(self):
        """DOM 检测当前问题是否可回答（替代 OCR 找「撤销删除」）。

        硬信号1：页面出现「撤销删除」→ 曾删过回答，绝不能回答。
        硬信号2：页面出现「查看我的回答」→ 本账号已发布过回答，
                无写回答入口，不能重复发布。
        软信号：「写回答」按钮存在 → 可回答。
        两者都无 → 默认可回答（与旧 OCR 语义一致，宁采后弃不前置挡）。
        返回 (can_answer, reason)。
        """
        has_undo = self._safe_evaluate(
            "() => document.body && document.body.innerText.includes('撤销删除')")
        if has_undo:
            return False, "检测到「撤销删除」——此问题下曾删除过回答，跳过"
        has_answered = self._safe_evaluate(
            "() => document.body && document.body.innerText.includes('查看我的回答')")
        if has_answered:
            return False, "检测到「查看我的回答」——此问题下已发布过回答，跳过"
        has_write = self._safe_evaluate("""(texts) =>
          Array.from(document.querySelectorAll('button'))
            .some(e => texts.includes(e.textContent
                        .replace(/[\\u200b-\\u200d\\ufeff]/g, '').trim()))""",
            list(self._WRITE_BUTTON_TEXTS))
        if has_write:
            return True, "检测到可写入口（写回答/编辑回答），可回答"
        return True, "未检测到禁止信号，默认可回答"

    # ----------------------------------------------------------
    # 语义接口：发布（导入文档到编辑器）
    # ----------------------------------------------------------

    _WRITE_BUTTON_TEXTS = ("写回答", "编辑回答")

    def _extract_question_id(self, url=None):
        m = re.search(r"/question/(\d+)", url or self.page.url)
        return m.group(1) if m else None

    def get_draft_content(self, question_id=None):
        """拉取服务端草稿正文（content 字段在响应顶层）。

        前端「草稿已保存」toast 在程序化上传后可能不出现、导入面板
        ModalLoading 也可能卡住（知乎前端缺陷），服务端草稿是否落盘
        以本 API 为准——发布成功判定都走这里。"""
        qid = question_id or self._extract_question_id()
        if not qid:
            return ""
        return self._safe_evaluate(
            """(qid) => fetch('/api/v4/questions/' + qid + '/draft',
                            {credentials: 'include'})
                .then(r => r.ok ? r.json() : null)
                .then(d => (d && d.content) || '')""", qid) or ""

    def wait_draft_content(self, marker, timeout=30):
        """轮询草稿 API 直到服务端草稿包含 marker 片段（保存确认）。

        marker 由 build_draft_marker 生成（剥空白）。服务端草稿是 HTML
        （段落 \n\n 渲染为 <br><br>），匹配前剥标签+空白，否则跨段
        marker 永远匹配不上。"""
        deadline = time.time() + timeout
        start = time.time()
        last_log = 0.0
        while time.time() < deadline:
            html = self.get_draft_content()
            plain = re.sub(r"<[^>]+>", "", html)
            if marker in re.sub(r"\s+", "", plain):
                return True
            # 进度日志：草稿确认最长等 60s，全程无日志会让用户干等
            now = time.time()
            if now - last_log >= 10:
                last_log = now
                log.info("browser_adapter: 等待服务端草稿确认… 已等 %.0fs/%ds"
                         "（草稿 API 轮询）", now - start, timeout)
            self.page.wait_for_timeout(2000)
        return False

    def _find_write_button(self, timeout=12):
        """查找并点击「写回答/编辑回答」按钮（DOM 直点）。带轮询重试：
        长耗时阶段（如生成故事）后页面 reload 可能较慢，单次
        evaluate 容易落在未就绪状态。

        ★ 关键：该问题下已有草稿时，知乎显示「编辑回答」而非
        「写回答」——两者都是打开编辑器的入口，必须都接受。"""
        deadline = time.time() + timeout
        start = time.time()
        last_log = 0.0
        while time.time() < deadline:
            clicked = self._safe_evaluate("""(texts) => {
              const clean = s => s.replace(/[\\u200b-\\u200d\\ufeff]/g, '').trim();
              const btn = Array.from(document.querySelectorAll('button'))
                .find(e => texts.includes(clean(e.textContent || '')));
              if (!btn) return false;
              btn.click();
              return true;
            }""", list(self._WRITE_BUTTON_TEXTS))
            if clicked:
                return True
            # 进度日志：生成长耗时后页面可能渲染慢，等待窗口可达 20s
            now = time.time()
            if now - last_log >= 5:
                last_log = now
                log.info("browser_adapter: 定位「写回答/编辑回答」按钮…"
                         " 已等 %.0fs/%ds", now - start, timeout)
            self.page.wait_for_timeout(1000)
        return False

    def _dump_page_state(self, tag):
        """失败诊断：把当前页面状态写进日志（URL/标题/按钮/正文开头）。

        发布偶发「找不到写回答按钮」——原因可能是 SPA 漂移、会话弹窗
        或风控空壳页。没有现场信息只能盲猜，dump 让下一次失败可诊断。"""
        try:
            state = self._safe_evaluate(
                """() => {
                  const clean = s => (s||'').replace(
                    /[\\u200b-\\u200d\\ufeff]/g,'').trim();
                  return {
                    url: location.href,
                    title: (document.title || '').slice(0, 80),
                    buttons: Array.from(document.querySelectorAll('button'))
                      .map(e => clean(e.textContent)).filter(Boolean).slice(0, 15),
                    bodyHead: (document.body ? document.body.innerText : '')
                      .replace(/\\n+/g, ' | ').slice(0, 160)
                  };
                }""")
            log.warning("browser_adapter: 页面状态[%s] url=%s title=%s",
                        tag, state.get("url"), state.get("title"))
            log.warning("browser_adapter: 页面状态[%s] buttons=%s",
                        tag, state.get("buttons"))
            log.warning("browser_adapter: 页面状态[%s] body=%s",
                        tag, state.get("bodyHead"))
        except Exception as e:
            log.warning("browser_adapter: 页面状态 dump 失败[%s]: %s", tag, e)

    def publish_story(self, story, question_url=None, max_wait=60):
        """发布（编辑器写回答通道）：打开编辑器 → 清空旧草稿 → 富文本粘贴。

        写入通道：md → HTML 转换 + 剪贴板富文本 + 真实 Ctrl+V。知乎
        编辑器是 Draft.js，粘贴富文本时按块解析，`<b>`/`<p>` 能真实
        落盘（实测确认）；fill 纯文本会把 `## **1**` 符号原样写进草稿。

        成功判定：轮询服务端草稿 API（前端保存提示 toast 在程序化
        写入后可能不出现，以服务端草稿内容为准——可验证）。

        ★ 不采用「导入文档 → 文件上传」路径：上传 API 全 200 但服务端
        草稿不更新（知乎程序化导入落盘不可靠，仅空草稿时偶发成功），
        且导入同样不转换 md 符号。

        返回 True 表示服务端草稿已确认包含故事全文，False 表示超时。
        """
        if not self._find_write_button(timeout=20):
            # 生成长耗时后重新导航，页面可能渲染慢/空壳：
            # 先 dump 现场再兜底。★ reload 只重载「当前 URL」——若
            # SPA 已漂移到别处等于重载错误页面；有目标 URL 时优先
            # goto 强制回到问题页，仍失败才报错
            self._dump_page_state("button-not-found")
            log.warning("browser_adapter: 首次未定位「写回答」按钮，"
                        "重新导航重试")
            if question_url:
                self.page.goto(question_url, wait_until="domcontentloaded",
                               timeout=_NAV_TIMEOUT)
            else:
                self.page.reload(wait_until="domcontentloaded",
                                 timeout=_NAV_TIMEOUT)
            self.page.wait_for_timeout(2000)
            if not self._find_write_button(timeout=15):
                self._dump_page_state("button-not-found-retry")
                raise RuntimeError(
                    "未定位「写回答」按钮（页面可能已发布过回答，"
                    "或无写回答入口）")
        try:
            self.page.wait_for_selector(
                '[contenteditable="true"], .AnswerForm-editor', timeout=10000)
        except Exception:
            raise RuntimeError("编辑器未出现")

        # 清空旧草稿：编辑器打开时自动加载已有草稿，先全选删除，
        # 避免新故事与旧内容拼接
        self._safe_evaluate("() => { document.execCommand('selectAll'); }")
        self.page.keyboard.press("Delete")
        self.page.wait_for_timeout(400)

        editor = self.page.locator(
            '.AnswerForm-editor [contenteditable="true"], '
            '[contenteditable="true"]').first
        plain = clean_story_markdown(story)
        self._paste_rich(editor, story_markdown_to_html(story), plain)

        marker = build_draft_marker(plain or "")
        if not marker:
            raise RuntimeError("故事内容为空，拒绝发布")
        return self.wait_draft_content(marker, timeout=max_wait)

    def _paste_rich(self, editor, html, plain):
        """剪贴板富文本 + 真实 Ctrl+V 写入编辑器。

        Draft.js 编辑器只把「粘贴事件」当富文本处理（fill 纯文本写入
        不会解析格式）。先经 navigator.clipboard 写入 text/html +
        text/plain，再派发真实粘贴键事件；权限按当前站点授予。"""
        origin = re.match(r"^(https?://[^/]+)", self.page.url)
        try:
            if origin:
                self.page.context.grant_permissions(
                    ["clipboard-read", "clipboard-write"], origin=origin.group(1))
            self._safe_evaluate(
                """([h, p]) => navigator.clipboard.write([
                    new ClipboardItem({
                      'text/html': new Blob([h], {type: 'text/html'}),
                      'text/plain': new Blob([p], {type: 'text/plain'})
                    })
                  ]).then(() => true)""", [html, plain])
            self.page.wait_for_timeout(800)
        except Exception:
            # 剪贴板不可用（权限/环境）时降级纯文本，保证流程不断
            log.warning("browser_adapter: 剪贴板富文本写入失败，"
                        "降级纯文本写入")
            editor.fill(plain)
            return
        editor.focus()
        self.page.keyboard.press("Control+V")

    # ----------------------------------------------------------
    # 语义接口：批量采集
    # ----------------------------------------------------------

    def open_new_page(self, url=None):
        """新开一个页面（替代中键新开 tab）。"""
        page = self.context.new_page()
        if url:
            page.goto(url, wait_until="domcontentloaded")
        return page

    def switch_page(self, page):
        """切换当前操作页面（批量采集新开 tab 后指向新页）。"""
        self.page = page

    def close_page(self, page):
        try:
            page.close()
        except Exception:
            pass

    def scroll_feed(self, pixels=1500):
        """推荐页滚动加载更多：JS 滚动窗口，与键盘/鼠标解绑。"""
        self._safe_evaluate(f"() => window.scrollBy(0, {int(pixels)})")
        self.page.wait_for_timeout(1200)

    # ----------------------------------------------------------
    # 底层工具（供发布环节等扩展使用）
    # ----------------------------------------------------------

    def eval_js(self, js, *args):
        return self._safe_evaluate(js, *args)

    def click(self, selector=None, text=None):
        """DOM 直点：在页面 JS 上下文内直接触发原生 click 事件。
        不经过坐标命中测试 —— 不受遮挡、滚动、分辨率影响，
        真正与鼠标/视图解绑（playwright 的 page.click 仍会做坐标
        命中测试，遇遮挡即失败，故不用）。
        selector 为 CSS 选择器；text 为按钮文本（精确匹配）。"""
        if text is not None:
            clicked = self._safe_evaluate("""(text) => {
              // 知乎按钮文本常带零宽空格(​)，trim 不去除，需先剥离
              const clean = s => s.replace(/[\\u200b-\\u200d\\ufeff]/g, '').trim();
              const el = Array.from(document.querySelectorAll('button'))
                .find(e => clean(e.textContent || '') === text);
              if (!el) return false;
              el.click();
              return true;
            }""", text)
            if not clicked:
                raise ValueError(f"未找到文本为 {text!r} 的按钮")
        else:
            clicked = self._safe_evaluate("""(sel) => {
              const el = document.querySelector(sel);
              if (!el) return false;
              el.click();
              return true;
            }""", selector)
            if not clicked:
                raise ValueError(f"选择器 {selector!r} 未匹配到元素")
        return True

    def get_text(self, selector, default=""):
        els = self.page.query_selector_all(selector)
        return "\n".join(e.text_content().strip() for e in els if e.text_content()) or default


# ----------------------------------------------------------
# 模块级浏览器单例：workflow 各阶段共享同一浏览器实例
# ----------------------------------------------------------

_shared_browser = None
_browser_lock = threading.Lock()  # 懒启动串行化：并发 get_browser 不再互抢 profile


def get_browser():
    """获取全局共享的 ZhihuBrowser（懒启动，线程安全）。

    headless 每次动态读取 config.BROWSER_HEADLESS——Web 控制台切换
    「调试/工作模式」后下一次任务启动即生效。
    锁内先 start() 成功才落盘 _shared_browser：并发调用时后到的线程
    不会拿半初始化实例（线上：并发启动互抢同一 profile，坏实例
    context=None 永久复用于登录引导 → 'NoneType' new_page）。"""
    global _shared_browser
    with _browser_lock:
        if _shared_browser is None or _shared_browser.context is None:
            from config import BROWSER_HEADLESS
            candidate = ZhihuBrowser(headless=BROWSER_HEADLESS)
            candidate.start()  # 失败抛异常，_shared_browser 保持 None 可重试
            _shared_browser = candidate
            log.info("browser_adapter: 浏览器模式：%s",
                     "无头（工作模式）" if BROWSER_HEADLESS
                     else "前台（调试模式）")
        return _shared_browser


def close_shared_browser():
    global _shared_browser
    with _browser_lock:
        if _shared_browser is not None:
            _shared_browser.close()
            _shared_browser = None


_DEEPSEEK_COOKIE_DOMAINS = ("deepseek.com",)


def _has_deepseek_cookies(context):
    try:
        cookies = context.cookies()
    except Exception:
        return False
    return any(
        c.get("domain") and any(
            d in c["domain"] for d in _DEEPSEEK_COOKIE_DOMAINS)
        and c.get("value")
        for c in cookies
    )


def web_llm_logged_in():
    """网页版 LLM（chat.deepseek.com）是否真实可登录。

    判定 = deepseek.com cookie 存在 + 加载 chat.deepseek.com 未停在
    登录页（URL 无 sign_in）。仅查 cookie 会假阳性：过期/无效 cookie
    残留时预检放行，运行才撞登录页（线上：切 Web 成功但运行报
    「找不到 DeepSeek 输入框」，页面停在 chat.deepseek.com/sign_in）。

    用独立无头实例检查，不碰共享浏览器（get_browser）——首启引导轮询
    setup/status 时不会反复弹出可见 Edge，也不影响任务浏览器的无头模式。
    与共享浏览器用同一把 _browser_lock 串行启动：两者都依赖
    USER_DATA_DIR 的持久化 cookie（临时目录读不到登录态），而
    Chromium 单例锁禁止同目录并发——串行化后登录引导不再被此
    检查挤掉（线上：Target page, context or browser has been closed）。
    返回 True/False；浏览器无法启动等异常返回 False（不阻塞引导）。"""
    try:
        with _browser_lock:
            with ZhihuBrowser(headless=True) as browser:
                if not _has_deepseek_cookies(browser.context):
                    return False
                page = browser.context.new_page()
                try:
                    page.goto("https://chat.deepseek.com",
                              wait_until="domcontentloaded", timeout=20000)
                    # 等 SPA 跳转定局：已登录 → URL 稳定即返回（省 1.2s
                    # 固定等待）；未登录 → 一旦跳到 /sign_in 立即判定
                    deadline = time.time() + 1.5
                    prev = page.url
                    if "sign_in" in prev:
                        return False
                    while time.time() < deadline:
                        page.wait_for_timeout(250)
                        cur = page.url
                        if "sign_in" in cur:
                            return False
                        if cur == prev:
                            return True
                        prev = cur
                    return "sign_in" not in page.url
                finally:
                    page.close()
    except Exception:
        return False


def login_deepseek_web_flow(timeout=300):
    """打开可见 Edge 到 chat.deepseek.com，等待用户登录网页版 LLM。

    供首启引导（/api/setup/web-login）使用；登录后 cookie 写入持久化
    profile，web_llm_logged_in() 即可判定。返回 (是否成功, 提示信息)。
    登录完成判定 = cookie 存在 + 页面不在登录页（仅 cookie 会因残留
    假阳性，导致引导秒过但实际未登录）。
    独立可见实例 + 全程持 _browser_lock：
      - 共享浏览器（get_browser）归任务线程创建/使用，登录线程跨线程
        复用会触发 Playwright「cannot switch to a different thread」
        （线上：登录线程退出后再次点击登录即报错）
      - 锁内独占持久化 profile，避免与其他浏览器实例并发互杀"""
    try:
        with _browser_lock:
            with ZhihuBrowser(headless=False) as browser:
                page = browser.context.new_page()
                try:
                    page.goto("https://chat.deepseek.com",
                              wait_until="domcontentloaded",
                              timeout=30000)
                    deadline = time.time() + timeout
                    while time.time() < deadline:
                        time.sleep(3)
                        page.wait_for_timeout(500)  # 等 SPA 跳回主页
                        if (_has_deepseek_cookies(browser.context)
                                and "sign_in" not in page.url):
                            return True, "检测到登录成功"
                    return False, f"超时（{timeout // 60} 分钟）未检测到登录"
                finally:
                    page.close()
    except Exception as exc:
        return False, f"登录引导失败：{exc}"


def login_zhihu_flow(timeout=300):
    """打开可见 Edge 窗口引导用户手动登录知乎，检测到登录后保存登录态。

    供 CLI（--login）与 Web 首启引导（/api/setup/zhihu-login）共用。
    返回 (是否成功, 提示信息)。独立实例 + 持 _browser_lock（与
    login_deepseek_web_flow 同理：不碰共享浏览器、独占 profile）。"""
    with _browser_lock:
        with ZhihuBrowser(headless=False) as browser:
            if browser.is_logged_in():
                browser.save_storage_state()
                return True, "已登录，登录态已保存"
            browser.page.goto("https://www.zhihu.com/signin",
                              wait_until="domcontentloaded")
            deadline = time.time() + timeout
            while time.time() < deadline:
                time.sleep(3)
                if browser.is_logged_in():
                    break
            else:
                return False, f"超时（{timeout // 60} 分钟）未检测到登录"
            browser.save_storage_state()
            return True, "检测到登录成功"


def main():
    """CLI：python -m applications.zhihu_story.browser_adapter --check-login
    或 --collect-author <作者页URL> --author 镜中花"""
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

    parser = argparse.ArgumentParser(description="知乎 DOM 浏览器通道")
    parser.add_argument("--check-login", action="store_true", help="检查登录态")
    parser.add_argument("--login", action="store_true",
                        help="打开浏览器等待手动登录，登录成功后保存登录态")
    parser.add_argument("--collect-author", metavar="URL", default="",
                        help="作者主页 URL，采集其全部答案")
    parser.add_argument("--author", default="", help="作者名（写入采集库）")
    parser.add_argument("--save-state", action="store_true",
                        help="登录后保存登录态")
    args = parser.parse_args()

    with ZhihuBrowser() as browser:
        if args.check_login:
            logged = browser.is_logged_in()
            print(f"登录态：{'已登录' if logged else '未登录'}")
            if logged and args.save_state:
                browser.save_storage_state()
            return

        if args.login:
            ok, msg = login_zhihu_flow()
            print(msg)
            if not ok:
                sys.exit(1)
            print(f"登录态已保存 → {browser.storage_state}")
            return

        if args.collect_author:
            if not browser.is_logged_in():
                print("❌ 未登录知乎，请先手动登录（--check-login 打开后登录一次）")
                sys.exit(1)
            links = browser.get_author_answer_links(args.collect_author)
            print(f"作者页发现 {len(links)} 篇答案")
            for link in links[:10]:
                print(f"  [{link['likes'] or 0:>4}赞] {link['title']}")

            from applications.zhihu_story.author_profiler import (
                load_author_stories, STORY_LIB)
            existing = load_author_stories(args.author or "")
            seen_titles = {s["title"] for s in existing}
            new_count = 0
            with open(STORY_LIB, "a", encoding="utf-8") as f:
                for link in links:
                    if link["title"] in seen_titles:
                        continue
                    data = browser.get_author_answer(link["href"], args.author)
                    if not data:
                        continue
                    rec = build_story_record(data, args.author)
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    seen_titles.add(rec["title"])
                    new_count += 1
                    print(f"  ✓ 新采集：{rec['title'][:30]}（{len(rec['answer'])}字）")
            print(f"完成：新增 {new_count} 篇")
            return

        parser.print_help()


if __name__ == "__main__":
    main()
