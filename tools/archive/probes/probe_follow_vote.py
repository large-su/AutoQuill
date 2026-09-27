# -*- coding: utf-8 -*-
# ============================================================
# probe_follow_vote.py - 真机验证「关注 / 取关 / 赞同 / 取消赞同」全流程
#
# ★ 这是**真实动作**探针（用户 2026-09-27 明确授权）：会对一位知友执行
#   关注 → 取关 → 再关注，并对其回答执行 赞同 → 取消赞同 → 再赞同。
#   目的是把「取关要不要二次确认」「取消赞同要不要确认」「按钮状态怎么变」
#   这些流程全部摸清，避免程序写完才发现走不通。
#
# 目标选择：问题页上第一个「未关注」且「非自己」的回答作者（自动挑）。
# 每一步都 dump 状态 + 截图，最后汇总成 JSON。
# 用法：
#   PYTHONIOENCODING=utf-8 AQ_DATA_DIR="$APPDATA/AutoQuill" .venv/Scripts/python \
#       tools/archive/probes/probe_follow_vote.py [问题URL]
# ============================================================

import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEFAULT_URL = "https://www.zhihu.com/question/1988024921025168911"

STATE_JS = """
(idx) => {
  const zw = new RegExp("[" + String.fromCharCode(8203,8204,8205,65279) + "]", "g");
  const clean = s => (s || "").replace(zw, " ").replace(/[ ]+/g, " ").trim();
  const txt = el => clean((el && (el.innerText || el.textContent)) || "");
  const cls = el => (el && typeof el.className === "string" ? el.className : "");
  const items = Array.from(document.querySelectorAll(".AnswerItem,.QuestionAnswer-content"));
  const it = (idx === null || idx === undefined) ? null : items[idx];
  const info = it ? {
    author: (() => { const a = it.querySelector("a[href*='/people/']"); return a ? txt(a).slice(0, 20) : ""; })(),
    authorHref: (() => { const a = it.querySelector("a[href*='/people/']"); return a ? a.getAttribute("href") : ""; })(),
    follow: (() => { const b = it.querySelector("button.FollowButton"); return b ? { text: txt(b), cls: cls(b).slice(0, 100) } : null; })(),
    vote: (() => { const b = it.querySelector("button.VoteButton"); return b ? { text: txt(b), cls: cls(b).slice(0, 120), disabled: !!b.disabled } : null; })(),
  } : null;
  const allButtons = Array.from(document.querySelectorAll("button")).filter(b => b.offsetParent)
      .map(b => ({ text: txt(b).slice(0, 16), cls: cls(b).slice(0, 70), disabled: !!b.disabled })).filter(b => b.text);
  const popovers = Array.from(document.querySelectorAll("div,li,span")).filter(el => {
    const t = txt(el);
    return t && t.length < 12 && /取消关注|确定取消|确认取消|取消赞同|确定|确认/.test(t) && el.offsetParent;
  }).map(el => ({ tag: el.tagName, text: txt(el), cls: cls(el).slice(0, 70) })).slice(0, 12);
  const captcha = Array.from(document.querySelectorAll("div,iframe")).filter(el => /验证|安全验证|滑动|captcha/i.test(cls(el) + " " + (el.id || ""))).length;
  return { item: info, buttons: allButtons.slice(0, 40), popovers: popovers, captchaHints: captcha,
           bodyTail: txt(document.body).slice(-300) };
}
"""

PICK_TARGET_JS = """
() => {
  const zw = new RegExp("[" + String.fromCharCode(8203,8204,8205,65279) + "]", "g");
  const clean = s => (s || "").replace(zw, " ").replace(/[ ]+/g, " ").trim();
  const txt = el => clean((el && (el.innerText || el.textContent)) || "");
  const items = Array.from(document.querySelectorAll(".AnswerItem,.QuestionAnswer-content"));
  const out = [];
  items.forEach((it, i) => {
    const a = it.querySelector("a[href*='/people/']");
    const f = it.querySelector("button.FollowButton");
    const v = it.querySelector("button.VoteButton");
    out.push({ i: i, author: a ? txt(a).slice(0, 20) : "", href: a ? a.getAttribute("href") : "",
               follow: f ? txt(f) : "(无)", vote: v ? txt(v) : "(无)", voteDisabled: v ? !!v.disabled : null });
  });
  const pick = out.find(o => o.follow === "关注" && o.voteDisabled === false);
  return { items: out, pickIndex: pick ? pick.i : -1, pick: pick || null };
}
"""

CLICK_JS = """
(arg) => {
  const zw = new RegExp("[" + String.fromCharCode(8203,8204,8205,65279) + "]", "g");
  const clean = s => (s || "").replace(zw, " ").replace(/[ ]+/g, " ").trim();
  const txt = el => clean((el && (el.innerText || el.textContent)) || "");
  const idx = arg.idx;
  const kind = arg.kind;
  const pattern = arg.pattern;
  const items = Array.from(document.querySelectorAll(".AnswerItem,.QuestionAnswer-content"));
  const it = idx === null ? null : items[idx];
  if (kind === "follow" || kind === "vote") {
    if (!it) return { ok: false, reason: "没有该回答" };
    const sel = kind === "follow" ? "button.FollowButton" : "button.VoteButton";
    const b = it.querySelector(sel);
    if (!b) return { ok: false, reason: "没有按钮 " + sel };
    b.click();
    return { ok: true, clicked: txt(b).slice(0, 16) };
  }
  const nodes = Array.from(document.querySelectorAll("button,div,span,li,a"));
  const hit = nodes.filter(el => el.offsetParent && txt(el) === pattern).pop();
  if (!hit) return { ok: false, reason: "没找到文本为 " + pattern + " 的元素" };
  hit.click();
  return { ok: true, clicked: pattern, tag: hit.tagName };
}
"""

HOVER_FOLLOW_JS = """
(idx) => {
  const items = Array.from(document.querySelectorAll(".AnswerItem,.QuestionAnswer-content"));
  const it = items[idx];
  if (!it) return false;
  const b = it.querySelector("button.FollowButton");
  if (!b) return false;
  ["mouseenter", "mouseover", "mousemove"].forEach(t => {
    b.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window }));
  });
  return true;
}
"""


def snap(b, name, outdir, dumps):
    time.sleep(2.5)
    st = b._safe_evaluate(STATE_JS, dumps.get("idx")) or {}
    dumps.setdefault("steps", []).append({"step": name, "state": st})
    try:
        b.page.screenshot(path=os.path.join(outdir, name + ".png"))
    except Exception as exc:
        print("    (截图失败：%s)" % str(exc)[:60])
    it = st.get("item") or {}
    print("  [%s] follow=%s vote=%s captcha=%s" % (
        name, (it.get("follow") or {}).get("text"), (it.get("vote") or {}).get("text"), st.get("captchaHints")))
    pops = st.get("popovers") or []
    if pops:
        print("        弹层候选：", [(p.get("tag"), p.get("text")) for p in pops])
    return st


def main():
    from applications.zhihu_story.browser_adapter import ZhihuBrowser
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    outdir = os.path.join("data", "cleanup", "follow_vote_%s" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(outdir, exist_ok=True)
    dumps = {"url": url, "steps": []}
    b = ZhihuBrowser(headless=True)
    try:
        b.start()
        # 先打开推荐问题页，挑一个首页作者还没关注的题（找不到就顺着候选往下试）
        cand_urls = [url]
        try:
            b.open_recommend_page()
            qs = b.get_recommend_questions(max_cards=10) or []
            for q in qs:
                if q.get("href") and q["href"] not in cand_urls:
                    cand_urls.append(q["href"])
        except Exception as exc:
            print("（推荐页候选获取失败：%s）" % str(exc)[:80])
        print("候选问题 %d 个" % len(cand_urls))
        idx = -1
        pick = {}
        for cu in cand_urls[:8]:
            b.page.goto(cu, wait_until="domcontentloaded", timeout=45000)
            time.sleep(7)
            pick = b._safe_evaluate(PICK_TARGET_JS) or {}
            print("=== %s ===" % cu[:70])
            for it in pick.get("items") or []:
                print("   #%s %s | 关注按钮=%s | 赞同=%s (disabled=%s)" % (it.get("i"), it.get("author"), it.get("follow"), it.get("vote"), it.get("voteDisabled")))
            idx = pick.get("pickIndex") if pick.get("pickIndex") is not None else -1
            if idx is not None and idx >= 0:
                break
        dumps["pick"] = pick
        dumps["candidates"] = cand_urls[:8]
        print("=== 选中目标 #%s：%s ===" % (idx, (pick.get("pick") or {}).get("author")))
        if idx is None or idx < 0:
            print("!! 没有找到「未关注 + 可赞同」的目标，流程中止")
            return 1
        dumps["idx"] = idx
        snap(b, "00_初始", outdir, dumps)

        print("--- 1) 点关注 ---")
        print("   ", b._safe_evaluate(CLICK_JS, {"idx": idx, "kind": "follow"}))
        snap(b, "01_点关注后", outdir, dumps)

        print("--- 2) 点已关注（看是否弹层） ---")
        print("   ", b._safe_evaluate(CLICK_JS, {"idx": idx, "kind": "follow"}))
        st = snap(b, "02_点已关注后", outdir, dumps)
        if not (st.get("popovers") or []):
            print("    （没看到弹层，试 hover 触发）")
            b._safe_evaluate(HOVER_FOLLOW_JS, idx)
            st = snap(b, "02b_hover后", outdir, dumps)

        print("--- 3) 点取消关注 ---")
        r = b._safe_evaluate(CLICK_JS, {"idx": None, "kind": "text", "pattern": "取消关注"})
        print("   ", r)
        if not r.get("ok"):
            print("    （没找到「取消关注」，改 hover 后重试）")
            b._safe_evaluate(HOVER_FOLLOW_JS, idx)
            time.sleep(2)
            r = b._safe_evaluate(CLICK_JS, {"idx": None, "kind": "text", "pattern": "取消关注"})
            print("   ", r)
        st = snap(b, "03_点取消关注后", outdir, dumps)
        # 若出现确认弹窗，点确认
        for label in ("确定", "确认", "确定取消"):
            rr = b._safe_evaluate(CLICK_JS, {"idx": None, "kind": "text", "pattern": label})
            if rr.get("ok"):
                print("    → 出现确认按钮，点了「%s」" % label)
                snap(b, "03b_确认取消关注后", outdir, dumps)
                break

        print("--- 4) 再点关注（恢复） ---")
        print("   ", b._safe_evaluate(CLICK_JS, {"idx": idx, "kind": "follow"}))
        snap(b, "04_重新关注后", outdir, dumps)

        print("--- 5) 点赞同 ---")
        print("   ", b._safe_evaluate(CLICK_JS, {"idx": idx, "kind": "vote"}))
        snap(b, "05_点赞同后", outdir, dumps)

        print("--- 6) 再点一次（取消赞同？） ---")
        print("   ", b._safe_evaluate(CLICK_JS, {"idx": idx, "kind": "vote"}))
        st = snap(b, "06_再点赞同后", outdir, dumps)
        for label in ("确定", "确认", "取消赞同"):
            rr = b._safe_evaluate(CLICK_JS, {"idx": None, "kind": "text", "pattern": label})
            if rr.get("ok"):
                print("    → 出现「%s」，点了" % label)
                snap(b, "06b_确认取消赞同后", outdir, dumps)
                break

        print("--- 7) 再点赞同（恢复已赞同） ---")
        print("   ", b._safe_evaluate(CLICK_JS, {"idx": idx, "kind": "vote"}))
        snap(b, "07_最终", outdir, dumps)
    finally:
        b.close()
    path = os.path.join(outdir, "result.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dumps, f, ensure_ascii=False, indent=2)
    print("=" * 60)
    print("全过程已存：%s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())