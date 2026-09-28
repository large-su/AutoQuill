# ============================================================
# applications/zhihu_story/browser_write.py
# 写操作通道：草稿API读写/写回答按钮/编辑器富文本粘贴与发布确认
# P0 拆分自 browser_adapter.ZhihuBrowser；方法体逐字搬运未改动，
# 行为由 test_browser_adapter 的源码锚点断言守护。
# ============================================================

import json
import logging
import re
import os
import time

log = logging.getLogger(__name__)

from core.paths import data as _data_path

from .browser_utils import (
    _NAV_TIMEOUT,
    build_draft_marker,
    clean_story_markdown,
    page_needs_login,
    story_markdown_to_html,
)


def _publish_verdict(*, clicked_ok, click_reason, url="", draft_pending=None,
                     receipt_ok=None, receipt_detail="", errors=()):
    """把「发布到底成没成」的判定收成一个**纯函数**（可单测，不碰页面）。

    ★ 2026-09-28 线上事故（界面报失败、账号里却真有新回答）就是判定散落在
    各处、且「点击失败」被当成了「发布失败」造成的。现在所有事实都摆在这里，
    结论只有三种，且**成功优先于失败**：

      ok=True  → 发布确实出去了（哪怕点击那一步报错）
      ok=False + certain=True  → 确实没发出去（可以安全重试/补发）
      ok=False + certain=False → 没确认到，但**也没证据说它没发出去**（不许重发）

    事实来源与可信度（高→低）：
      1. URL 已是 /answer/<aid>：铁证，成功；
      2. 服务端草稿已不在草稿箱（draft_pending is False）：铁证，成功；
      3. 发布接口回执 code=0：服务端已受理，成功；
      4. 回执 code!=0：服务端明确拒绝，失败且确定；
      5. 点击失败 + 草稿仍在：失败但**不确定**（可能请求在路上）；
      6. 什么都没有：不确定。
    """
    if re.search(r"/answer/(\d+)", url or ""):
        return {"ok": True, "certain": True, "detail": "页面已跳到回答页"}
    if draft_pending is False:
        return {"ok": True, "certain": True,
                "detail": "草稿已不在草稿箱（核对后确认：本次发布其实已成功）"}
    if receipt_ok is True:
        return {"ok": True, "certain": True,
                "detail": "发布请求已被服务端受理（code=0，草稿尚未刷新）"}
    if receipt_ok is False:
        why = receipt_detail or "服务端拒绝了发布"
        return {"ok": False, "certain": True, "detail": "服务端拒绝发布：%s" % why}
    if clicked_ok:
        # 点击成功但迟迟没有结果：不确定（可能在审核/接口滞后）
        return {"ok": False, "certain": False,
                "detail": "已点发布，但没确认到结果"}
    # 点击失败：只有「草稿确实还在」才能说「确定没发出去」
    if draft_pending is True:
        return {"ok": False, "certain": True,
                "detail": "点击「发布回答」失败：%s（草稿仍在草稿箱）"
                          % (click_reason or "未知原因")}
    return {"ok": False, "certain": False,
            "detail": "点击「发布回答」没能确认：%s" % (click_reason or "未知原因")}


class WriteActionsMixin:

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

    def _arm_publish_watch(self):
        """挂一个只认「发布接口回执」的监听器。

        ★ 为什么必须听回执：/api/v4/content/publish 用 **HTTP 200 + body.code**
        表达业务失败（例如 code=403「当前问题不支持开启送礼物」），只看状态码
        会把「被服务端拒绝」当成「已提交」而干等超时（2026-09-28 真机）。
        """
        events = []
        state = {"_events": events}

        def _on_resp(resp):
            try:
                if "/api/v4/content/publish" not in (resp.url or ""):
                    return
                ev = {"status": resp.status, "body": ""}
                try:
                    ev["body"] = (resp.text() or "")[:400]
                except Exception:               # noqa: BLE001 响应体读不到不算致命
                    pass
                events.append(ev)
            except Exception:                   # noqa: BLE001 监听器绝不能抛
                pass

        try:
            self.page.on("response", _on_resp)
            state["page"] = self.page
            state["_on_resp"] = _on_resp
        except Exception as exc:                # noqa: BLE001
            log.debug("browser_adapter: 发布回执监听挂载失败：%s", exc)
        return state

    def _disarm_publish_watch(self, watch):
        """摘掉回执监听（不摘会在长驻进程里越积越多）。"""
        try:
            if watch and watch.get("page") is not None:
                watch["page"].remove_listener("response", watch.get("_on_resp"))
        except Exception:                       # noqa: BLE001
            pass

    def _publish_receipt(self, watch):
        """解析**本次点击之后**捕获到的发布回执：{"ok": bool|None, "detail": str}。

        ok=True/False = 拿到了明确结果；ok=None = 还没收到回执。
        只看 `since` 之后的回执：关掉送礼物重试时，若还把上一次的 403 当结果，
        就会把已经成功的第二次发布判成失败（2026-09-28 测试抓到）。
        """
        events = list((watch or {}).get("_events") or [])
        since = int((watch or {}).get("since") or 0)
        events = events[since:]
        if not events:
            return {"ok": None, "detail": ""}
        ev = events[-1]
        body = ev.get("body") or ""
        msg = ""
        try:
            data = json.loads(body) if body else {}
            code = data.get("code")
            msg = (data.get("toast_message") or data.get("message") or "").strip()
            if code in (0, None) and not msg:
                return {"ok": True, "detail": ""}
            if code not in (0, None):
                return {"ok": False, "detail": msg or ("服务端返回 code=%s" % code)}
        except Exception:                       # noqa: BLE001 非 JSON：按状态码判
            pass
        if ev.get("status") and int(ev["status"]) >= 400:
            return {"ok": False,
                    "detail": msg or ("服务端 HTTP %s" % ev.get("status"))}
        return {"ok": True, "detail": msg}

    def _publish_already_done(self, trust_draft=True):
        """发布是不是其实已经完成了。

        ★ 2026-09-28 线上事故（界面报失败、账号里却真有新回答）：
          真实鼠标点击发起了发布，但按钮在点中之后立刻**重新渲染/变灰**，
          Playwright 的 actionability 检查于是超时抛错，代码把它当成「没点中」
          报了 no-button——而服务端那边已经发布成功了。
          所以**任何「点失败」的结论都必须先过这一关**。

        trust_draft：是否把「服务端草稿已清空」当作发布完成的证据。
          · 点过发布之后 → True：草稿清空就是发出去了的铁证（历史成功路径都靠它）；
          · 还没点过之前 → False：草稿接口首帧未加载/抖动也会读回空，
            那时把它当成功会**把没发的当成发了**——这个方向更危险。
        返回 (bool, url, detail)。
        """
        try:
            url = self.page.url or ""
        except Exception:                       # noqa: BLE001
            url = ""
        if re.search(r"/answer/(\d+)", url):
            return True, url, "页面已跳到回答页"
        if trust_draft:
            try:
                if not self.get_draft_content():
                    return True, url, "服务端草稿已清空（已发布）"
            except Exception as exc:            # noqa: BLE001
                log.debug("browser_adapter: 发布后状态确认失败：%s", exc)
        return False, url, ""

    def _recheck_after_click_gap(self, watch, gap=16):
        """等一小会儿再看：点击引发的发布是不是已经在路上/已完成。

        用途：真实点击「报错」时先别急着判失败——请求可能已经发出去了。
        命中以下任一即算成功：页面跳到 /answer/、服务端草稿已清空、
        发布接口回了 code=0。
        """
        deadline = time.time() + max(3, int(gap))
        while time.time() < deadline:
            done, url, why = self._publish_already_done()
            if done:
                return True, url, why
            rc = self._publish_receipt(watch)
            if rc.get("ok") is True:
                return True, url, "服务端已受理发布（code=0）"
            if rc.get("ok") is False:
                return False, url, rc.get("detail") or "服务端拒绝了发布"
            time.sleep(2)
        done, url, why = self._publish_already_done()
        return done, url, why

    def _reset_publish_watch(self, watch):
        """把回执窗口推到「现在」：之后只看新回执（重试点击前调一次）。"""
        try:
            if watch is not None:
                watch["since"] = len(watch.get("_events") or [])
        except Exception:                       # noqa: BLE001
            pass

    def _click_publish_native(self, timeout=20):
        """点「发布回答」——**真实鼠标点击**（不是 JS 的 hit.click()）。

        2026-09-28 真机对照：JS click 触发的那次发布请求根本没发出去（页面无任何
        网络请求、按钮只是变灰），换成 Playwright 真实点击后
        POST /api/v4/content/publish 立刻发出。发布是不可逆动作，宁可多花一次
        真实点击，也不要「点了但什么都没发生」。

        两个真机坑（都已踩过）：
          · 按钮**晚于编辑器**渲染 → 有界重试着找；
          · 按钮就绪前是 disabled，点它 Playwright 会一直等到超时 →
            先等按钮可用再点。
        """
        deadline = time.time() + max(3, int(timeout))
        last = "no-button"
        while time.time() < deadline:
            try:
                loc = self.page.get_by_role("button", name=re.compile("发布回答"))
                if loc.count():
                    if not loc.first.is_enabled():
                        # 按钮还没就绪（draft 仍在加载/上一次提交还在飞）。点它会
                        # 一直等到超时——旧代码就是这样把「已发出的发布」误报成
                        # 「no-button」的（2026-09-28 线上事故）。
                        last = "按钮暂不可用（disabled），等待就绪"
                        time.sleep(1.5)
                        continue
                    loc.first.click(timeout=6000)
                    return {"ok": True, "how": "native"}
            except Exception as exc:            # noqa: BLE001
                # ★ 点击抛错**不等于没点中**：按钮可能已经收到点击并重新渲染
                #   （Playwright 的 actionability 检查因此超时）。这里绝不立刻
                #   返回失败——交给上层 _recheck_after_click_gap 用「页面是否已
                #   跳到回答页 / 草稿是否已清空 / 回执是否 code=0」来定性。
                return {"ok": False, "how": "native-uncertain",
                        "uncertain": True, "reason": str(exc)[:120]}
            hit = self._safe_evaluate(self._PUBLISH_BTN_JS, True)
            if hit:
                if isinstance(hit, dict) and hit.get("disabled"):
                    last = "按钮暂不可用（disabled）"
                else:
                    return {"ok": True, "how": "js"}
            time.sleep(1.5)
        return {"ok": False, "how": "", "reason": last}

    def _draft_still_pending(self, qid):
        """草稿箱里还有这篇草稿吗（True=还没发出去 / False=已经不在了）。

        ★ 这是「这次到底发出去没有」的**权威判据**：草稿箱只列未发布的内容，
        一篇草稿从草稿箱消失只有两种可能——已发布，或被人工删除。
        返回 None = 读不到（网络/登录异常），调用方不得据此下结论。
        """
        cards = self.list_draft_cards()
        if page_needs_login(self.page):
            return None
        qids = {str(c.get("qid") or "") for c in (cards or [])}
        return str(qid) in qids

    def _reconcile_failure(self, target):
        """失败前的最后一道一致性检查：这篇草稿是不是其实已经发出去了。

        ★ 2026-09-28 线上事故（用户看到「界面报失败、账号里却真有新回答」）：
          发布请求成功、但代码因为拿不到确认信号而报失败，界面上就出现
          「失败 + 账号里有新回答」的自相矛盾。宁可多花一次只读的草稿箱检查，
          也不要给用户一个和事实相反的结论。
        返回 (published: bool, detail: str)。
        """
        qid = str((target or {}).get("qid") or "")
        if not qid:
            return False, ""
        try:
            pending = self._draft_still_pending(qid)
        except Exception as exc:                # noqa: BLE001
            log.debug("browser_adapter: 失败核对（读草稿箱）异常：%s", exc)
            return False, ""
        if pending is False:
            return True, "草稿已不在草稿箱（核对后确认：本次发布其实已成功）"
        return False, ""

    def _turn_gift_off(self):
        """发布设置里把「送礼物」切成「关闭送礼物」（问题不支持送礼物时的必需动作）。

        PublishPanel-RewardSetting-1 = 关闭送礼物（0 = 开启送礼物）。
        返回 True 表示已切到「关闭」。
        """
        try:
            r = self._safe_evaluate(self._GIFT_OFF_JS) or {}
            if r.get("ok"):
                log.info("browser_adapter: 已关闭「送礼物」（%s）",
                         "本来就关着" if r.get("already") else "本次切换")
                return True
            log.info("browser_adapter: 未找到「关闭送礼物」选项：%s",
                     r.get("reason"))
        except Exception as exc:                # noqa: BLE001
            log.warning("browser_adapter: 关闭送礼物失败：%s", exc)
        return False

    def _gift_settings_reachable(self):
        """「发布设置」面板此刻可见吗（可见才点得到里面的单选框）。"""
        try:
            return bool(self.page.query_selector(
                '#PublishPanel-RewardSetting-1, label[for="PublishPanel-RewardSetting-1"]'))
        except Exception:                       # noqa: BLE001
            return False

    def _open_publish_settings(self):
        """点开「发布设置」面板（草稿态默认收起；送礼物选项在里面）。"""
        try:
            loc = self.page.get_by_text("发布设置", exact=True)
            if loc.count():
                loc.first.click(timeout=5000)
                time.sleep(1.2)
                return True
            hit = self._safe_evaluate("""() => {
              const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
              const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
                  .join('').replace(/\\s+/g, '').trim();
              const el = Array.from(document.querySelectorAll('button,div,span,a'))
                  .find(e => e.offsetParent !== null
                             && clean(e.innerText) === '发布设置');
              if (!el) return false;
              el.click();
              return true;
            }""")
            time.sleep(1.2)
            return bool(hit)
        except Exception as exc:                # noqa: BLE001
            log.debug("browser_adapter: 打开发布设置失败：%s", exc)
            return False

    def _dump_page_state(self, tag):
        """失败诊断：把当前页面状态写进日志（URL/标题/按钮/正文开头）+ 截图。

        发布偶发「找不到写回答按钮」/「点了发布没反应」——原因可能是 SPA
        漂移、会话弹窗、风控空壳页或「回答已存在」。没有现场信息只能盲猜，
        dump 让下一次失败可诊断（返回截图路径，供通知里直接给用户看）。"""
        shot = ""
        try:
            outdir = _data_path("data", "cleanup")
            os.makedirs(outdir, exist_ok=True)
            shot = os.path.join(
                outdir, "page_%s_%s.png"
                % (tag, time.strftime("%Y%m%d_%H%M%S")))
            self.page.screenshot(path=shot, full_page=False)
        except Exception as exc:            # noqa: BLE001 截图失败不该影响主流程
            log.debug("browser_adapter: 现场截图失败[%s]: %s", tag, exc)
            shot = ""
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
            state = state if isinstance(state, dict) else {}
            log.warning("browser_adapter: 页面状态[%s] url=%s title=%s",
                        tag, state.get("url"), state.get("title"))
            log.warning("browser_adapter: 页面状态[%s] buttons=%s",
                        tag, state.get("buttons"))
            log.warning("browser_adapter: 页面状态[%s] body=%s",
                        tag, state.get("bodyHead"))
        except Exception as e:
            log.warning("browser_adapter: 页面状态 dump 失败[%s]: %s", tag, e)

    # ---------------- 发布草稿（自动化 M2；2026-09-19 真机探针确认的 DOM） ----------------
    # 真实链路：草稿箱列表（DOM 顺序 = 「编辑于」倒序，**最旧的在最后一张**）
    #   → 打开该草稿的编辑页（/question/<qid>#write，编辑器内已带草稿正文）
    #   → 点编辑器主按钮「发布回答」（Button--primary Button--blue）
    #   → 若弹出发布设置/确认弹窗，点其中的「发布/确认发布/确定」
    #   → 校验：URL 变成 /question/<qid>/answer/<aid>，或服务端草稿 API 已无内容
    # 探针结论（tools/archive/probes/probe_draft_publish.py）：列表卡片 12 张、
    # 编辑页 has_editor=True、按钮文案「发布回答」、旁边有「发布设置」。
    _DRAFT_URL = "https://www.zhihu.com/creator/manage/creation/draft?type=answer"

    _DRAFT_CARDS_JS = r"""() => Array.from(
        document.querySelectorAll('.CreationManage-CreationCard')).map((c, i) => {
      const a = c.querySelector('a[href*="/question/"][href*="#write"]');
      const t = c.querySelector('.CreationCardTitle-wrapper');
      const m = a ? a.href.match(/question\/(\d+)/) : null;
      return {index: i, qid: m ? m[1] : '', href: a ? a.href : '',
              title: t ? (t.innerText || '').trim() : ''};
    })"""

    # click=False 用于演练：只确认按钮在，不点（发布不可逆，演练绝不点）
    # 匹配放宽（2026-09-28）：按钮文案带零宽字符/换行/内部 span 时，
    # 「innerText === '发布回答'」会失配 → 演练误报「找不到发布按钮」。
    # 现在剥掉全部空白与零宽字符后比较，并返回命中的 class 供日志核对。
    _PUBLISH_BTN_JS = """(click) => {
      const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
      const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
          .join('').replace(/\\s+/g, '').trim();
      const hit = Array.from(document.querySelectorAll('button'))
          .find(b => clean(b.innerText) === '发布回答' && b.offsetParent !== null);
      if (!hit) return null;
      if (click) hit.click();
      return {text: clean(hit.innerText).slice(0, 12),
              cls: String(hit.className || '').slice(0, 70),
              disabled: !!hit.disabled};
    }"""

    _PUBLISH_CONFIRM_JS = r"""() => {
      const clean = s => (s || '').replace(/[\u200b-\u200d\ufeff]/g, '')
          .replace(/\s+/g, '').trim();
      const modals = Array.from(document.querySelectorAll(
          '[class*=Modal],[role=dialog]')).filter(m => m.offsetParent !== null);
      for (const m of modals) {
        const hit = Array.from(m.querySelectorAll('button')).find(b =>
            /^(确认发布|确定发布|发布|确认|确定)$/.test(clean(b.innerText))
            && b.offsetParent !== null);
        if (hit) { hit.click(); return clean(hit.innerText); }
      }
      return '';
    }"""

    _PUBLISH_ERROR_JS = r"""() => {
      // 页面上的报错/风控文案（发布被服务端拒绝时唯一可读的证据）
      const clean = s => (s || '').replace(/[\u200b-\u200d\ufeff]/g, '')
          .replace(/\s+/g, ' ').trim();
      const vis = e => e.offsetParent !== null;
      const nodes = Array.from(document.querySelectorAll('div,span,p,li'));
      const hits = [];
      for (const e of nodes) {
        if (!vis(e)) continue;
        const t = clean(e.innerText);
        if (!t || t.length > 120) continue;
        if (/发布失败|发布出错|操作失败|操作频繁|系统繁忙|请稍后再试|内容违规|涉嫌违规|无法发布|不能发布|已被|重复发布|审核/.test(t)) {
          hits.push(t);
          if (hits.length >= 3) break;
        }
      }
      return hits;
    }"""

    _DRAFT_STATE_JS = r"""() => {
      // 编辑页底部的草稿状态条：「N 小时前 · 草稿」「草稿已保存」「已发布」…
      const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
      const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
          .join('').replace(/\s+/g, ' ').trim();
      const vis = e => e.offsetParent !== null;
      const bar = Array.from(document.querySelectorAll('div,span'))
          .filter(e => vis(e) && /草稿|已发布|字数/.test(clean(e.innerText))
                       && clean(e.innerText).length < 40)
          .map(e => clean(e.innerText));
      const btns = Array.from(document.querySelectorAll('button')).filter(vis)
          .map(b => clean(b.innerText)).filter(Boolean);
      return { draft_bar: bar.slice(0, 4), buttons: btns.slice(0, 25),
               already_published: btns.some(t => t === '编辑回答'
                                                 || t === '修改回答') };
    }"""

    # ★ 2026-09-28 真机定位的发布被拒真因：
    #   编辑页「发布设置」里的「送礼物设置」默认是「开启送礼物」，而有些问题
    #   根本不支持送礼物 → 服务端直接 403 拒绝整次发布：
    #     POST /api/v4/content/publish -> {"code":403,
    #        "message":"当前问题不支持开启送礼物"}
    #   旧实现用 JS 的 hit.click() 触发，这次提交**根本没发出去**（无任何请求），
    #   于是只能干等 90s 报「未确认到结果」。现在：真实鼠标点击 + 读服务端回执 +
    #   自动关掉送礼物后重试一次。
    _GIFT_OFF_JS = r"""() => {
      const ZW = String.fromCharCode(8203, 8204, 8205, 65279);
      const clean = s => (s || '').split('').filter(c => ZW.indexOf(c) < 0)
          .join('').replace(/\s+/g, '').trim();
      const vis = e => e.offsetParent !== null;
      const label = document.querySelector('label[for="PublishPanel-RewardSetting-1"]')
          || Array.from(document.querySelectorAll('label'))
              .filter(vis).find(e => clean(e.innerText) === '关闭送礼物');
      if (!label) return {ok: false, reason: 'no-gift-off-option'};
      const input = document.getElementById('PublishPanel-RewardSetting-1');
      if (input && input.checked) return {ok: true, already: true};
      label.click();
      return {ok: true, already: false};
    }"""

    def list_draft_cards(self):
        """打开草稿箱并返回卡片列表（DOM 顺序 = 「编辑于」倒序，最后一张最旧）。"""
        self.page.goto(self._DRAFT_URL, wait_until="domcontentloaded",
                       timeout=_NAV_TIMEOUT)
        time.sleep(5)
        for _ in range(6):          # 滚动加载（列表分页/懒加载）
            self._safe_evaluate(
                "() => { window.scrollTo(0, document.body.scrollHeight); return true; }")
            time.sleep(1.2)
        return self._safe_evaluate(self._DRAFT_CARDS_JS) or []

    def publish_draft(self, qid="", verify_timeout=90, progress=None, dry_run=False):
        """发布草稿箱里的一篇草稿（qid 为空 = 发布**最旧**的一篇）。

        返回 {"ok", "qid", "title", "url", "detail"}；草稿箱为空时
        额外带 "reason": "empty"，便于调用方与真正的发布失败区分。

        dry_run=True 只做演练：定位草稿 → 打开编辑页 → 确认「发布回答」按钮存在，
        **不点击**，返回 reason="dry_run"（供首次验证链路，不会真的公开）。

        ★ 不可逆（草稿变公开回答）：调用方必须先取得用户授权（自动化模块只在
          计划里声明的数量/顺序下调用，且失败不盲目重试）。
        """
        def _say(text):
            """进度回调只传文本：调度器/界面各自决定怎么呈现（本层不碰 UI 结构）。"""
            log.info("browser_adapter: %s", text)
            if progress:
                try:
                    progress(text)
                except Exception:      # noqa: BLE001
                    pass

        _say("打开草稿箱…")
        cards = self.list_draft_cards()
        if page_needs_login(self.page):
            # 登录态失效：草稿箱页被 302 到 /signin → 卡片必然是 0 张。
            # 不识别就会误报成「草稿箱里没有可发布的草稿」，调度器把它记
            # 「跳过」（不失败、不熔断、不通知）→ 无人值守时静默空转，用户
            # 永远等不到「该重新登录了」这句话（2026-09-23 修）。reason=
            # need_login 由执行器转成 NeedHuman：暂停自动化 + 通知人工。
            detail = ("知乎登录态已失效（草稿箱页被重定向到登录页），请先在"
                      "控制台「设置 → 知乎账号」重新登录知乎")
            return {"ok": False, "reason": "need_login", "qid": "",
                    "title": "", "url": self.page.url or "", "detail": detail}
        _say("草稿箱可见 %d 篇草稿" % len(cards))
        target = None
        if qid:
            target = next((c for c in cards if str(c.get("qid")) == str(qid)), None)
        elif cards:
            target = cards[-1]        # 倒序列表的最后一张 = 最旧
        if not target:
            # reason=empty：调度器据此记「跳过」而不是「失败」——
            # 草稿箱空是正常状态（今天还没写、或已发完），不该触发熔断。
            return {"ok": False, "reason": "empty", "qid": "", "title": "",
                    "url": "",
                    "detail": "草稿箱里没有可发布的草稿" + ("（找不到 qid=%s）" % qid if qid else "")}
        _say("准备发布最旧的一篇：《%s》" % (target.get("title") or target.get("qid")))
        self.page.goto(target["href"], wait_until="domcontentloaded",
                       timeout=_NAV_TIMEOUT)
        time.sleep(6)
        # ★ 回执监听必须在**任何可能触发发布的动作之前**挂上。
        watch = self._arm_publish_watch()

        def _finish(ok, url, detail, **extra):
            """统一收尾：摘监听 + 出结果（避免任何分支漏摘）。

            ★ 报失败之前先跟草稿箱核对一次：宁可多一次只读检查，
              也绝不报出「界面失败、账号里却有新回答」这种自相矛盾的结果。
            """
            if not ok and not extra.get("rehearsed") and not extra.get("certain"):
                published, why = self._reconcile_failure(target)
                if published:
                    self._disarm_publish_watch(watch)
                    _say("核对草稿箱后确认：本次发布其实已成功（%s）" % why)
                    return {"ok": True, "qid": target.get("qid", ""),
                            "title": target.get("title", ""),
                            "url": url or (self.page.url or ""),
                            "detail": why}
            self._disarm_publish_watch(watch)
            out = {"ok": ok, "qid": target.get("qid", ""),
                   "title": target.get("title", ""), "url": url or "",
                   "detail": detail}
            out.update(extra)
            return out

        # 进页面先看结果：可能上一步（草稿箱页/导航）就已把发布发出去了。
        # trust_draft=False：还没点过发布，草稿接口读回空不能当成功
        # （首帧未加载也会读回空，那会「把没发的当成发了」）。
        done, url0, why0 = self._publish_already_done(trust_draft=False)
        if done:
            _say("发布成功（%s）" % why0)
            return _finish(True, url0, why0)
        # 等编辑器就绪（草稿正文可能还在异步填充）
        for _ in range(10):
            ready = self._safe_evaluate(
                "() => !!document.querySelector('.public-DraftEditor-content')")
            if ready:
                break
            time.sleep(1.5)
        if dry_run:
            # 演练：只确认「发布回答」按钮在，绝不点击（发布不可逆）
            btn = self._safe_evaluate(self._PUBLISH_BTN_JS, False)
            if not btn:
                return _finish(False, self.page.url or "",
                               "演练未通过：编辑器里没找到「发布回答」按钮",
                               reason="dry_run", rehearsed=False)
            _say("演练通过：已定位《%s》与「发布回答」按钮（未点击）"
                 % (target.get("title") or target.get("qid")))
            return _finish(False, self.page.url or "",
                           "演练通过：草稿箱 %d 篇，将发最旧的一篇《%s》，未点击发布"
                           % (len(cards), target.get("title") or target.get("qid")),
                           reason="dry_run", rehearsed=True)
        # ★ 这里**不再**用 _PUBLISH_BTN_JS 做「预检点击」：
        #   它在非演练时是真的会点按钮的，等于把发布提前到点击函数之外——
        #   2026-09-28 线上事故就是它发出的发布，随后真实点击因按钮已变灰而超时，
        #   整次发布被误报成「no-button 失败」。现在只有一条点击路径。
        try:
            clicked = self._click_publish_native()
            if not clicked.get("ok"):
                # 点击「失败」先别急着定性：请求可能已经发出去了。
                # 判据只看结果——URL 跳到回答页 / 服务端草稿已清空 / 回执 code=0。
                done, url, why = self._recheck_after_click_gap(watch)
                if done:
                    _say("发布成功（%s）" % why)
                    return _finish(True, url, why)
                verdict = _publish_verdict(
                    clicked_ok=False, click_reason=clicked.get("reason"),
                    url=self.page.url or "",
                    draft_pending=self._draft_still_pending(target.get("qid")))
                _say(verdict["detail"])
                return _finish(verdict["ok"], self.page.url or "",
                               verdict["detail"],
                               certain=verdict["certain"])
            log.info("browser_adapter: 已用 %s 方式点击「发布回答」",
                     clicked.get("how"))
        except Exception:
            # 点击本身炸了（页面/上下文已关）：先摘监听再往上抛，
            # 否则长驻进程里监听器会越积越多
            self._disarm_publish_watch(watch)
            raise
        _say("已点「发布回答」，等待确认…")
        time.sleep(2)
        # 确认弹窗（「确认发布」/「发布设置」）不是点完立刻出现——只查一次会漏，
        # 漏了就等于「点了主按钮、没点确认」→ 服务端什么都没发生（2026-09-28 修）。
        for _ in range(5):
            hit = self._safe_evaluate(self._PUBLISH_CONFIRM_JS) or ""
            if hit:
                _say("已确认发布弹窗（%s）" % hit)
                break
            time.sleep(1.5)
        # ★ 先把「点完这一下」的现场记下来：真出问题时，日志里能直接看到
        #   按钮是否还在、草稿状态条写的什么、页面上有没有报错文案。
        state_after = self._safe_evaluate(self._DRAFT_STATE_JS)
        state_after = state_after if isinstance(state_after, dict) else {}
        log.info("browser_adapter: 点发布后状态 url=%s bar=%s err=%s",
                 self.page.url or "", state_after.get("draft_bar"),
                 self._safe_evaluate(self._PUBLISH_ERROR_JS) or [])

        deadline = time.time() + max(30, int(verify_timeout))
        start = time.time()
        empty_hits = 0
        last_log = 0.0
        gift_retried = False
        receipt_ok = False          # 服务端回过 code=0（这次发布已被受理）
        while time.time() < deadline:
            url = self.page.url or ""
            m = re.search(r"/answer/(\d+)", url)
            if m:
                _say("发布成功：%s" % url)
                self._disarm_publish_watch(watch)
                return {"ok": True, "qid": target.get("qid", ""),
                        "title": target.get("title", ""), "url": url,
                        "detail": "页面已跳到回答页"}
            # 服务端回执优先：它是「到底发出去没有」的唯一权威答案。
            # /api/v4/content/publish 用 HTTP 200 + body.code 表达业务结果，
            # 不读它就会把「被拒绝」当成「在路上」干等到超时（2026-09-28 真机）。
            receipt = self._publish_receipt(watch)
            if receipt.get("ok") is False:
                why = receipt.get("detail") or "服务端拒绝了发布"
                if not gift_retried and "送礼物" in why:
                    # 「当前问题不支持开启送礼物」：发布设置里的默认选项和这个问题
                    # 冲突，关掉它再发一次即可（真机确认的错误码 403）。
                    gift_retried = True
                    _say("服务端拒绝（%s），关掉「送礼物设置」后重试一次…" % why)
                    if self._open_publish_settings() or self._gift_settings_reachable():
                        self._turn_gift_off()
                    time.sleep(1.5)
                    self._reset_publish_watch(watch)   # 只看重试之后的新回执
                    reclick = self._click_publish_native()
                    if not reclick.get("ok"):
                        self._disarm_publish_watch(watch)
                        return {"ok": False, "qid": target.get("qid", ""),
                                "title": target.get("title", ""),
                                "url": self.page.url or "",
                                "detail": "关闭送礼物后重试失败：找不到「发布回答」按钮"}
                    time.sleep(2)
                    continue
                # 其它业务失败：如实上报（连同服务端原话），别让用户猜
                self._dump_page_state("publish-rejected")
                self._disarm_publish_watch(watch)
                return {"ok": False, "qid": target.get("qid", ""),
                        "title": target.get("title", ""),
                        "url": self.page.url or "",
                        "detail": "服务端拒绝发布：%s" % why}
            if receipt.get("ok") is True:
                receipt_ok = True
            if not self.get_draft_content():
                # 连续两次读到空才算数：单次空读可能是接口抖动/首帧未加载
                empty_hits += 1
                if empty_hits >= 2:
                    _say("发布成功（服务端草稿已清空）")
                    self._disarm_publish_watch(watch)
                    return {"ok": True, "qid": target.get("qid", ""),
                            "title": target.get("title", ""), "url": url,
                            "detail": "服务端草稿已清空（已发布）"}
            else:
                empty_hits = 0
                # ★ 服务端已回过 code=0（发布被受理）但草稿还没清空、URL 也没变：
                #   不再干等到超时。知乎的草稿清理可能滞后（或在审核中），
                #   这时如实报「已受理但未确认」比「未确认到结果 = 失败」准确得多，
                #   尤其不能在重试成功后还把整次发布判失败（2026-09-28 测试抓到）。
                if receipt_ok:
                    _say("服务端已受理发布（code=0），但页面/草稿未及时刷新")
                    self._disarm_publish_watch(watch)
                    return {"ok": True, "qid": target.get("qid", ""),
                            "title": target.get("title", ""), "url": url,
                            "detail": "发布请求已被服务端受理（草稿尚未刷新，"
                                      "可在创作中心核对）"}
            now = time.time()
            if now - last_log >= 15:
                last_log = now
                _say("等待发布确认… 已等 %.0fs/%ds" % (now - start, verify_timeout))
            time.sleep(2)

        # 超时：把现场拍成截图 + 明确诊断，别只丢一句「请人工核对」。
        # 真机经验（2026-09-28）：发布请求可能**根本没发出去**（JS 点击不被受理）
        # 或被服务端 200+code 拒绝——两者都表现为「URL 不变、草稿不清空」。
        receipt = self._publish_receipt(watch)
        errs = self._safe_evaluate(self._PUBLISH_ERROR_JS)
        errs = [str(x) for x in errs] if isinstance(errs, (list, tuple)) else []
        shot = self._dump_page_state("publish-timeout")
        if receipt.get("ok") is None and not errs:
            detail = ("点了发布但服务端**没有收到**发布请求（浏览器侧被拦/按钮无效），"
                      "%ds 后仍无结果" % verify_timeout)
        else:
            detail = "已点发布，但 %ds 内未确认到结果" % verify_timeout
        if receipt.get("detail"):
            detail += "；服务端回执：%s" % receipt["detail"]
        if errs:
            detail += "；页面提示：%s" % " / ".join(errs[:2])
        if shot:
            detail += "；现场截图：%s" % shot
        detail += "（已核对草稿箱仍在此篇，请人工核对回答页）"
        verdict = _publish_verdict(
            clicked_ok=True, click_reason="", url=self.page.url or "",
            draft_pending=self._draft_still_pending(target.get("qid")),
            receipt_ok=receipt.get("ok"), receipt_detail=receipt.get("detail") or "",
            errors=errs)
        if verdict["detail"]:
            detail = verdict["detail"] + "；" + detail
        return _finish(verdict["ok"], self.page.url or "", detail,
                       certain=verdict["certain"])

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
