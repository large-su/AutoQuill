# ============================================================
# tools/ds_history_cleanup.py — DeepSeek 历史会话审计与清理
#   （清掉 AutoQuill 写故事链路留下的旧会话，防风控/封号）
#
# 背景：v4.9.0 起「单条链路只开一个会话 + 完成后自动删除」已上线，
# 但上线之前在 DeepSeek 网页端生成的成百上千条会话仍堆在账号里
# （用户实测：生成多了被平台警告乃至封号）。本工具复用同一套登录态
# 把「30 天前的写故事链路残留」找出来，先出统计报告 + 正文备份，
# 二次确认后再批量删除。
#
# 站点链路（2026-09-15 真机只读探测确认，见 tools/archive/probes/
# probe_ds_session_list.py / probe_ds_history_api.py）：
#   列表  GET  /api/v0/chat_session/fetch_page
#              ?lte_cursor.pinned=false[&lte_cursor.updated_at=<ts>]
#         → biz_data.chat_sessions[]（每页 100 条，has_more 指示还有没有）
#         字段：id / seq_id / title / title_type / model_type / pinned
#               / inserted_at / updated_at（unix 秒，float）
#   内容  GET  /api/v0/chat/history_messages?chat_session_id=<id>
#         → biz_data.chat_messages[]（role / content / thinking_content …）
#   删除  POST /api/v0/chat_session/delete  body {"chat_session_ids": [...]}
#   新建  POST /api/v0/chat_session/create  body {}（仅 smoke 自检用）
#   鉴权  Authorization: Bearer <页面 localStorage.userToken.value>
#
# 判定（两道闸，缺一不删）：
#   ① 时间：最后活动（updated_at）早于 --days 天（默认 30）
#   ② 内容：拉取该会话消息，匹配 AutoQuill 提示词指纹（story_prompt.py /
#      applications/zhihu_story/prompts.py 里的签名短语，跨版本收录）
#      标题关键词只用于决定「值不值得拉内容」，本身不作为删除依据
#   ③ 默认跳过置顶会话（用户手动置顶的可能是自己要留的）
#
# 用法（项目根目录，Windows）：
#   # ① 只读扫描，出统计报告（不删任何东西）
#   PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/ds_history_cleanup.py scan
#   ... scan --days 30 --all-content    # 旧会话全拉内容确认（最保守，慢）
#   ... scan --headed                   # 弹浏览器排查登录态
#   # ② 按报告删除（不带 --yes 只打印计划 = dry-run）
#   ... delete --report data/cleanup/scan_20260915_162000.json --yes
#   # ③ 删除链路自检（自建一条空会话再用同一链路删掉，不碰历史数据）
#   ... smoke
#
# 安全边界：
#   - scan 全程只读（只 GET 列表与消息）
#   - delete 默认 dry-run；--yes 才真删，且删除前逐条备份正文到
#     data/cleanup/backup_<ts>/（备份失败的那条不删）
#   - 连续 --max-consecutive-fail 次删除失败立即中止（防风控）
#   - 只动判定为 confirmed 的会话；绝不碰未命中/新会话/置顶会话
# ============================================================

import argparse
import json
import os
import sys
import time
from datetime import datetime
from urllib.parse import urlencode

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SITE = "https://chat.deepseek.com/"
LIST_PATH = "/api/v0/chat_session/fetch_page"
HISTORY_PATH = "/api/v0/chat/history_messages"
DELETE_PATH = "/api/v0/chat_session/delete"
CREATE_PATH = "/api/v0/chat_session/create"

MAX_LIST_PAGES = 200        # 分页上限（200 × 100 = 2 万条，防御死循环）
CONFIRM_WEIGHT = 2          # 指纹总权重 ≥ 该值 → confirmed

# 站点「客户端头」缺省值（仅在页面捕获失败时兜底）。
# ★ 2026-09-15 实测：不带 x-client-* 头时 fetch_page 会忽略分页游标，
#   每次都返回第一页（表现为「翻了 7 页只有 100 条」）——这些头由
#   页面自身的请求在 open() 时捕获，版本升级自动跟随。
DEFAULT_CLIENT_HEADERS = {
    "accept": "*/*",
    "referer": SITE,
    "x-client-platform": "web",
    "x-client-bundle-id": "com.deepseek.chat",
    "x-client-version": "2.5.0",
    "x-client-locale": "zh_CN",
    "x-client-timezone-offset": "28800",
}

# 捕获站点请求头时剔除的头（cookie/鉴权另有来源；编码/长度由
# Playwright 自己协商，照抄会破坏 gzip 解压）
_HEADER_DROP = {"cookie", "authorization", "host", "content-length",
                "content-type", "accept-encoding", "connection"}

# ---------------- 判定规则（纯函数区，可单测） ----------------

# AutoQuill 提示词指纹：(原文字面量, 权重)
# 权重 2 = 只此一条即可确认是 AutoQuill 的提示词；1 = 辅助证据（需与
# 其它指纹叠加才确认）。字面量均取自本仓库 prompt 源文件（跨版本收录），
# 其中「参考文章仅供」为旧版 prompt 实测原文（当前版本已改写）。
FINGERPRINTS = (
    # —— 早期提示词（V1.0.0–V2.3.0，2026-03–2026-07，取自 git 历史）——
    ("请你扮演一位顶尖的故事创作大师", 2),
    ("爆款故事架构能力", 2),
    ("去AI化写作", 2),
    ("你是一位顶尖的知乎故事创作大师", 2),
    ("## 开头铁律", 2),
    ("## 引言的输出格式（严格按示范执行）", 2),
    # —— 生成链路（story_prompt / STORY_SYSTEM_PROMPT / 纯净模式）——
    ("知乎故事区", 2),
    ("## 核心铁律", 2),
    ("输出前格式自检", 2),
    ("发布前自检", 2),
    ("输出方式（硬性）", 2),
    ("章节标题 ## **N**", 2),
    ("## 格式规范（硬性要求，输出前逐条核对）", 2),
    ("主人公命名要求", 2),
    ("行文去AI味守则", 2),
    ("量化克制守则", 2),
    ("环境与场景描写守则", 2),
    ("最高优先级：以「知乎问题」", 2),
    ("严禁搬运", 2),
    ("知乎答主", 1),
    ("人名缓出", 1),
    ("环境空镜", 1),
    ("参考文章仅供", 1),
    # —— 选题 / 评分 / 原创审核 / 配方 / 文风链路 ——
    ("创作选题顾问", 2),
    ("知乎故事分类专家", 2),
    ("故事创作技法分析师", 2),
    ("只返回严格 JSON", 2),
    ("毒舌但中肯", 2),
    ("六项之和", 2),
    ("原创审核员", 2),
    ("【抽象创作配方】", 2),
    ("写作技能签名", 2),
    ("通用写作风格签名", 2),
    ("文风分析师", 2),
    ("洗稿", 1),
    ("故事潜力", 1),
    ("去AI味", 1),
)

# 只在历史版本提示词里出现、当前工作区已改写的指纹（单测跳过错字校验）
HISTORICAL_FINGERPRINTS = {
    "参考文章仅供",
    "请你扮演一位顶尖的故事创作大师",
    "爆款故事架构能力",
    "去AI化写作",
    "你是一位顶尖的知乎故事创作大师",
    "## 开头铁律",
    "## 引言的输出格式（严格按示范执行）",
}

# 「用户自己找模型写故事」的意图短语（非 AutoQuill，单独一档供人工决定）
STORY_HINTS = (
    "写故事", "写个故事", "写一个故事", "写一篇故事", "创作故事", "故事创作",
    "写小说", "写一篇小说", "写个小说", "小说创作", "写短篇", "写长篇",
    "故事情节", "故事大纲", "故事开头", "故事结尾", "编个故事",
)

# 标题关键词：只决定「值不值得拉内容确认」（宁可多拉，不可漏拉）
TITLE_KEYWORDS = (
    "故事", "小说", "短文", "短篇", "长文", "创作", "写作", "撰写", "文案",
    "情节", "剧情", "人物", "主角", "男主", "女主", "设定", "章节", "开头",
    "结尾", "悬念", "反转", "钩子", "言情", "甜文", "虐文", "甜宠", "穿书",
    "重生", "穿越", "悬疑", "脑洞", "古言", "现言", "耽美", "校园", "仙侠",
    "玄幻", "素材", "选题", "评分", "筛选", "审核", "配方", "文风", "风格",
    "去AI味", "洗稿", "抄袭", "原创", "高赞", "参考", "改写", "润色",
    "大纲", "灵感", "桥段", "知乎", "读者",
)

VERDICT_CONFIRMED = "confirmed"      # 时间 + 内容指纹都命中 → 可删
VERDICT_STORY_OTHER = "story_other"  # 内容是写故事，但不是 AutoQuill 提示词
VERDICT_LIKELY = "likely"            # 标题命中但内容没确认（或只有弱指纹）
VERDICT_UNRELATED = "unrelated"      # 拉了内容，确认无关
VERDICT_UNCHECKED = "unchecked"      # 旧会话但标题未命中 → 没拉内容
VERDICT_ERROR = "check_failed"       # 拉内容失败

VERDICT_LABEL = {
    VERDICT_CONFIRMED: "确认 AutoQuill 写故事链路",
    VERDICT_STORY_OTHER: "写故事（非 AutoQuill 提示词）",
    VERDICT_LIKELY: "疑似（内容未确认）",
    VERDICT_UNRELATED: "无关（内容已确认）",
    VERDICT_UNCHECKED: "未查内容（标题未命中）",
    VERDICT_ERROR: "内容拉取失败",
}


def match_fingerprints(text):
    """返回命中的指纹原文字面量列表（去重、保持声明顺序）。"""
    if not text:
        return []
    hits = []
    for literal, _weight in FINGERPRINTS:
        if literal in text:
            hits.append(literal)
    return hits


def fingerprint_weight(hits):
    """指纹命中集合 → 权重合计。"""
    weight_of = {lit: w for lit, w in FINGERPRINTS}
    return sum(weight_of.get(h, 1) for h in hits)


def title_keyword_hits(title):
    """标题命中的关键词列表（用于决定是否拉内容）。"""
    t = str(title or "")
    return [kw for kw in TITLE_KEYWORDS if kw in t]


def age_days(ts, now=None):
    """时间戳（unix 秒）距今多少天；无值返回 None。"""
    if ts in (None, ""):
        return None
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    return (now - value) / 86400.0


def story_intent(text, title=""):
    """判断「用户自己在让模型写故事」的意图（非 AutoQuill 链路）。

    用于把用户手工写故事的会话单独列一档，交给人工决定是否一并删除；
    绝不进入默认删除集合。
    """
    blob = str(text or "")
    if any(hint in blob for hint in STORY_HINTS):
        return True
    t = str(title or "")
    if ("故事" in t or "小说" in t) and ("故事" in blob or "小说" in blob):
        return True
    return False


def story_kind(text, title=""):
    """story_other 细分：template=模板式提示词（工具生成），manual=用户手写请求。

    2026-04–05 那批「你是一位故事创作者。+## 格式规范…+## 创作指引」的会话
    与 output/story_*.md 里同款提示词产物同分钟出现（同一条流水线写的），
    但该模板不在本仓库 git 历史里——按「疑似工具生成」单列一档，交给人工决定。
    """
    head = str(text or "").lstrip()[:600]
    if head.startswith(("你是一位", "你是一个", "**Role**", "Role:", "请你扮演")):
        return "template"
    if "## 格式规范" in head or "## 创作指引" in head or "## 核心铁律" in head:
        return "template"
    return "manual"


def classify(title_hits, fp_weight, content_ok, story_hint=False):
    """判定：时间闸已由调用方负责，这里只看内容侧证据。"""
    if not content_ok:
        return VERDICT_LIKELY if title_hits else VERDICT_ERROR
    if fp_weight >= CONFIRM_WEIGHT:
        return VERDICT_CONFIRMED
    if fp_weight > 0:
        return VERDICT_LIKELY
    if story_hint:
        return VERDICT_STORY_OTHER
    return VERDICT_UNRELATED


def normalize_messages(messages):
    """把会话消息归一成 [{role, content}]，兼容站点两种消息格式。

    ★ 2026-09-17 实测（首次真机扫描踩到）：同一站点、不同时期的会话
    返回结构不同——

      · 新会话：message["content"] 直接是正文
      · 旧会话：没有 content 字段，正文在
        message["fragments"][{type: "REQUEST"/"RESPONSE", content: …}]

    只看 content 会把整条旧会话读成「空内容」→ 指纹 0 命中 →
    真故事会话被判成「无关」而漏删（首轮扫描 344 条全部误判）。
    """
    out = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if content in (None, ""):
            parts = []
            for frag in msg.get("fragments") or []:
                if isinstance(frag, dict):
                    parts.append(str(frag.get("content") or ""))
            content = "".join(parts)
        out.append({"role": str(msg.get("role") or "").upper(),
                    "content": str(content or "")})
    return out


def session_text(messages, limit=60000):
    """把会话消息拼成一段用于匹配指纹的文本（只用用户侧提示词）。"""
    parts = []
    total = 0
    for msg in normalize_messages(messages):
        if msg["role"] != "USER":
            continue                    # 指纹都在提示词（用户侧）里
        parts.append(msg["content"])
        total += len(msg["content"])
        if total >= limit:
            break
    return "\n".join(parts)


def extract_sessions(payload):
    """从 fetch_page 响应里取 (会话列表, 是否还有下一页)。"""
    biz = ((payload or {}).get("data") or {}).get("biz_data") or {}
    items = biz.get("chat_sessions") or []
    return items, bool(biz.get("has_more"))


def extract_messages(payload):
    """从 history_messages 响应里取消息列表。"""
    biz = ((payload or {}).get("data") or {}).get("biz_data") or {}
    return biz.get("chat_messages") or []


def api_ok(status, payload):
    """站点业务码判定：HTTP 2xx 且 code/biz_code 均为 0。"""
    if not (200 <= int(status) < 300):
        return False
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    biz = ((payload.get("data") or {}) or {}).get("biz_code")
    return code in (None, 0) and biz in (None, 0)


def fmt_ts(ts):
    if ts in (None, ""):
        return "-"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "-"


def _clip(text, width):
    """按显示宽度粗裁（中文按 2 宽度计），用于表格对齐。"""
    s = str(text or "")
    out, used = "", 0
    for ch in s:
        w = 2 if ord(ch) > 0x2E7F else 1
        if used + w > width:
            return out + "…"
        out += ch
        used += w
    return out + " " * (width - used)


# ---------------- 站点客户端（浏览器登录态 + page.request） ----------------


class DeepSeekClient:
    """复用 AutoQuill 持久化浏览器（data/browser_profile）访问站点接口。

    只读接口 + 删除接口都是站点内部 API，走 page.request 复用登录
    cookie 与 Bearer（localStorage.userToken）。
    """

    def __init__(self, headless=True, timeout_ms=30000, delay=0.25):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.delay = delay
        self._browser = None
        self._page = None
        self._token = ""
        self._client_headers = dict(DEFAULT_CLIENT_HEADERS)
        self._headers_captured = False
        self._penalty = 0.0          # 限流退避秒数（429 后翻倍）
        self.pagination_ok = True     # 分页游标是否被站点接受

    # —— 生命周期 ——

    def open(self):
        import config
        config.BROWSER_HEADLESS = bool(self.headless)
        # 注册浏览器工厂（browser_pool 不依赖 applications，工厂由应用层注册）
        import applications.zhihu_story.browser_adapter  # noqa: F401
        from web_drivers.browser_pool import get_browser
        print("[browser] 启动持久化浏览器（%s）…"
              % ("无头" if self.headless else "前台"), flush=True)
        self._browser = get_browser()
        self._page = self._browser.context.new_page()

        # 先挂监听，再进站点：把 SPA 自己发的 /api 请求头抄下来
        # （分页游标要靠 x-client-* 头才生效，见 DEFAULT_CLIENT_HEADERS）
        def _sniff(request):
            if self._headers_captured or "/api/v0/" not in request.url:
                return
            try:
                headers = dict(request.all_headers())
            except Exception:
                return
            if not headers.get("x-client-version"):
                return
            clean = {k: v for k, v in headers.items()
                     if not k.startswith(":") and k.lower() not in _HEADER_DROP}
            if clean:
                self._client_headers.update(clean)
                self._headers_captured = True

        self._page.on("request", _sniff)
        self._page.goto(SITE, wait_until="domcontentloaded", timeout=45000)
        self._page.wait_for_timeout(6000)
        print("[browser] 站点客户端头：%s"
              % ("已捕获" if self._headers_captured else "未捕获（用兜底值）"),
              flush=True)
        self._token = self._read_token()
        if not self._token:
            raise RuntimeError(
                "未在页面 localStorage 取到 userToken：登录态可能已失效。"
                "请先启动 AutoQuill 打开一次网页端完成登录，再重跑本工具"
                "（也可加 --headed 弹出浏览器手动登录）。")
        status, data = self._req("GET", "/api/v0/users/current")
        if not api_ok(status, data):
            raise RuntimeError(
                "DeepSeek 登录态校验失败（HTTP %s）：请先在 AutoQuill 网页端"
                "确认已登录，再重跑本工具。" % status)
        return data

    def close(self):
        try:
            if self._page is not None:
                self._page.close()
        except Exception:
            pass
        self._page = None
        try:
            from web_drivers.browser_pool import close_shared_browser
            close_shared_browser()
        except Exception:
            pass
        self._browser = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # —— 基础件 ——

    def _read_token(self):
        try:
            raw = self._page.evaluate(
                "() => { try { return localStorage.getItem('userToken') || ''; }"
                " catch (e) { return ''; } }") or ""
        except Exception:
            return ""
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return str(data.get("value") or "")
        except Exception:
            pass
        return str(raw)

    def account(self):
        """当前账号摘要（脱敏，只用于报告头部）。"""
        status, data = self._req("GET", "/api/v0/users/current")
        biz = ((data or {}).get("data") or {}).get("biz_data") or {}
        profile = biz.get("id_profile") or {}
        return {
            "name": profile.get("name") or "",
            "email": biz.get("email") or "",
            "mobile": biz.get("mobile_number") or "",
            "user_id": biz.get("id") or "",
        }

    def _req(self, method, path, params=None, body=None, _retry=True):
        url = SITE.rstrip("/") + path
        if params:
            url += "?" + urlencode(params)
        headers = dict(self._client_headers)
        headers["Authorization"] = "Bearer " + self._token
        try:
            if method.upper() == "GET":
                resp = self._page.request.get(
                    url, headers=headers, timeout=self.timeout_ms)
            else:
                headers["Content-Type"] = "application/json"
                resp = self._page.request.post(
                    url, data=json.dumps(body or {}, ensure_ascii=False),
                    headers=headers, timeout=self.timeout_ms)
        except Exception as exc:
            return 0, {"_error": str(exc)}
        status = resp.status
        payload = None
        try:
            payload = resp.json()
        except Exception:
            try:
                payload = {"_raw": (resp.text() or "")[:300]}
            except Exception:
                payload = {"_raw": ""}
        if status == 429:
            # 站点限流：指数退避（5s → 60s 封顶），避免越限越猛
            self._penalty = min(max(self._penalty * 2, 5.0), 60.0)
            print("[throttle] 站点限流（HTTP 429），等待 %.0fs 后继续"
                  % self._penalty, flush=True)
            time.sleep(self._penalty)
        elif 200 <= int(status) < 300:
            self._penalty = 0.0
        if status == 401 and _retry:
            # token 轮换：重新读一次页面再试一遍（登录态仍有效的情况）
            self._token = self._read_token()
            if self._token:
                return self._req(method, path, params=params, body=body,
                                 _retry=False)
        return status, payload

    # —— 站点能力 ——

    def sessions(self):
        """枚举全部会话（分页 + 置顶补齐），返回按站点顺序的列表。"""
        merged = {}
        order = []

        def merge(items):
            fresh = 0
            for item in items:
                sid = item.get("id")
                if not sid or sid in merged:
                    continue
                merged[sid] = item
                order.append(sid)
                fresh += 1
            return fresh

        status, data = self._req("GET", LIST_PATH,
                                 {"lte_cursor.pinned": "false"})
        if not api_ok(status, data):
            raise RuntimeError(
                "会话列表接口未命中（HTTP %s，%s）：站点可能改版，请重跑"
                "探测脚本 tools/archive/probes/probe_ds_session_list.py 校准。"
                % (status, str(data)[:160]))
        items, has_more = extract_sessions(data)
        merge(items)
        print("[list] 第 1 页 %d 条（累计 %d）" % (len(items), len(order)),
              flush=True)

        page_no = 1
        cursor = items[-1].get("updated_at") if items else None
        while has_more and cursor is not None and page_no < MAX_LIST_PAGES:
            page_no += 1
            status, data = self._req(
                "GET", LIST_PATH,
                {"lte_cursor.pinned": "false",
                 "lte_cursor.updated_at": "%.3f" % float(cursor)})
            if not api_ok(status, data):
                print("[list] 第 %d 页失败（HTTP %s），停止翻页"
                      % (page_no, status), flush=True)
                break
            items, has_more = extract_sessions(data)
            if not items:
                break
            fresh = merge(items)
            print("[list] 第 %d 页 %d 条（新增 %d，累计 %d）"
                  % (page_no, len(items), fresh, len(order)), flush=True)
            if fresh == 0:              # 游标没前进 → 防死循环
                self.pagination_ok = False
                print("[list] !!! 分页游标未生效（第 %d 页与上一页重复）："
                      "站点可能改版/缺客户端头，本次列表可能不完整。"
                      % page_no, flush=True)
                break
            cursor = items[-1].get("updated_at")

        # 置顶会话单独一页（首屏之外可能还有）
        status, data = self._req("GET", LIST_PATH,
                                 {"lte_cursor.pinned": "true"})
        if api_ok(status, data):
            items, _ = extract_sessions(data)
            fresh = merge(items)
            if fresh:
                print("[list] 置顶补齐 %d 条" % fresh, flush=True)

        return [merged[sid] for sid in order]

    def history(self, session_id):
        """拉取单个会话的全部消息（原始响应 biz_data）。"""
        status, data = self._req(
            "GET", HISTORY_PATH, {"chat_session_id": session_id})
        if not api_ok(status, data):
            return None, "HTTP %s %s" % (status, str(data)[:120])
        return extract_messages(data), None

    def delete_sessions(self, session_ids):
        """批量删除会话，返回 (是否成功, 说明)。"""
        if not session_ids:
            return True, "空批次"
        status, data = self._req("POST", DELETE_PATH,
                                 body={"chat_session_ids": list(session_ids)})
        if api_ok(status, data):
            return True, "ok"
        return False, "HTTP %s %s" % (status, str(data)[:160])

    def create_empty_session(self):
        """新建一条空会话（仅 smoke 自检用，随即删除）。"""
        status, data = self._req("POST", CREATE_PATH, body={})
        if not api_ok(status, data):
            return None, "HTTP %s %s" % (status, str(data)[:160])
        biz = ((data.get("data") or {}).get("biz_data") or {})
        # 实测两种返回形态：biz_data.id 或 biz_data.chat_session.id
        sid = biz.get("id") or (biz.get("chat_session") or {}).get("id")
        if not sid:
            return None, "响应里没有会话 id：%s" % str(data)[:200]
        return sid, None


# ---------------- 子命令：scan ----------------

DEFAULT_REPORT_DIR = os.path.join("data", "cleanup")


def _days_text(days):
    """天数显示（30 而不是 30.0）。"""
    f = float(days)
    return "%d 天" % f if f == int(f) else "%.1f 天" % f


def _report_path(explicit=""):
    if explicit:
        return explicit
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(DEFAULT_REPORT_DIR, "scan_%s.json" % stamp)


def cmd_scan(args):
    client = DeepSeekClient(headless=not args.headed,
                            timeout_ms=int(args.timeout * 1000),
                            delay=args.delay)
    client.open()
    try:
        account = client.account()
        print("[account] %s / %s" % (account.get("name") or "?",
                                     account.get("email") or account.get("mobile") or "?"),
              flush=True)
        sessions = client.sessions()
        now = time.time()
        cutoff = now - args.days * 86400.0
        print("[scan] 会话共 %d 条；年龄阈值 %d 天（cutoff=%s）"
              % (len(sessions), args.days, fmt_ts(cutoff)), flush=True)

        old, fresh = [], []
        for item in sessions:
            ts = item.get("updated_at")
            days = age_days(ts, now)
            if days is None:
                continue
            (old if float(ts) < cutoff else fresh).append(item)

        print("[scan] %s前的旧会话 %d 条，新会话 %d 条（新会话不动）"
              % (_days_text(args.days), len(old), len(fresh)), flush=True)

        entries = []
        fetched = 0
        for idx, item in enumerate(old, 1):
            sid = item.get("id")
            title = item.get("title") or ""
            hits = title_keyword_hits(title)
            need_content = bool(args.all_content or hits)
            if (need_content and args.max_content
                    and fetched >= args.max_content):
                need_content = False          # 调试用量上限，本次不查
            fp_hits, content_ok, msg_count, err = [], False, 0, None
            story_hint = False
            kind = ""
            if need_content:
                messages, err = client.history(sid)
                if messages is None:            # 瞬时失败重试一次
                    if args.delay:
                        time.sleep(max(args.delay, 1.0))
                    messages, err = client.history(sid)
                fetched += 1
                if messages is None:
                    content_ok = False
                else:
                    content_ok = True
                    msg_count = len(normalize_messages(messages))
                    prompt_text = session_text(messages)
                    fp_hits = match_fingerprints(prompt_text)
                    story_hint = story_intent(prompt_text, title)
                    kind = story_kind(prompt_text, title)
                if args.delay:
                    time.sleep(args.delay)
                verdict = classify(hits, fingerprint_weight(fp_hits),
                                   content_ok, story_hint=story_hint)
            else:
                verdict = VERDICT_UNCHECKED
            entry = {
                "id": sid,
                "title": title,
                "title_type": item.get("title_type"),
                "model_type": item.get("model_type"),
                "pinned": bool(item.get("pinned")),
                "inserted_at": item.get("inserted_at"),
                "updated_at": item.get("updated_at"),
                "updated_local": fmt_ts(item.get("updated_at")),
                "age_days": round(age_days(item.get("updated_at"), now) or 0, 1),
                "title_hits": hits,
                "fp_hits": fp_hits,
                "fp_weight": fingerprint_weight(fp_hits),
                "msg_count": msg_count,
                "content_checked": bool(need_content),
                "story_hint": bool(story_hint),
                "story_kind": kind if story_hint else "",
                "verdict": verdict,
                "error": err,
            }
            entries.append(entry)
            if idx % 25 == 0 or idx == len(old):
                print("  …进度 %d/%d（已拉内容 %d 条）"
                      % (idx, len(old), fetched), flush=True)
    finally:
        client.close()

    # —— 统计 ——
    counts = {v: 0 for v in VERDICT_LABEL}
    for e in entries:
        counts[e["verdict"]] = counts.get(e["verdict"], 0) + 1
    deletable = [e for e in entries
                 if e["verdict"] == VERDICT_CONFIRMED and not e["pinned"]]
    pinned_hit = [e for e in entries
                  if e["verdict"] == VERDICT_CONFIRMED and e["pinned"]]

    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cutoff_ts": cutoff,
        "cutoff_local": fmt_ts(cutoff),
        "days": args.days,
        "site": SITE,
        "account": account,
        "total_sessions": len(sessions),
        "old_sessions": len(old),
        "new_sessions": len(fresh),
        "content_checked": fetched,
        "counts": counts,
        "deletable_count": len(deletable),
        "pinned_confirmed_count": len(pinned_hit),
        "candidates": entries,
    }
    out_path = _report_path(args.out)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)

    # —— 报告打印 ——
    print()
    print("=" * 74)
    print("DeepSeek 历史会话扫描报告")
    print("=" * 74)
    print("站点            %s" % SITE)
    print("账号            %s / %s" % (account.get("name") or "?",
                                      account.get("email") or account.get("mobile") or "?"))
    print("扫描时间        %s" % report["generated_at"])
    print("会话总数        %d" % len(sessions))
    print("旧会话(≥%s)   %d   ← 本工具的处理范围"
          % (_days_text(args.days), len(old)))
    print("新会话          %d   ← 一律不动" % len(fresh))
    print("-" * 74)
    for verdict, label in VERDICT_LABEL.items():
        print("  %-22s %5d" % (label, counts.get(verdict, 0)))
    print("-" * 74)
    print("可删除（确认命中且未置顶）  %d" % len(deletable))
    if pinned_hit:
        print("置顶命中（默认跳过）        %d"
              "（如需一并删除：delete --include-pinned）" % len(pinned_hit))
    if counts.get(VERDICT_STORY_OTHER):
        kinds = {}
        for e in entries:
            if e["verdict"] == VERDICT_STORY_OTHER:
                kinds[e.get("story_kind") or "manual"] = \
                    kinds.get(e.get("story_kind") or "manual", 0) + 1
        print("写故事但非 AutoQuill 的会话  %d"
              "（默认不删；确认要一起清就加 --include-other）"
              % counts[VERDICT_STORY_OTHER])
        print("    · 模板式长提示词（疑似工具生成） %d"
              % kinds.get("template", 0))
        print("    · 用户手写写故事请求             %d"
              % kinds.get("manual", 0))
    if counts.get(VERDICT_UNCHECKED):
        print("未查内容的旧会话            %d"
              "（标题未命中关键词；要全量确认请加 --all-content）"
              % counts[VERDICT_UNCHECKED])
    print()
    print("前 25 条可删除会话：")
    print("  %-16s %6s  %-28s %s" % ("最后活动", "天数", "标题", "命中指纹"))
    for e in deletable[:25]:
        print("  %-16s %6.1f  %-28s %s"
              % (e["updated_local"], e["age_days"],
                 _clip(e["title"], 30),
                 ",".join(e["fp_hits"][:3])))
    if len(deletable) > 25:
        print("  … 其余 %d 条见报告" % (len(deletable) - 25))
    print()
    print("报告已写入：%s" % out_path)
    print("下一步（确认无误后才会真删）：")
    print("  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe "
          "tools/ds_history_cleanup.py delete --report %s --yes" % out_path)
    return 0


# ---------------- 子命令：delete ----------------


def cmd_delete(args):
    if not os.path.exists(args.report):
        print("报告不存在：%s" % args.report)
        return 2
    with open(args.report, encoding="utf-8") as fh:
        report = json.load(fh)
    entries = report.get("candidates") or []

    def selected(entry):
        """三档范围：confirmed（默认）/ +模板式 story_other / +全部 story_other。"""
        verdict = entry.get("verdict")
        if verdict == VERDICT_CONFIRMED:
            return True
        if verdict == VERDICT_LIKELY:
            return bool(args.include_likely)
        if verdict == VERDICT_STORY_OTHER:
            if args.include_other:
                return True
            if args.include_template:
                return entry.get("story_kind") == "template"
            return False
        return False

    targets = [e for e in entries
               if selected(e)
               and (args.include_pinned or not e.get("pinned"))]
    if args.ids:
        only = {s.strip() for s in args.ids.split(",") if s.strip()}
        targets = [e for e in targets if e["id"] in only]
    if args.limit:
        targets = targets[:args.limit]

    print("=" * 74)
    print("DeepSeek 历史会话删除计划")
    print("=" * 74)
    print("依据报告        %s" % args.report)
    scope = "confirmed"
    if args.include_template:
        scope += "+模板式写故事"
    if args.include_other:
        scope += "+全部写故事会话"
    if args.include_likely:
        scope += "+likely"
    print("判定范围        %s%s" % (scope,
                                   "" if args.include_pinned
                                   else "（跳过置顶）"))
    print("命中会话        %d 条" % len(targets))
    if not targets:
        print("没有需要删除的会话，结束。")
        return 0
    for e in targets[:15]:
        print("  %-16s %-28s %s"
              % (e["updated_local"], _clip(e["title"], 30),
                 ",".join(e["fp_hits"][:2])))
    if len(targets) > 15:
        print("  … 其余 %d 条" % (len(targets) - 15))

    backup_dir = args.backup_dir
    if not backup_dir:
        backup_dir = os.path.join(
            DEFAULT_REPORT_DIR,
            "backup_%s" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    print("正文备份目录    %s%s" % (backup_dir,
                                   "（--no-backup，跳过备份）"
                                   if args.no_backup else ""))
    if not args.yes:
        print()
        print("当前是 dry-run（未加 --yes）：一条都没删。")
        print("确认无误后执行：  ... delete --report %s --yes" % args.report)
        return 0

    client = DeepSeekClient(headless=not args.headed,
                            timeout_ms=int(args.timeout * 1000),
                            delay=args.delay)
    client.open()
    deleted, failed, skipped = [], [], []
    try:
        # —— 1. 备份（备份失败的条目直接不删，保证可回溯）——
        if not args.no_backup:
            os.makedirs(backup_dir, exist_ok=True)
            index = []
            for i, e in enumerate(targets, 1):
                messages, err = client.history(e["id"])
                if messages is None:
                    skipped.append({"id": e["id"], "title": e.get("title"),
                                    "reason": "备份失败：%s" % err})
                    print("[backup] %d/%d 跳过 %s（备份失败 %s）"
                          % (i, len(targets), e["id"], err), flush=True)
                    continue
                path = os.path.join(backup_dir, "%s.json" % e["id"])
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"session": e, "messages": messages}, fh,
                              ensure_ascii=False, indent=1)
                index.append({"id": e["id"], "title": e.get("title"),
                              "updated_at": e.get("updated_at"),
                              "file": os.path.basename(path),
                              "msg_count": len(messages)})
                e["_backed_up"] = True
                if args.delay:
                    time.sleep(args.delay)
                if i % 20 == 0 or i == len(targets):
                    print("[backup] %d/%d 已备份" % (i, len(targets)),
                          flush=True)
            with open(os.path.join(backup_dir, "index.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(index, fh, ensure_ascii=False, indent=1)
            todo = [e for e in targets if e.get("_backed_up")]
        else:
            todo = list(targets)

        # —— 2. 删除（分批 + 连续失败熔断）——
        chunk = max(1, int(args.chunk))
        consecutive_fail = 0
        for start in range(0, len(todo), chunk):
            batch = todo[start:start + chunk]
            ids = [e["id"] for e in batch]
            ok, note = client.delete_sessions(ids)
            if ok:
                deleted.extend(ids)
                consecutive_fail = 0
                print("[delete] %d/%d 已删除（本批 %d 条）"
                      % (len(deleted), len(todo), len(ids)), flush=True)
            else:
                consecutive_fail += 1
                failed.extend({"id": i, "reason": note} for i in ids)
                print("[delete] 本批失败：%s" % note, flush=True)
                if consecutive_fail >= args.max_consecutive_fail:
                    print("[delete] 连续 %d 批失败，中止（防风控）；"
                          "已完成 %d 条，可稍后重跑同一命令续删。"
                          % (consecutive_fail, len(deleted)))
                    break
            if args.delay:
                time.sleep(max(args.delay, 0.2))

        # —— 3. 复核：重新拉一遍列表，看还有哪些没删掉 ——
        print("[verify] 重新枚举会话校验…", flush=True)
        try:
            alive = {s.get("id") for s in client.sessions()}
        except Exception as exc:
            alive = set()
            print("[verify] 复核失败（不影响已删结果）：%s" % exc)
        still_there = [i for i in deleted if i in alive]
    finally:
        client.close()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(DEFAULT_REPORT_DIR, "delete_%s.json" % stamp)
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as fh:
        json.dump({
            "report": args.report,
            "deleted": deleted,
            "failed": failed,
            "skipped": skipped,
            "still_present_after_verify": still_there,
            "backup_dir": None if args.no_backup else backup_dir,
        }, fh, ensure_ascii=False, indent=1)

    print()
    print("=" * 74)
    print("删除完成：成功 %d 条，失败 %d 条，跳过（备份失败）%d 条"
          % (len(deleted), len(failed), len(skipped)))
    if still_there:
        print("复核发现仍有 %d 条在列表里（可重跑同一命令续删）" % len(still_there))
    if not args.no_backup:
        print("正文备份：%s" % backup_dir)
    print("删除日志：%s" % log_path)
    return 0


# ---------------- 子命令：smoke（删除链路自检） ----------------


def cmd_smoke(args):
    """自建一条空会话，再用生产删除链路删掉（不碰任何历史会话）。"""
    client = DeepSeekClient(headless=not args.headed,
                            timeout_ms=int(args.timeout * 1000),
                            delay=args.delay)
    client.open()
    try:
        sid, err = client.create_empty_session()
        if not sid:
            print("[smoke] 新建空会话失败：%s" % err)
            return 1
        print("[smoke] 已新建空会话 %s（无消息，随即删除）" % sid)
        ok, note = client.delete_sessions([sid])
        print("[smoke] 删除接口返回：%s（%s）" % ("成功" if ok else "失败", note))
        alive = {s.get("id") for s in client.sessions()}
        gone = sid not in alive
        print("[smoke] 列表复核：%s" % ("已消失 ✓" if gone else "仍在列表里 ✗"))
        return 0 if (ok and gone) else 1
    finally:
        client.close()


# ---------------- CLI ----------------


def build_parser():
    parser = argparse.ArgumentParser(
        description="DeepSeek 历史会话审计与清理（AutoQuill 写故事链路残留）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--headed", action="store_true",
                        help="弹出浏览器（排查登录态用，默认无头）")
    common.add_argument("--timeout", type=float, default=30.0,
                        help="单个请求超时秒数（默认 30）")
    common.add_argument("--delay", type=float, default=0.25,
                        help="接口之间停顿秒数（默认 0.25，防风控）")

    p_scan = sub.add_parser("scan", parents=[common],
                            help="只读扫描 + 统计报告（不删任何东西）")
    p_scan.add_argument("--days", type=float, default=30,
                        help="只看最后活动早于 N 天的会话（默认 30）")
    p_scan.add_argument("--all-content", action="store_true",
                        help="所有旧会话都拉内容确认（最保守，请求最多）")
    p_scan.add_argument("--max-content", type=int, default=0,
                        help="本次最多拉多少条的会话内容（0=不限，调试用）")
    p_scan.add_argument("--out", default="", help="报告 JSON 输出路径")
    p_scan.set_defaults(func=cmd_scan)

    p_del = sub.add_parser("delete", parents=[common],
                           help="按报告删除（不带 --yes 只打印计划）")
    p_del.add_argument("--report", required=True, help="scan 产出的报告 JSON")
    p_del.add_argument("--yes", action="store_true", help="真的执行删除")
    p_del.add_argument("--include-likely", action="store_true",
                       help="连「疑似」一起删（默认只删 confirmed）")
    p_del.add_argument("--include-template", action="store_true",
                       help="连「模板式长提示词（疑似工具生成）」那档一起删")
    p_del.add_argument("--include-other", action="store_true",
                       help="连全部「写故事但非 AutoQuill」会话一起删（含手写请求）")
    p_del.add_argument("--include-pinned", action="store_true",
                       help="连置顶会话一起删（默认跳过置顶）")
    p_del.add_argument("--ids", default="", help="只删这些 id（逗号分隔）")
    p_del.add_argument("--limit", type=int, default=0, help="最多删 N 条")
    p_del.add_argument("--chunk", type=int, default=10,
                       help="每批提交多少条给删除接口（默认 10）")
    p_del.add_argument("--max-consecutive-fail", type=int, default=3,
                       help="连续失败多少批后中止（默认 3）")
    p_del.add_argument("--backup-dir", default="", help="正文备份目录")
    p_del.add_argument("--no-backup", action="store_true",
                       help="跳过正文备份（不推荐：删掉不可恢复）")
    p_del.set_defaults(func=cmd_delete)

    p_smoke = sub.add_parser("smoke", parents=[common],
                             help="删除链路自检（建空会话再删，不碰历史）")
    p_smoke.set_defaults(func=cmd_smoke)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
