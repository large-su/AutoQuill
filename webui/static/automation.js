/* ============================================================
   automation.js —— 自动化模块前端（24 小时时间轴 · 无人化调度）

   设计口径（用户 2026-09-19 定）：
     · 时间轴要「清晰好看」：24 小时刻度 + 每类任务一条泳道 + 状态配色 + 现在指针；
     · 全部只读展示 + 几个开关（开始/暂停/停止/立即执行/演练发布），配置改动即存；
     · 排班是「时段内铺开」而不是「发一个等一小时」：任务卡上写明
       上限 = 时段分钟 ÷ 最小间隔 + 1，设多了要当场提示排不下；
     · 独立文件，避免把 app.js 撑大（app.js 里的 $ / esc / LEFT_MODES 直接复用）。
   ============================================================ */

let autoData = null;
let autoTimer = null;
let autoSaveTimer = null;

/* ── 表单编辑保护（2026-09-25 修：用户实测「每日 N 篇」改几遍都弹回旧值）────
   事故链：面板每 4 秒轮询一次状态并**整块重建**配置表单（box.innerHTML=…），
   于是 ① 正在输入的框被销毁重建 → 刚敲的数字被清掉、焦点也丢了；
   ② 保存只挂在 change（失焦）上，被清掉的编辑连一次保存都触发不了；
   ③ 重建后用旧值回写服务器 → 服务端也变成旧值，看起来就是「自动改回默认」。
   现在：有未保存编辑、或焦点还在表单里时，轮询只更新数据、**不重建表单**；
   焦点离开（或保存成功）后再补一次渲染。 */
let autoDirty = false;          // 有未保存的编辑
let autoPendingRender = false;  // 被保护期间攒下的「该重建」信号
let autoSaveRetry = 0;          // 保存失败重试次数（有界，避免死循环）

const AUTO_PLAN_INPUT_IDS = ["autoWinStart", "autoWinEnd", "autoMinGap",
                             "autoGapRatio", "autoJitter", "autoCatchUp",
                             "autoPauseBusy"];

function autoFormBusy() {
  const el = document.activeElement;
  const box = $("autoTaskConfig");
  const inTasks = !!(box && el && box.contains(el));
  const inPlan = !!(el && el.id && AUTO_PLAN_INPUT_IDS.indexOf(el.id) >= 0);
  return autoDirty || inTasks || inPlan;
}

function flushAutoRender() {
  if (autoFormBusy() || !autoPendingRender) return;
  autoPendingRender = false;
  renderAutoTaskConfig();
}

const AUTO_STATUS_TEXT = {
  planned: "待执行", running: "执行中", done: "已完成",
  failed: "失败", skipped: "已跳过", needs_human: "需要人工",
};
const AUTO_STATUS_COLOR = {
  planned: "#818cf8", running: "#a78bfa", done: "#34d399",
  failed: "#f87171", skipped: "#3b4762", needs_human: "#fbbf24",
};

// ── 单次任务轴（一天做一次的任务：回复评论 / 打卡互动）────────────────
// 视觉分工（用户 2026-09-27 口径）：**颜色只表达状态**，任务身份用形状 +
// 编号 + 中文标签表达。形状按「动作性质」归类，新增单次任务按性质挑形状即可：
//   菱形 = 回复 / 对话类 · 圆形 = 检查 / 巡检类 · 方形 = 生成 / 写入类 · 三角 = 对外发布类
const SA_SHAPE = { reply_comment: "diamond", checkin: "circle" };
const SA_ROLE = [
  ["diamond", "回复 / 对话类"],
  ["circle", "检查 / 巡检类"],
  ["square", "生成 / 写入类"],
  ["triangle", "对外发布类"],
];
const SA_STAGE_CHAR = { 1: "\u2460", 2: "\u2461", 3: "\u2462", 4: "\u2463" };
const SA_STAGE_TEXT = { reply_comment: "先回复", checkin: "后检查" };

function saShapeOf(t) { return SA_SHAPE[t] || "circle"; }
function saColorOf(s) { return AUTO_STATUS_COLOR[s] || "#818cf8"; }
function saStatusText(s) { return AUTO_STATUS_TEXT[s] || s || "-"; }

function autoPad(n) { return (n < 10 ? "0" : "") + n; }

function autoClock(iso) {
  if (!iso) return "-";
  const parts = String(iso).split("T");
  return parts.length > 1 ? parts[1].slice(0, 5) : String(iso).slice(11, 16);
}

function autoMinutes(iso) {
  if (!iso) return -1;
  const t = String(iso).split("T")[1] || "";
  const hm = t.split(":");
  if (hm.length < 2) return -1;
  return parseInt(hm[0], 10) * 60 + parseInt(hm[1], 10);
}

function autoHHMMToMin(s) {
  const p = String(s || "0:0").split(":");
  return (parseInt(p[0], 10) || 0) * 60 + (parseInt(p[1], 10) || 0);
}

function autoCountdown(nextIso, nowIso) {
  if (!nextIso || !nowIso) return "-";
  const a = new Date(nextIso).getTime(), b = new Date(nowIso).getTime();
  if (isNaN(a) || isNaN(b)) return "-";
  let mins = Math.round((a - b) / 60000);
  if (mins < 0) mins = 0;
  if (mins < 60) return mins + " 分钟后";
  return Math.floor(mins / 60) + " 小时 " + (mins % 60) + " 分钟后";
}

async function loadAutomation() {
  try {
    const r = await fetch("/api/automation");
    autoData = await r.json();
  } catch (e) { return; }
  renderAutoState();
  renderAutoProgress();
  renderAutoTimeline();
  renderAutoSingleAxis();
  renderAutoSingleCards();
  renderAutoTaskConfig();
  renderAutoNotices();
  renderAutoHistory();
}

function startAutoPoll() {
  loadAutomation();
  if (!autoTimer) autoTimer = setInterval(loadAutomation, 4000);
}

function stopAutoPoll() {
  if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
}

function renderAutoState() {
  const st = autoData || {};
  const el = $("autoState"), pill = $("autoStatePill");
  let text = "未启用", cls = "";
  if (st.running) { text = "执行中：" + (st.running.type || ""); cls = "busy"; }
  else if (st.paused) { text = "已暂停"; cls = "paused"; }
  else if (st.enabled) { text = "运行中"; cls = "on"; }
  else if (st.summary && st.summary.in_window === false) { text = "运行中 · 时段外待机"; }
  if (el) { el.textContent = text; el.className = "auto-state " + cls; }
  if (pill) pill.textContent = text;
  const on = !!st.enabled;
  if ($("autoStartBtn")) $("autoStartBtn").hidden = on;
  if ($("autoStopBtn")) $("autoStopBtn").hidden = !on;
  if ($("autoPauseBtn")) {
    $("autoPauseBtn").hidden = !on;
    $("autoPauseBtn").textContent = st.paused ? "继续" : "暂停";
  }
  const hint = $("autoHint");
  if (hint) {
    const s = (st.summary || {});
    hint.textContent = st.paused
      ? ("已暂停：" + (st.pause_reason || "") + "（点「继续」恢复）")
      : ("时段 " + (s.window_label || "-") + " · 今天已完成 " + (s.done_total || 0)
         + "/" + (s.plan_total || 0) + " 项 · 错过补做 " + (s.rescheduled || 0) + " 次");
  }
}

function renderAutoProgress() {
  const box = $("autoProgress");
  if (!box) return;
  const st = autoData || {};
  const s = st.summary || {};
  const per = s.per_type || {};
  const order = Object.keys(per).sort(function (a, b) { return per[a].lane - per[b].lane; });
  let html = "";
  order.forEach(function (t) {
    const m = per[t];
    if (!m.implemented || !m.enabled) return;
    const pct = m.cap ? Math.min(100, Math.round(m.done * 100 / m.cap)) : 0;
    const single = m.job_mode === "single";
    // 单次任务（打卡互动）的「每日数量」是班次不是条数：显示成「0/1 项」
    // 会让人以为还差一项配额。这里按语义显示：一天一班 + 当前状态。
    const value = (single && m.cap <= 1)
      ? "<span class=\"sub\">一天一班</span>"
      : m.done + "<span class=\"sub\"> / " + m.cap + " " + esc(m.unit) + "</span>";
    const jobs = (st.schedule || []).filter(function (j) { return j.type === t; });
    const jstatus = jobs.length ? (AUTO_STATUS_TEXT[jobs[0].status] || jobs[0].status) : "未排班";
    const when = (jobs.length && jobs[0].planned_at) ? autoClock(jobs[0].planned_at) : "-";
    const desc = single
      ? ("窗口 " + esc((m.window || {}).start || "-") + "–" + esc((m.window || {}).end || "-")
         + " · " + jstatus + " " + when)
      : ("窗口 " + esc((m.window || {}).start || "-") + "–" + esc((m.window || {}).end || "-")
         + " · 间隔 ≥ " + m.min_gap_minutes + " 分钟");
    html += "<div class=\"auto-pcard\">"
      + "<div class=\"l\">" + esc(m.label) + "</div>"
      + "<div class=\"v\">" + value + "</div>"
      + "<div class=\"d\">" + desc + "</div>"
      + "<div class=\"bar\"><i style=\"width:" + pct + "%\"></i></div>"
      + "</div>";
  });
  const next = (s.next_job || {});
  const nextLabel = next.type ? ((per[next.type] || {}).label || next.type) : "";
  html += "<div class=\"auto-pcard\"><div class=\"l\">下一次执行</div>"
    + "<div class=\"v\">" + esc(autoClock(next.planned_at)) + "</div>"
    + "<div class=\"d\">" + (next.type
        ? esc(nextLabel) + " · " + esc(autoCountdown(next.planned_at, st.now))
        : "今天没有待执行任务") + "</div></div>";
  html += "<div class=\"auto-pcard\"><div class=\"l\">运行时段</div>"
    + "<div class=\"v\">" + esc(s.window_label || "-") + "</div>"
    + "<div class=\"d\">" + (s.in_window ? "当前在时段内" : "当前不在时段内（时段外不派活，程序不退出）")
    + "</div></div>";
  box.innerHTML = html;
}

function renderAutoTimeline() {
  const box = $("autoTimeline");
  if (!box) return;
  const st = autoData || {};
  const s = st.summary || {};
  const per = s.per_type || {};
  const plan = st.plan || {};
  const win = plan.window || {};
  const wStart = autoHHMMToMin(win.start), wEnd = autoHHMMToMin(win.end);
  const nowMin = autoMinutes(st.now);
  let html = "<div class=\"tl-ruler\"><div></div><div class=\"tl-ticks\">";
  for (let h = 0; h <= 24; h += 2) {
    html += "<span style=\"left:" + (h / 24 * 100) + "%\">" + autoPad(h) + ":00</span>";
  }
  html += "</div></div>";
  // 单次任务（axis=single）不画在这里：它们有自己的「单次任务轴」
  const order = Object.keys(per).filter(function (t) {
    return (per[t].axis || "") !== "single";
  }).sort(function (a, b) { return per[a].lane - per[b].lane; });
  order.forEach(function (t) {
    const m = per[t];
    const jobs = (st.schedule || []).filter(function (j) { return j.type === t; });
    const off = (!m.enabled || !m.implemented) ? " off" : "";
    const capText = m.implemented
      ? (m.enabled ? (m.done + "/" + m.cap + " " + m.unit) : "未启用")
      : "待接入（预留）";
    html += "<div class=\"tl-lane\"><div class=\"tl-lane-label" + off + "\">"
      + esc(m.label) + "<span class=\"cap\">" + capText + "</span></div>";
    html += "<div class=\"tl-track\">";
    html += "<div class=\"tl-window\" style=\"left:" + (wStart / 1440 * 100)
      + "%;width:" + ((wEnd - wStart) / 1440 * 100) + "%\"></div>";
    jobs.forEach(function (j) {
      if (!j.planned_at) return;
      const mMin = autoMinutes(j.planned_at);
      if (mMin < 0) return;
      const left = Math.max(0, Math.min(99.4, mMin / 1440 * 100));
      const title = (AUTO_STATUS_TEXT[j.status] || j.status) + " · " + autoClock(j.planned_at)
        + (j.note ? " · " + j.note : "");
      html += "<div class=\"tl-block " + j.status + "\" data-key=\"" + esc(j.key)
        + "\" style=\"left:" + left + "%\" title=\"" + esc(title) + "\"></div>";
    });
    if (nowMin >= 0) {
      html += "<div class=\"tl-now\" style=\"left:" + (nowMin / 1440 * 100) + "%\"></div>";
    }
    html += "</div></div>";
  });
  html += "<div class=\"tl-legend\">";
  ["planned", "running", "done", "failed", "skipped", "needs_human"].forEach(function (k) {
    html += "<span><i style=\"background:" + AUTO_STATUS_COLOR[k] + "\"></i>"
      + AUTO_STATUS_TEXT[k] + "</span>";
  });
  html += "<span style=\"margin-left:auto\">竖线 = 现在 · 淡紫底 = 运行时段</span></div>";
  box.innerHTML = html;
  box.querySelectorAll(".tl-block").forEach(function (el) {
    el.addEventListener("click", function () { showAutoJob(el.dataset.key); });
  });
}

// 单次任务轴：与主时间轴共用刻度 / 泳道行 / 时段底 / 现在线，
// 只是把「长条块」换成「药丸标记」（形状=任务身份，底色=状态）。
function saCapText(j) {
  const parts = [];
  if (j.dry_run) parts.push("演练");
  parts.push(saStatusText(j.status));
  return parts.join(" · ");
}

function renderAutoSingleAxis() {
  const box = $("autoSingleAxis");
  if (!box) return;
  const st = autoData || {};
  const single = st.single || {};
  const axis = single.axis || [];
  const nowMin = autoMinutes(st.now);
  const head = '<div class="sa-head"><span class="sa-title">◎ 单次任务轴</span>'
    + '<span class="sa-hint">一天一次 · 按 ①→② 顺序执行（前面没结束，后面的不动手）</span></div>';
  if (!axis.length) {
    box.innerHTML = head + '<div class="empty-note">今天没有单次任务：在下方「任务与配额」里打开「打卡互动」或「回复评论」</div>';
    return;
  }
  let html = head + '<div class="tl">';
  html += '<div class="tl-ruler"><div></div><div class="tl-ticks">';
  for (let h = 0; h <= 24; h += 2) {
    html += '<span style="left:' + (h / 24 * 100) + '%">' + autoPad(h) + ':00</span>';
  }
  html += '</div></div>';
  axis.forEach(function (j, i) {
    const w = j.window || {};
    const wStart = autoHHMMToMin(w.start), wEnd = autoHHMMToMin(w.end);
    const hasWin = wStart >= 0 && wEnd > wStart;
    const mMin = autoMinutes(j.planned_at);
    const left = mMin >= 0 ? Math.max(1.5, Math.min(98.5, mMin / 1440 * 100)) : 50;
    const when = mMin >= 0 ? autoClock(j.planned_at) : "未排班";
    const stage = SA_STAGE_CHAR[j.stage] || (i + 1);
    const title = j.label + " · " + saStatusText(j.status) + " · " + when
      + (j.note ? " · " + j.note : "");
    html += '<div class="tl-lane"><div class="tl-lane-label">' + esc(j.label)
      + '<span class="cap">' + esc(saCapText(j)) + '</span></div>'
      + '<div class="tl-track">';
    if (hasWin) {
      html += '<div class="tl-window" style="left:' + (wStart / 1440 * 100)
        + '%;width:' + ((wEnd - wStart) / 1440 * 100) + '%"></div>';
    }
    html += '<div class="sa-pill ' + esc(j.status) + '" style="left:' + left
      + '%;--c:' + saColorOf(j.status) + '" data-key="' + esc(j.key || "")
      + '" title="' + esc(title) + '">'
      + '<i class="sa-shape sa-shape-' + saShapeOf(j.type) + '"></i>'
      + '<span class="sa-stage">' + stage + '</span>'
      + '<span class="sa-time">' + when + '</span></div>';
    if (nowMin >= 0) {
      html += '<div class="tl-now" style="left:' + (nowMin / 1440 * 100) + '%"></div>';
    }
    html += '</div></div>';
  });
  html += '<div class="tl-legend">';
  SA_ROLE.forEach(function (r) {
    html += '<span><i class="sa-shape sa-shape-' + r[0] + '" style="--c:#34d399"></i>'
      + r[1] + '</span>';
  });
  html += '<span style="margin-left:auto">底色 = 状态（同主时间轴）· 竖线 = 现在 · 淡紫底 = 该任务允许执行的时段</span>';
  html += '</div></div>';
  box.innerHTML = html;
  box.querySelectorAll(".sa-pill").forEach(function (el) {
    el.addEventListener("click", function () { showAutoJob(el.dataset.key); });
  });
}

function saDropLabel(k) {
  const m = { hostile: "戾气", spam: "引流", replied: "已回过",
              "no-content": "无内容", "no-answer-url": "无回答链接" };
  return m[k] || k;
}

function renderAutoSingleCards() {
  const box = $("autoSingleCards");
  if (!box) return;
  const st = autoData || {};
  const single = st.single || {};
  const ck = single.checkin || {};
  const rp = single.replies || {};
  const axis = single.axis || [];
  const today = rp.today || [];
  if (!axis.length && !today.length && !ck.line) { box.innerHTML = ""; return; }
  let html = "";

  // ── 打卡互动卡 ──
  const ckJob = axis.filter(function (j) { return j.type === "checkin"; })[0] || {};
  const tasks = ck.tasks || {};
  const localDone = ck.done || {};
  const noteAt = {};
  (ck.notes || []).forEach(function (n) {
    if (n && n.kind) noteAt[n.kind] = String(n.at || "").slice(11, 16);
  });
  const pending = ck.pending || [];
  html += '<div class="sa-card"><div class="sa-card-head"><b>打卡互动</b>'
    + '<span class="sa-chip ' + (pending.length ? "warn" : "ok") + '">'
    + (pending.length ? "还差 " + pending.length + " 项" : "今日已达标") + '</span>'
    + (ckJob.planned_at ? '<span class="sa-sub">检查/补做 · ' + autoClock(ckJob.planned_at) + '</span>' : '')
    + '</div><div class="sa-card-body">';
  if (ck.campaign) {
    html += '<div class="sa-line">' + esc(ck.campaign)
      + (ck.checked_at ? ' · 读取于 ' + esc(String(ck.checked_at).slice(11, 16)) : '') + '</div>';
  }
  html += '<div class="sa-line sa-strong">' + esc(ck.line || "（还没读过打卡页，晚上那一班会读）") + '</div>';
  [["follow", "关注 1 位知友"], ["vote", "送出 1 个赞同"],
   ["comment", "发布 1 条评论"]].forEach(function (r) {
    const t = tasks[r[0]];
    if (!t) return;
    let how = t.action || "待完成";
    if (t.done && localDone[r[0]]) how = "自动化" + (noteAt[r[0]] ? " " + noteAt[r[0]] : "");
    else if (t.done) how = "已完成";
    html += '<div class="sa-item"><span class="' + (t.done ? "ok" : "no") + '">'
      + (t.done ? "√" : "×") + '</span><span class="sa-item-name">' + esc(r[1])
      + '</span><span class="sa-item-how">' + esc(how) + '</span></div>';
  });
  const tried = ck.tried || [];
  if (tried.length) {
    html += '<div class="sa-line sa-dim">试过但跳过：' + tried.map(function (x) {
      return esc((x.author || "?") + "（" + (x.reason || "") + "）");
    }).join("、") + '</div>';
  }
  html += '</div></div>';

  // ── 回复评论卡 ──
  const rpJob = axis.filter(function (j) { return j.type === "reply_comment"; })[0] || {};
  const run = rp.last_run || {};
  const dry = rpJob.dry_run;
  html += '<div class="sa-card"><div class="sa-card-head"><b>回复评论</b>'
    + '<span class="sa-chip ' + (dry ? "dry" : "auto") + '">'
    + (dry ? "演练（不发送）" : "自动发送") + '</span>'
    + (rpJob.planned_at ? '<span class="sa-sub">今天 ' + autoClock(rpJob.planned_at) + '</span>' : '')
    + '</div><div class="sa-card-body">';
  if (run.collected != null) {
    const d = run.dropped || {};
    const dropText = Object.keys(d).map(function (k) { return saDropLabel(k) + " " + d[k]; }).join(" · ");
    html += '<div class="sa-line">抓取 <b>' + run.collected + '</b> 条 → 候选 <b>'
      + (run.candidates || 0) + '</b> 条' + (dropText ? '（过滤：' + esc(dropText) + '）' : '') + '</div>';
  } else {
    html += '<div class="sa-line sa-dim">今天还没跑过（每天一班，到点自动执行）</div>';
  }
  today.slice(0, 3).forEach(function (r) {
    const state = r.failed ? "生成不合格" : (r.sent ? "已发送" : (r.dry_run ? "演练" : "未发送"));
    html += '<div class="sa-reply"><div class="sa-cmt">' + esc(String(r.comment || "").slice(0, 60)) + '</div>'
      + '<div class="sa-ans">→ ' + esc(String(r.reply || "").slice(0, 90)) + '</div>'
      + '<div class="sa-meta">' + esc(r.author || "") + " · " + state
      + (r.at ? " · " + esc(String(r.at).slice(11, 16)) : "") + '</div></div>';
  });
  if (today.length > 3) {
    html += '<div class="sa-line sa-dim">另有 ' + (today.length - 3) + ' 条（见下方台账）</div>';
  }
  html += '</div></div>';
  box.innerHTML = html;
}
function showAutoJob(key) {
  const box = $("autoDetail");
  if (!box) return;
  const st = autoData || {};
  const job = (st.schedule || []).filter(function (j) { return j.key === key; })[0];
  if (!job) { box.hidden = true; return; }
  const per = ((st.summary || {}).per_type || {})[job.type] || {};
  const rows = (st.ledger || []).filter(function (r) { return r.key === key; });
  const last = rows.length ? rows[rows.length - 1] : null;
  let html = "<span class=\"k\">任务</span>" + esc(per.label || job.type)
    + "　<span class=\"k\">计划</span>" + esc(autoClock(job.planned_at))
    + "　<span class=\"k\">状态</span>" + esc(AUTO_STATUS_TEXT[job.status] || job.status);
  if (last) {
    html += "　<span class=\"k\">开始</span>" + esc(autoClock(last.started_at))
      + "　<span class=\"k\">结束</span>" + esc(autoClock(last.finished_at))
      + "　<span class=\"k\">产出</span>" + (last.units || 0);
  }
  if (job.note) html += "<br><span class=\"k\">说明</span>" + esc(job.note);
  if (last && (last.artifacts || []).length) {
    html += "<br><span class=\"k\">产物</span>" + esc(last.artifacts.join("、"));
  }
  box.innerHTML = html;
  box.hidden = false;
}

function renderAutoHistory() {
  const box = $("autoHistoryList");
  if (!box) return;
  const st = autoData || {};
  const rows = (st.ledger || []).filter(function (r) { return r.status !== "running"; });
  if (!rows.length) {
    box.innerHTML = "<div class=\"empty-note\">今天还没有执行记录</div>";
    return;
  }
  const per = ((st.summary || {}).per_type || {});
  let html = "";
  rows.slice().reverse().forEach(function (r) {
    const m = per[r.type] || {};
    html += "<div class=\"auto-hist-row\"><span class=\"tm\">"
      + esc(autoClock(r.finished_at || r.started_at)) + "</span>"
      + "<span class=\"st " + r.status + "\">" + esc(AUTO_STATUS_TEXT[r.status] || r.status) + "</span>"
      + "<span>" + esc(m.label || r.type) + "</span>"
      + "<span class=\"ms\">" + esc((r.message || "").slice(0, 90)) + "</span>"
      + (r.units ? ("<span>" + r.units + " " + esc(m.unit || "") + "</span>") : "")
      + "</div>";
  });
  box.innerHTML = html;
}

function renderAutoNotices() {
  const box = $("autoNoticeList");
  if (!box) return;
  const list = (autoData || {}).notices || [];
  if (!list.length) {
    box.innerHTML = "<div class=\"empty-note\">暂无通知</div>";
    return;
  }
  let html = "";
  list.slice().reverse().forEach(function (n) {
    html += "<div class=\"auto-notice " + esc(n.level || "") + "\"><span class=\"t\">"
      + esc(String(n.at || "").slice(11, 16)) + "</span>" + esc(n.text || "") + "</div>";
  });
  box.innerHTML = html;
}

function renderAutoTaskConfig() {
  const box = $("autoTaskConfig");
  if (!box) return;
  if (autoFormBusy()) {          // 用户正在改配置：不重建表单（见文件头说明）
    autoPendingRender = true;
    return;
  }
  const st = autoData || {};
  const plan = st.plan || {};
  const per = ((st.summary || {}).per_type || {});
  const order = Object.keys(per).sort(function (a, b) { return per[a].lane - per[b].lane; });
  let html = "";
  order.forEach(function (t) {
    const m = per[t];
    const cfg = ((plan.tasks || {})[t]) || {};
    html += "<div class=\"auto-task\" data-type=\"" + esc(t) + "\">"
      + "<div class=\"auto-task-head\"><label class=\"auto-check\">"
      + "<input type=\"checkbox\" data-role=\"enabled\"" + (cfg.enabled ? " checked" : "")
      + (m.implemented ? "" : " disabled") + "> " + esc(m.label) + "</label>"
      + "<span class=\"tag" + (m.implemented ? "" : " todo") + "\">"
      + (m.implemented ? "可用" : "待接入") + "</span></div>"
      + "<div class=\"auto-task-desc\">" + esc(m.desc || "") + "</div>"
      + "<div class=\"auto-task-ctl\">每日 <input type=\"number\" data-role=\"cap\" min=\"0\" max=\"99\" value=\""
      + (cfg.daily_cap || 0) + "\"> " + esc(m.unit)
      + "　最小间隔 <input type=\"number\" data-role=\"gap\" min=\"5\" max=\"720\" step=\"5\" value=\""
      + (m.min_gap_minutes || 60) + "\"> 分钟</div>";
    // 上限提示：N 个作业只有 N-1 个间隔 → 时段内最多 floor(时段/间隔)+1 个
    const cap = cfg.daily_cap || 0;
    const maxN = m.max_per_day || 0;
    const over = maxN > 0 && cap > maxN;
    html += "<div class=\"auto-task-note" + (over ? " warn" : "") + "\">"
      + "时段 " + (m.window_minutes || 0) + " 分钟 ÷ 间隔 " + (m.min_gap_minutes || 60)
      + " 分钟 + 1 → 最多 <b>" + maxN + " " + esc(m.unit) + "</b>/天"
      + (over ? "　⚠ 你设了 " + cap + " " + esc(m.unit) + "，多出的 " + (cap - maxN) + " "
                + esc(m.unit) + " 排不下（按上限排班，时间轴上会标原因）" : "")
      + "</div>";
    if (t === "full_chain") {
      const mode = ((cfg.params || {}).mode) || "single";
      html += "<div class=\"auto-task-ctl\">链路 <select data-role=\"mode\">"
        + "<option value=\"single\"" + (mode === "single" ? " selected" : "")
        + ">经典模式（带格式守则）</option>"
        + "<option value=\"clean\"" + (mode === "clean" ? " selected" : "")
        + ">纯净模式（去限制）</option></select></div>";
    }
    if (t === "publish_drafts") {
      html += "<div class=\"auto-task-ctl\">顺序：从旧到新（按草稿更新时间）</div>";
    }
    if (t === "reply_comment") {
      // 演练/自动开关：默认演练（只生成不发送），语气确认后再切自动
      const dry = ((cfg.params || {}).dry_run !== false);
      html += "<div class=\"auto-task-ctl\">模式 <select data-role=\"dryrun\">"
        + "<option value=\"1\"" + (dry ? " selected" : "") + ">演练（只生成不发送）</option>"
        + "<option value=\"0\"" + (!dry ? " selected" : "") + ">自动发送</option></select>"
        + "　一天一班，一次把该回的都回完</div>";
    }
    html += "</div>";
  });
  box.innerHTML = html;
  const w = plan.window || {};
  if ($("autoWinStart")) $("autoWinStart").value = w.start || "08:00";
  if ($("autoWinEnd")) $("autoWinEnd").value = w.end || "23:30";
  if ($("autoMinGap")) $("autoMinGap").value = plan.min_gap_minutes || 60;
  if ($("autoGapRatio")) $("autoGapRatio").value = (plan.gap_jitter_ratio != null ? plan.gap_jitter_ratio : 0.6);
  if ($("autoJitter")) $("autoJitter").value = (plan.jitter_minutes != null ? plan.jitter_minutes : 8);
  if ($("autoCatchUp")) $("autoCatchUp").value = plan.catch_up || "same_day";
  if ($("autoPauseBusy")) $("autoPauseBusy").checked = plan.pause_when_user_busy !== false;
  box.querySelectorAll("input,select").forEach(function (el) {
    // input = 边打边存（500ms 防抖）；change = 失焦/回车兜底
    el.addEventListener("input", queueAutoSave);
    el.addEventListener("change", queueAutoSave);
  });
  box.addEventListener("focusout", function () {
    // 等防抖保存落地（500ms）再放行重建，避免用旧值回写
    setTimeout(flushAutoRender, 700);
  });
}

function collectAutoPlan() {
  const st = autoData || {};
  const plan = JSON.parse(JSON.stringify(st.plan || {}));
  plan.window = {
    start: ($("autoWinStart") || {}).value || "08:00",
    end: ($("autoWinEnd") || {}).value || "23:30",
  };
  plan.min_gap_minutes = parseInt(($("autoMinGap") || {}).value, 10) || 60;
  plan.gap_jitter_ratio = parseFloat(($("autoGapRatio") || {}).value);
  plan.jitter_minutes = parseInt(($("autoJitter") || {}).value, 10) || 0;
  plan.catch_up = ($("autoCatchUp") || {}).value || "same_day";
  plan.pause_when_user_busy = !!($("autoPauseBusy") || {}).checked;
  plan.tasks = plan.tasks || {};
  document.querySelectorAll("#autoTaskConfig .auto-task").forEach(function (el) {
    const t = el.dataset.type;
    const cfg = plan.tasks[t] || {};
    const en = el.querySelector("[data-role=enabled]");
    const cap = el.querySelector("[data-role=cap]");
    const gap = el.querySelector("[data-role=gap]");
    const mode = el.querySelector("[data-role=mode]");
    const dryrun = el.querySelector("[data-role=dryrun]");
    if (en) cfg.enabled = en.checked;
    // 空/非法一律**保留原值**：边打边存时用户可能正处在「清空重打」的中间态，
    // 用 `|| 0` 会把配额瞬间写成 0（任务直接不排班），也是「改几遍都变回去」的帮凶
    if (cap) {
      const v = parseInt(cap.value, 10);
      cfg.daily_cap = isNaN(v) ? (parseInt(cfg.daily_cap, 10) || 0) : v;
    }
    if (gap) {
      const v = parseInt(gap.value, 10);
      cfg.min_gap_minutes = isNaN(v) ? (parseInt(cfg.min_gap_minutes, 10) || 60) : v;
    }
    if (mode) { cfg.params = cfg.params || {}; cfg.params.mode = mode.value; }
    if (dryrun) { cfg.params = cfg.params || {}; cfg.params.dry_run = dryrun.value === "1"; }
    plan.tasks[t] = cfg;
  });
  return plan;
}

function queueAutoSave() {
  autoDirty = true;              // 未保存期间禁止重建表单
  if (autoSaveTimer) clearTimeout(autoSaveTimer);
  autoSaveTimer = setTimeout(saveAutoPlan, 500);
}

async function saveAutoPlan() {
  try {
    const r = await fetch("/api/automation/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan: collectAutoPlan() }),
    });
    const d = await r.json();
    if (d && d.status) {
      autoData = d.status;
      autoDirty = false;           // 服务端已确认 → 解除保护
      autoSaveRetry = 0;
      renderAutoState(); renderAutoProgress(); renderAutoTimeline();
      renderAutoSingleAxis(); renderAutoSingleCards();
      setTimeout(flushAutoRender, 0);
    }
  } catch (e) {
    // 保存失败：保留 dirty（不把用户刚输入的值冲掉），有界重试
    if (autoSaveRetry < 3) {
      autoSaveRetry += 1;
      setTimeout(function () { if (autoDirty) queueAutoSave(); }, 2000);
    }
  }
}

async function autoPost(path, body) {
  try {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const d = await r.json();
    if (d && d.status) autoData = d.status;
    renderAutoState(); renderAutoProgress(); renderAutoTimeline();
    renderAutoSingleAxis(); renderAutoSingleCards(); renderAutoTaskConfig();
  } catch (e) { /* 忽略 */ }
}

function initAutomationPanel() {
  if (!$("autoStartBtn")) return;
  $("autoStartBtn").addEventListener("click", function () { autoPost("/api/automation/start"); });
  $("autoStopBtn").addEventListener("click", function () { autoPost("/api/automation/stop"); });
  $("autoPauseBtn").addEventListener("click", function () {
    const paused = (autoData || {}).paused;
    autoPost(paused ? "/api/automation/resume" : "/api/automation/pause");
  });
  $("autoRunNowBtn").addEventListener("click", function () { autoPost("/api/automation/run-now", {}); });
  // 演练：走完「找草稿 → 开编辑页 → 确认发布按钮」，不点发布（不可逆动作先验证）
  $("autoDryRunBtn").addEventListener("click", function () {
    autoPost("/api/automation/run-now", { type: "publish_drafts", dry_run: true });
  });
  $("autoRefreshBtn").addEventListener("click", loadAutomation);
  AUTO_PLAN_INPUT_IDS.forEach(function (id) {
    const el = $(id);
    if (!el) return;
    el.addEventListener("input", queueAutoSave);
    el.addEventListener("change", queueAutoSave);
    el.addEventListener("blur", function () { setTimeout(flushAutoRender, 700); });
  });
}

/* ---- 接入左侧模式切换：注册「自动化」模式 + 进出时启停轮询 ---- */
(function registerAutomationMode() {
  if (typeof LEFT_MODES !== "undefined") {
    LEFT_MODES.push({
      id: "automation",
      name: "自动化",
      desc: "24 小时时间轴：到点自动发布草稿 / 全链路撰写（无人化，可随时暂停）",
    });
  }
  const baseApply = applyLeftMode;
  applyLeftMode = function (id) {
    baseApply(id);
    document.body.classList.toggle("auto-mode", id === "automation");
    if (id === "automation") startAutoPoll(); else stopAutoPoll();
  };
  initAutomationPanel();
  if (typeof renderLeftMode === "function") {
    renderLeftMode();
    applyLeftMode(currentLeftMode);
  }
})();
