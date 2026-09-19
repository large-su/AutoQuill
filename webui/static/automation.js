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

const AUTO_STATUS_TEXT = {
  planned: "待执行", running: "执行中", done: "已完成",
  failed: "失败", skipped: "已跳过", needs_human: "需要人工",
};
const AUTO_STATUS_COLOR = {
  planned: "#818cf8", running: "#a78bfa", done: "#34d399",
  failed: "#f87171", skipped: "#3b4762", needs_human: "#fbbf24",
};

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
    html += "<div class=\"auto-pcard\">"
      + "<div class=\"l\">" + esc(m.label) + "</div>"
      + "<div class=\"v\">" + m.done + "<span class=\"sub\"> / " + m.cap
      + " " + esc(m.unit) + "</span></div>"
      + "<div class=\"d\">窗口 " + esc((m.window || {}).start || "-") + "–"
      + esc((m.window || {}).end || "-") + " · 间隔 ≥ " + m.min_gap_minutes + " 分钟</div>"
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
  const order = Object.keys(per).sort(function (a, b) { return per[a].lane - per[b].lane; });
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
    el.addEventListener("change", queueAutoSave);
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
    if (en) cfg.enabled = en.checked;
    if (cap) cfg.daily_cap = parseInt(cap.value, 10) || 0;
    if (gap) cfg.min_gap_minutes = parseInt(gap.value, 10) || 60;
    if (mode) { cfg.params = cfg.params || {}; cfg.params.mode = mode.value; }
    plan.tasks[t] = cfg;
  });
  return plan;
}

function queueAutoSave() {
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
      renderAutoState(); renderAutoProgress(); renderAutoTimeline();
    }
  } catch (e) { /* 忽略：下次轮询会刷新 */ }
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
    renderAutoState(); renderAutoProgress(); renderAutoTimeline(); renderAutoTaskConfig();
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
  ["autoWinStart", "autoWinEnd", "autoMinGap", "autoGapRatio", "autoJitter",
   "autoCatchUp", "autoPauseBusy"].forEach(function (id) {
    const el = $(id);
    if (el) el.addEventListener("change", queueAutoSave);
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
