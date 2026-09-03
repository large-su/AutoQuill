
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const STATE_TEXT = {
  idle: "空闲", running: "运行中", stopping: "停止中",
  done: "完成", error: "出错", stopped: "已停止", timeout: "超时",
};

const CFG_LABELS = {
  LLM_MODE: "LLM 模式", LLM_PROVIDER: "提供商", LLM_MODEL: "模型",
  QUESTION_SELECT_MODE: "选题模式",
  QUESTION_SOURCE: "选题来源",
  STORY_MATERIAL_MODE: "素材模式", AUTHOR_PROFILE: "作者风格",
  ENABLE_FORMAT_RETRY: "格式重试", MIN_ANSWER_LENGTH: "最短回答",
  MAX_TOPIC_RETRY: "选题重试",
  ENABLE_STORY_FILTER: "故事过滤", ENABLE_MATERIAL_LIKES_GATE: "点赞门槛",
  MATERIAL_MIN_LIKES: "最低点赞",
  KB_ENABLE: "知识库",
};

let es = null;
let currentMode = "extract";
let genChars = 0;
let formatScore = null;
let genMode = null;
let browserHeadless = false;
let taskState = "idle";

/* ---------- 状态 ---------- */

function setState(state, message, guide) {
  taskState = state;
  const pill = $("statusPill");
  pill.className = "status-pill " + (state === "running" ? "running" : state);
  $("statusText").textContent = STATE_TEXT[state] || state;
  if (message && state !== "running") $("statusText").textContent = STATE_TEXT[state] || state;
  $("btnRun").disabled = (state === "running" || state === "stopping");
  $("btnStop").disabled = !(state === "running" || state === "stopping");
  if (state !== "running") {
    stopTaskTimer();
    $("progressWrap").classList.remove("show");
  }
  // 运行前检测失败（当前通道缺必要信息）：直接弹引导，不只看报错
  if (state === "error" && guide) showSetupGuide(guide);
}

async function showSetupGuide(need) {
  manualGuideOpen = true;
  await loadSetupStatus();
  // 按 need 对应的具体配置判断就绪，而非整体 allDone：用户可能已配好
  // API Key 但切到 Web 未登录（整体配置「已完成」），此时仍须弹引导
  const st = setupCache || {};
  const ready = need === "api_key" ? !!st.llm_configured
                                   : !!st.web_llm_logged_in;
  if (ready) {
    manualGuideOpen = false;
    hideSetupWizard();  // loadSetupStatus 内部可能已显示引导窗，关掉
    return;
  }
  // loadSetupStatus 可能已因整体配置完成而自动收尾关闭窗口——重新打开
  manualGuideOpen = true;
  $("setupMask").classList.add("show");
  renderModeCardState(setupCache);
  selectModeCard(setupCache);
  showStStatus(need === "api_key"
    ? "API 模式需要先配置 API Key（见引导窗口）"
    : "Web 模式需要先登录 DeepSeek 网页版（见引导窗口）", "err");
}

/* ---------- 任务阶段进度（文风提炼等） ---------- */

function applyTaskProgress(p) {
  // SSE 顺序：state(done) 事件先于最后一条进度行到达，这里用本地状态
  // 拦截过期进度，避免任务结束后进度条又被弹出来
  if (taskState !== "running") return;
  $("progressWrap").classList.add("show");
  if (typeof p.pct === "number") {
    stopTaskTimer();
    $("progressFill").classList.remove("indeterminate");
    $("progressFill").style.width = Math.min(100, p.pct) + "%";
    $("progressText").textContent = p.text || "";
  } else {
    $("progressFill").classList.add("indeterminate");
    $("progressFill").style.width = "100%";
    startTaskTimer(p.text || "处理中…");
  }
}

async function pollTaskProgress() {
  // SSE 断连时（任务由命令行/其他页面触发）轮询 /api/status 兜底：
  // 以服务端状态为准同步本地状态，并应用当前阶段进度
  try {
    const r = await fetch("/api/status");
    const st = await r.json();
    if (st.state !== taskState) {
      setState(st.state, st.message, st.guide_needed);
      if (st.state === "running") {
        if (!es) connectSSE();
      } else {
        refreshStatus();
        loadStories();
        loadAuthors();
        loadProfileSources();
      }
    }
    if (st.state === "running" && st.progress) applyTaskProgress(st.progress);
  } catch (e) { /* ignore */ }
}

setInterval(pollTaskProgress, 3000);

/* ---------- 日志 ---------- */

async function loadLogHistory() {
  if (window.__logCleared) return;  // 用户已手动清空：不回放历史，只显示清空后的新日志
  try {
    const r = await fetch("/api/logs/latest?lines=150");
    const d = await r.json();
    if (d.path) $("logFile").textContent = "日志文件：" + d.path.split(/[\\/]/).pop();
    (d.lines || []).forEach((ln) => {
      if (ln.trim()) {
        // 回放也按严重级高亮，方便快速定位历史错误
        const cls = /\[ERROR\]/.test(ln) ? "error"
          : /\[WARNING\]|失败|异常/.test(ln) ? "warn" : "muted";
        addLog(ln, cls);
      }
    });
  } catch (e) { /* ignore */ }
}

function addLog(text, cls) {
  const box = $("logBox");
  const ph = box.querySelector(".placeholder");
  if (ph) ph.remove();
  const line = document.createElement("div");
  line.className = "log-line " + (cls || "");
  line.textContent = text;
  box.appendChild(line);
  if ($("autoScroll").checked) box.scrollTop = box.scrollHeight;
}

function clearLog() {
  const box = $("logBox");
  box.innerHTML = '<div class="placeholder">日志已清空。点击「运行」开始，日志将实时显示在这里。</div>';
  window.__logCleared = true;  // 本次会话内不再回放历史日志，新日志从头开始
}

function copyLog() {
  const box = $("logBox");
  const text = [...box.querySelectorAll(".log-line")].map((l) => l.textContent).join("\n");
  if (!text) { showStStatus("日志为空", "err"); return; }
  const lines = text.split("\n").length;
  const ok = () => showStStatus(`日志已复制（${lines} 行）`, "ok");
  const fail = () => showStStatus("复制失败：剪贴板不可用", "err");
  // 主路径：Clipboard API（要求页面焦点）
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(ok, () => legacyCopy(text) ? ok() : fail());
  } else {
    legacyCopy(text) ? ok() : fail();
  }
}

function legacyCopy(text) {
  // 后备：execCommand 复制（无焦点/旧环境仍可用）
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const done = document.execCommand("copy");
    ta.remove();
    return done;
  } catch (e) { return false; }
}

$("btnLogClear").addEventListener("click", clearLog);
$("btnLogCopy").addEventListener("click", copyLog);

function handleLogEvent(ev, cls) {
  const payload = JSON.parse(ev.data);
  addLog(payload.raw || payload.text, cls);
}

/* ---------- 结果渲染 ---------- */

function renderContext(ctx) {
  if (!ctx) return;
  if (ctx.title) {
    $("extractCard").classList.add("show");
    $("extractTitle").textContent = ctx.title;
    $("extractUrl").textContent = ctx.url || "";
    const chips = [];
    if (ctx.answer) chips.push(`回答 ${ctx.answer.length} 字`);
    const f = ctx.footer || {};
    if (f.likes) chips.push(`赞 ${f.likes}`);
    if (f.comments) chips.push(`评 ${f.comments}`);
    if (f.favorites) chips.push(`藏 ${f.favorites}`);
    if (f.thanks) chips.push(`喜 ${f.thanks}`);
    if (f.time) chips.push(`发表 ${f.time}`);
    $("extractChips").innerHTML = chips
      .map((c) => `<span class="chip acc">${esc(c)}</span>`).join("");
    $("answerBody").textContent = ctx.answer || "(无内容)";
  }
  if (ctx.sample_preview) {
    $("samplePreview").textContent = ctx.sample_preview;
    $("sampleDetail").style.display = "";
  } else {
    $("sampleDetail").style.display = "none";
  }
  if (ctx.collect) {
    $("collectCard").classList.add("show");
    $("collectTitle").textContent = ctx.collect.title || "故事采集";
    $("collectSummary").textContent = ctx.collect.summary || "";
  }
  if (ctx.profile) {
    $("profileCard").classList.add("show");
    $("profileTitle").textContent = ctx.profile.title || "文风签名";
    $("profilePath").textContent = ctx.profile.path || "";
    $("profileBody").textContent = ctx.profile.summary || "(无摘要)";
  }
}

function renderStory(story, title) {
  if (!story || !story.text) return;
  $("storyCard").classList.add("show");
  $("storyTitle").textContent = title || "已生成";
  const meta = [{ t: `${story.chars} 字`, c: "acc" }];
  if (story.md_path) meta.push({ t: story.md_path, c: "acc" });
  if (formatScore !== null) meta.push({ t: `格式检测 ${formatScore}/10`, c: "acc" });
  if (story.ai_flavor !== undefined && story.ai_flavor !== null) {
    const f = Number(story.ai_flavor) || 0;
    const c = f >= 40 ? "danger" : f >= 20 ? "warn" : "ok";
    meta.push({ t: `AI味 ${f}/100`, c: c });
  }
  if (story.audit) {
    const a = story.audit;
    const ok = !!a.passed;
    meta.push({ t: `原创审核：${ok ? "通过" : "未过"} ${a.verdict || ""}`, c: ok ? "ok" : "danger" });
  }
  $("storyMeta").innerHTML = meta.map((m) => `<span class="chip ${m.c}">${esc(m.t)}</span>`).join("");
  $("storyBody").textContent = story.text;
}

/* ---------- 历史故事 ---------- */

const STORY_MAX_VISIBLE = 10;

async function loadStories() {
  try {
    const r = await fetch("/api/stories");
    const data = await r.json();
    const list = $("storyList");
    if (!data.stories.length) {
      list.innerHTML = '<div class="empty-note">暂无故事（运行后自动生成）</div>';
      return;
    }
    list.innerHTML = "";
    const all = data.stories;
    renderStoryItems(all.slice(0, STORY_MAX_VISIBLE));
    if (all.length > STORY_MAX_VISIBLE) {
      const btn = document.createElement("button");
      btn.className = "story-more";
      btn.textContent = `展开全部（共 ${all.length} 条）`;
      btn.onclick = () => {
        btn.remove();
        renderStoryItems(all);
      };
      list.appendChild(btn);
    }
  } catch (e) {
    $("storyList").innerHTML = '<div class="empty-note">加载失败</div>';
  }
}

function renderStoryItems(stories) {
  const list = $("storyList");
  stories.forEach((s) => {
    const item = document.createElement("div");
    item.className = "story-item";
    const d = new Date(s.mtime * 1000);
    const time = `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    item.innerHTML = `<span class="n">${esc(s.name)}</span><span class="s">${(s.size / 1024).toFixed(1)}k · ${time}</span>`;
    item.onclick = () => openStory(s.name);
    list.appendChild(item);
  });
}

async function openStory(name) {
  try {
    const r = await fetch("/api/story?name=" + encodeURIComponent(name));
    const data = await r.json();
    $("modalName").textContent = data.name;
    $("modalBody").textContent = data.text;
    $("modalMask").classList.add("show");
  } catch (e) { /* ignore */ }
}

/* ---------- 配置速览 ---------- */

/* 选题参数：前端可修改（POST /api/config，持久化到 webui_model.json），在设置弹窗中编辑 */
const TUNABLE_KEYS = new Set(["MAX_TOPIC_RETRY", "MIN_ANSWER_LENGTH", "MATERIAL_MIN_LIKES"]);
const TUNABLE_HINTS = {
  MAX_TOPIC_RETRY: "首答不合格时最多重试次数（默认 5）",
  MIN_ANSWER_LENGTH: "首答最短字数，低于则重试（默认 500）",
  MATERIAL_MIN_LIKES: "素材最低点赞门槛（默认 200；纯净模式同样生效）",
};

/* 设置操作状态条（顶栏下方）：设置项修改即自动提交，成败在此反馈 */
let _stStatusTimer = null;
function showStStatus(text, kind) {
  const el = $("stStatus");
  el.textContent = text;
  el.className = "st-status show " + (kind || "ok");
  if (_stStatusTimer) clearTimeout(_stStatusTimer);
  _stStatusTimer = setTimeout(() => el.classList.remove("show"), 4000);
}

function initTunables() {
  const box = $("tunableBox");
  box.innerHTML = "";
  TUNABLE_KEYS.forEach((k) => {
    const row = document.createElement("div");
    row.className = "tun-row";
    row.innerHTML =
      `<span class="tun-k">${esc(CFG_LABELS[k] || k)}</span>` +
      `<input type="number" id="tun_${k}" min="0" title="修改后自动保存，下次启动保留">` +
      `<span class="tun-hint">${esc(TUNABLE_HINTS[k] || "")}</span>`;
    const input = row.querySelector("input");
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { saveTunable(k, input, null); input.blur(); }
    });
    input.addEventListener("blur", () => saveTunable(k, input, null));
    box.appendChild(row);
  });
}

async function saveTunable(key, input) {
  const val = parseInt(input.value, 10);
  if (!Number.isInteger(val)) { showStStatus("请输入整数", "err"); return; }
  const prev = input.value;
  input.disabled = true;
  try {
    const r = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, value: val }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || "保存失败");
    const d = await r.json();
    addLog(`选题参数 ${CFG_LABELS[key] || key} → ${d.value}`, "result");
    showStStatus(`${CFG_LABELS[key] || key} → ${d.value}`, "ok");
    loadConfig();
  } catch (e) {
    input.value = prev;
    showStStatus("保存失败：" + e.message, "err");
  } finally {
    input.disabled = false;
  }
}

async function loadConfig() {
  try {
    const r = await fetch("/api/config");
    const cfg = await r.json();
    const grid = $("cfgGrid");
    grid.innerHTML = "";
    Object.entries(CFG_LABELS).forEach(([k, label]) => {
      if (!(k in cfg) || TUNABLE_KEYS.has(k)) return;  // 可调参数在设置弹窗中编辑
      let v = cfg[k];
      const cls = (typeof v === "boolean") ? (v ? "on" : "off") : "";
      let txt = (typeof v === "boolean") ? (v ? "开" : "关") : String(v);
      if (k === "QUESTION_SOURCE") txt = ({recommend: "推荐话题", invited: "邀请回答", custom: "自选问题"})[v] || txt;
      const cell = document.createElement("div");
      cell.className = "cfg-cell";
      cell.innerHTML = `<div class="k">${esc(label)}</div><div class="v ${cls}">${esc(txt)}</div>`;
      grid.appendChild(cell);
    });
    // 同步设置弹窗中的可调参数当前值
    TUNABLE_KEYS.forEach((k) => {
      if (!(k in cfg)) return;
      const input = $("tun_" + k);
      if (input) input.value = cfg[k];
    });
  } catch (e) {
    $("cfgGrid").innerHTML = '<div class="empty-note">配置加载失败</div>';
  }
}

/* ---------- 模型切换 ---------- */

const providerSel = $("providerSel");
const modelSel = $("modelSel");
let modelProviders = [];
let modelCurrent = null;
let modelDirty = false;

async function loadModels() {
  try {
    const r = await fetch("/api/models");
    const data = await r.json();
    modelProviders = data.providers;
    modelCurrent = data.current;
    if (!modelProviders.length) return;
    $("modelRow").hidden = false;
    providerSel.innerHTML = "";  // 重复加载（如保存 key 后）时先清空再填充
    modelProviders.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.name;
      opt.textContent = p.name;
      providerSel.appendChild(opt);
    });
    providerSel.value = modelCurrent.provider;
    renderModelOptions();
    modelSel.value = modelCurrent.model_id;
  } catch (e) { /* 配置不可用时不显示选择器 */ }
}

function renderModelOptions() {
  const p = modelProviders.find((x) => x.name === providerSel.value);
  modelSel.innerHTML = "";
  (p ? p.models : []).forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m.id;
    opt.textContent = m.id;
    modelSel.appendChild(opt);
  });
  modelDirty = true;
}

/* 模型切换：修改即自动保存（切 provider 时自动选其默认模型一并提交） */
let _modelSubmitTimer = null;
async function applyModel() {
  const r = await fetch("/api/model", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider: providerSel.value, model_id: modelSel.value }),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || r.status);
  modelCurrent = data.effective;
  loadConfig();
  addLog(`模型已切换 → ${data.effective.provider} / ${data.effective.api_model}`, "result");
  showStStatus(`模型已切换 → ${data.effective.api_model}`, "ok");
}

function queueModelSubmit() {
  if (_modelSubmitTimer) clearTimeout(_modelSubmitTimer);
  _modelSubmitTimer = setTimeout(() => {
    _modelSubmitTimer = null;
    const cur = modelCurrent;
    const same = cur && cur.provider === providerSel.value && cur.model_id === modelSel.value;
    if (same) return;  // 选择未实际变化（如重载后重建选项）不提交
    applyModel().catch((e) => {
      showStStatus("模型切换失败：" + e.message, "err");
    });
  }, 300);
}

providerSel.addEventListener("change", () => {
  renderModelOptions();
  queueModelSubmit();
});
modelSel.addEventListener("change", () => { queueModelSubmit(); });

let _modeSubmitBusy = false;
async function applyMode() {
  if (_modeSubmitBusy) return;
  const sel = document.querySelector('input[name="genMode"]:checked');
  if (!sel || sel.value === genMode) return;
  const target = sel.value;
  _modeSubmitBusy = true;
  try {
    const r = await fetch("/api/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: target }),
    });
    if (!r.ok) {
      // 还原选择（服务端拒绝，等配置完成后重新切换）
      document.querySelectorAll('input[name="genMode"]').forEach((el) => {
        el.checked = (el.value === genMode);
      });
      // 服务端闭环预检拒绝：detail 为 {detail, needs} → 弹对应引导
      const body = await r.json().catch(() => null);
      const d = body && body.detail;
      if (d && typeof d === "object" && d.needs) {
        showSetupGuide(d.needs);
        return;
      }
      throw new Error(typeof d === "string" ? d : "切换失败");
    }
    const data = await r.json();
    genMode = data.effective.mode;
    loadConfig();
    hideSetupWizard();
    addLog(`生成通道已切换 → ${genMode === "web" ? "Web 网页版" : "API"}`, "result");
    showStStatus(`生成通道 → ${genMode === "web" ? "Web 网页版" : "API"}`, "ok");
  } catch (e) {
    // 还原选择并提示
    document.querySelectorAll('input[name="genMode"]').forEach((el) => {
      el.checked = (el.value === genMode);
    });
    showStStatus("切换失败：" + e.message, "err");
  } finally {
    _modeSubmitBusy = false;
  }
}

/* 生成通道：单选 change 即自动切换 */
document.querySelectorAll('input[name="genMode"]').forEach((el) => {
  el.addEventListener("change", applyMode);
});

async function loadMode() {
  try {
    const r = await fetch("/api/mode");
    const data = await r.json();
    genMode = data.mode;
    document.querySelectorAll('input[name="genMode"]').forEach((el) => {
      el.checked = (el.value === genMode);
    });
  } catch (e) { /* 旧服务无 /api/mode，保持默认 */ }
}

/* 生成通道状态（setup/status 查询结果缓存，首次切换引导用） */
let setupCache = null;
/* 引导窗是否为按需弹出（模式切换/运行检测失败触发）。
   为 true 时轮询不得自动关闭窗口——首启引导完成后可自动收尾，
   按需引导要等用户点「进入控制台」或主动操作完成。 */
let manualGuideOpen = false;
async function fetchSetupStatus() {
  try {
    const r = await fetch("/api/setup/status");
    setupCache = await r.json();
  } catch (e) { setupCache = null; }
}


let _browserSubmitBusy = false;
async function applyBrowserMode() {
  if (_browserSubmitBusy) return;
  const sel = document.querySelector('input[name="browserMode"]:checked');
  if (!sel || sel.value === String(browserHeadless)) return;
  _browserSubmitBusy = true;
  try {
    const r = await fetch("/api/browser", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ headless: sel.value === "true" }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || "切换失败");
    const data = await r.json();
    browserHeadless = data.effective.headless;
    addLog(`浏览器模式已切换 → ${browserHeadless ? "无头（工作，下次任务生效）" : "前台（调试）"}`, "result");
    showStStatus(`浏览器模式 → ${browserHeadless ? "无头" : "前台"}`, "ok");
  } catch (e) {
    document.querySelectorAll('input[name="browserMode"]').forEach((el) => {
      el.checked = (el.value === String(browserHeadless));
    });
    showStStatus("切换失败：" + e.message, "err");
  } finally {
    _browserSubmitBusy = false;
  }
}

document.querySelectorAll('input[name="browserMode"]').forEach((el) => {
  el.addEventListener("change", applyBrowserMode);
});

async function loadBrowserMode() {
  try {
    const r = await fetch("/api/browser");
    const data = await r.json();
    browserHeadless = data.headless;
    document.querySelectorAll('input[name="browserMode"]').forEach((el) => {
      el.checked = (el.value === String(browserHeadless));
    });
  } catch (e) { /* 旧服务无 /api/browser，保持默认 */ }
}

/* ---------- 网页模式预设 ---------- */

let webPreset = null;

async function loadWebPreset() {
  try {
    const r = await fetch("/api/config");
    const cfg = await r.json();
    if (!cfg.WEB_PRESET) return;
    const wp = cfg.WEB_PRESET;
    $("webPresetRow").hidden = false;
    webPreset = wp.preset;
    $("webPresetSel").value = wp.preset;
  } catch (e) { /* 旧服务无此配置 */ }
}

async function applyWebPreset() {
  const val = $("webPresetSel").value;
  if (val === webPreset) return;
  try {
    const r = await fetch("/api/web-preset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preset: val }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || "切换失败");
    const data = await r.json();
    webPreset = data.effective.preset;
    loadConfig();
    addLog(`网页模式预设已切换 → ${webPreset === "expert" ? "专家 + 深度思考" : "快速 + 深度思考 + 智能搜索"}`, "result");
    showStStatus(`网页预设 → ${webPreset === "expert" ? "专家" : "快速"}`, "ok");
  } catch (e) {
    $("webPresetSel").value = webPreset;
    showStStatus("切换失败：" + e.message, "err");
  }
}

$("webPresetSel").addEventListener("change", applyWebPreset);


/* ---------- 选题来源 ---------- */

let questionSource = "recommend";

const QUESTION_SOURCE_LABELS = {recommend: "推荐话题", invited: "邀请回答", custom: "自选问题"};

async function loadQuestionSource() {
  try {
    const r = await fetch("/api/question-source");
    const data = await r.json();
    questionSource = data.source;
    $("questionSourceSel").value = data.source;
    $("customUrlInput").value = data.custom_url || "";
    $("customUrlRow").hidden = (data.source !== "custom");
  } catch (e) { /* 旧服务无此端点 */ }
}

async function applyQuestionSource() {
  const val = $("questionSourceSel").value;
  if (val === questionSource) return;
  try {
    const r = await fetch("/api/question-source", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: val, custom_url: $("customUrlInput").value.trim() }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || "切换失败");
    const data = await r.json();
    questionSource = data.effective.question_source;
    $("customUrlRow").hidden = (questionSource !== "custom");
    loadConfig();
    addLog(`选题来源已切换 → ${QUESTION_SOURCE_LABELS[questionSource]}`, "result");
    showStStatus(`选题来源 → ${QUESTION_SOURCE_LABELS[questionSource]}`, "ok");
  } catch (e) {
    $("questionSourceSel").value = questionSource;
    showStStatus("切换失败：" + e.message, "err");
  }
}

async function saveCustomUrl() {
  const url = $("customUrlInput").value.trim();
  try {
    const r = await fetch("/api/question-source", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: "custom", custom_url: url }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || "保存失败");
    const data = await r.json();
    $("customUrlInput").value = data.effective.custom_question_url || "";
    showStStatus("自选问题链接已保存", "ok");
  } catch (e) {
    showStStatus("保存失败：" + e.message, "err");
  }
}

$("questionSourceSel").addEventListener("change", applyQuestionSource);
$("customUrlInput").addEventListener("change", saveCustomUrl);


/* ---------- 作者文风 ---------- */

const authorSel = $("authorSel");

async function loadAuthors() {
  try {
    const r = await fetch("/api/authors");
    const data = await r.json();
    authorSel.innerHTML = '<option value="">（不注入文风）</option>';
    (data.authors || []).forEach((a) => {
      const opt = document.createElement("option");
      opt.value = a.name;
      opt.textContent = `${a.general ? "通用" : a.name}（${a.stories_count} 篇样本）`;
      authorSel.appendChild(opt);
    });
    const cur = data.current || "";
    authorSel.value = (cur === "通用") ? "通用" : cur;
    authorSel.dataset.current = authorSel.value;
    authorSel.disabled = false;
    // 提炼完成后自动把新文风加进下拉
    window._authorsCache = data.authors || [];
  } catch (e) { /* 旧服务无 /api/authors */ }
}

async function applyAuthorProfile() {
  const name = authorSel.value;
  const prev = authorSel.dataset.current || "";
  if (name === prev) return;
  try {
    const r = await fetch("/api/author", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || "切换失败");
    const data = await r.json();
    authorSel.dataset.current = data.effective.author_profile || "";
    addLog(`作者文风已切换 → ${data.effective.author_profile || "（不注入）"}`, "result");
    showStStatus(`作者文风 → ${data.effective.author_profile || "不注入"}`, "ok");
    loadConfig();
  } catch (e) {
    authorSel.value = prev;
    showStStatus("文风切换失败：" + e.message, "err");
  }
}

authorSel.addEventListener("change", applyAuthorProfile);

/* ---------- 任务进度计时器（剖析中无百分比 → 显示已等待秒数） ---------- */

let taskTimer = null;
let taskTimerBase = 0;
let taskTimerText = "";

function startTaskTimer(text) {
  if (taskTimer && taskTimerText === text) return;  // 重复轮询不重置计时
  stopTaskTimer();
  taskTimerText = text;
  taskTimerBase = Date.now();
  const tick = () => {
    const sec = Math.floor((Date.now() - taskTimerBase) / 1000);
    const mm = String(Math.floor(sec / 60)).padStart(2, "0");
    const ss = String(sec % 60).padStart(2, "0");
    $("progressText").textContent = `${text} 已等待 ${mm}:${ss}（通常 1-3 分钟，可随时点停止）`;
  };
  tick();
  taskTimer = setInterval(tick, 1000);
}

function stopTaskTimer() {
  if (taskTimer) { clearInterval(taskTimer); taskTimer = null; }
  taskTimerText = "";
}

/* ---------- 左列模式切换（扩展点：新增模式 = 加一项 + 对应 pane 容器） ---------- */

const LEFT_MODES = [
  { id: "workspace", name: "工作台",
    desc: "故事生成主流程：选题、提取、生成、批量运行" },
  { id: "distill", name: "作者蒸馏",
    desc: "扩展模块：采集作者样本 → 提炼文风签名" },
  { id: "dashboard", name: "已发布内容看板",
    desc: "管理已发布内容：查看、刷新、筛选、搜索" },
  { id: "drafts", name: "草稿箱素材",
    desc: "预览/筛选/批量删除知乎草稿（不含发布）" },
];
let currentLeftMode = "workspace";

function renderLeftMode() {
  const sel = $("leftModeSel");
  sel.innerHTML = "";
  LEFT_MODES.forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m.id;
    opt.textContent = m.name;
    sel.appendChild(opt);
  });
  sel.value = currentLeftMode;
  applyLeftMode(currentLeftMode);
}

function applyLeftMode(id) {
  currentLeftMode = id;
  const mode = LEFT_MODES.find((m) => m.id === id);
  $("leftModeDesc").textContent = mode ? mode.desc : "";
  document.querySelectorAll(".mode-pane").forEach((p) => {
    p.hidden = p.id !== "pane-" + id;
  });
  document.body.classList.toggle("dash-mode", id === "dashboard");
  document.body.classList.toggle("drafts-mode", id === "drafts");
  if (id === "dashboard") loadDashboard();
  if (id === "drafts") loadDrafts();
}

/* ---------- 已发布内容看板 ---------- */

const trimZero = (s) => {
  if (s.indexOf(".") < 0) return s;
  while (s.endsWith("0")) s = s.slice(0, -1);
  return s.endsWith(".") ? s.slice(0, -1) : s;
};

const fmtNum = (v) => {
  const n = Number(v || 0);
  if (n >= 10000) return trimZero((n / 10000).toFixed(n >= 100000 ? 0 : 1)) + "万";
  if (n >= 1000) return trimZero((n / 1000).toFixed(1)) + "千";
  return String(n);
};

const SORT_META = {
  newest:   { label: "最新发布", dir: "desc" },
  oldest:   { label: "最早发布", dir: "asc" },
  likes:    { label: "赞同最多", dir: "desc" },
  reads:    { label: "阅读最多", dir: "desc" },
  comments: { label: "评论最多", dir: "desc" },
  collects: { label: "收藏最多", dir: "desc" },
  favors:   { label: "喜欢最多", dir: "desc" },
};

let dashState = {
  q: "", start: "", end: "",
  minLikes: 0, minReads: 0, minComments: 0,
  sort: "newest", direction: "desc",
  page: 1, pageSize: 50,
};
let dashData = { rows: [], total: 0, all_total: 0, stats: {}, generated_at: "", source_file: "" };
let dashTab = "trend";
let dashDebounce = null;
let dashPollTimer = null;
let dashJustRefreshed = false;

const BS = String.fromCharCode(92);
function baseName(p) {
  const s = p || "";
  const i = Math.max(s.lastIndexOf(BS), s.lastIndexOf("/"));
  return i >= 0 ? s.slice(i + 1) : (s || "-");
}

function dashParams() {
  const p = new URLSearchParams();
  p.set("q", dashState.q.trim());
  p.set("start", dashState.start);
  p.set("end", dashState.end);
  p.set("min_likes", dashState.minLikes || 0);
  p.set("min_reads", dashState.minReads || 0);
  p.set("min_comments", dashState.minComments || 0);
  p.set("sort", dashState.sort);
  p.set("direction", dashState.direction);
  return p.toString();
}

function _showTaskStatus(elId, html, cls, autoHideMs) {
  const el = $(elId);
  el.className = "dash-status show" + (cls ? " " + cls : "");
  el.innerHTML = html;
  if (autoHideMs) setTimeout(() => {
    if (el.classList.contains("show")) el.className = "dash-status";
  }, autoHideMs);
}
function showDashStatus(html, cls, autoHideMs) { _showTaskStatus("dashStatus", html, cls, autoHideMs); }
function hideDashStatus() { $("dashStatus").className = "dash-status"; }
function showDraftStatus(html, cls, autoHideMs) { _showTaskStatus("draftStatus", html, cls, autoHideMs); }
function hideDraftStatus() { const el = $("draftStatus"); if (el) el.className = "dash-status"; }

const dashHideTimer = { current: null };
const draftHideTimer = { current: null };

function refreshBarHTML(pct, text) {
  const fill = (pct === null || pct === undefined)
    ? '<i class="dp-fill indet"></i>'
    : '<i class="dp-fill" style="width:' + Math.min(100, Math.max(0, pct)) + '%"></i>';
  return '<span class="spin"></span>' + esc(text || "刷新中…") + '<span class="dp-bar">' + fill + "</span>";
}
function _setTaskBar(pct, text, keepMs, wrapId, fillId, txtId, timerRef) {
  const wrap = $(wrapId);
  const fill = $(fillId);
  const t = $(txtId);
  if (pct === null || pct === undefined) {
    fill.style.width = "";
    fill.className = "dp-fill indet";
  } else {
    fill.className = "dp-fill";
    fill.style.width = Math.min(100, Math.max(0, pct)) + "%";
  }
  t.textContent = text || "";
  wrap.hidden = false;
  clearTimeout(timerRef.current);
  if (keepMs) timerRef.current = setTimeout(() => { wrap.hidden = true; }, keepMs);
}
function setRefreshBar(pct, text, keepMs) { _setTaskBar(pct, text, keepMs, "dashRefreshHint", "dpFill", "dpText", dashHideTimer); }
function setDrfBar(pct, text, keepMs) { _setTaskBar(pct, text, keepMs, "draftRefreshHint", "drfFill", "drfText", draftHideTimer); }

async function loadDashboard(keepPage) {
  if (!keepPage) dashState.page = 1;
  try {
    const r = await fetch("/api/dashboard?" + dashParams());
    const d = await r.json();
    dashData = d;
    renderDashHeader(d);
    renderKpis(d.stats || {}, d.all_total || 0);
    renderFilterChips();
    renderCharts(d.rows || []);
    renderDashTable(d.rows || []);
    renderSrcInfo(d);
    const rs = d.refresh || {};
    if (dashJustRefreshed) {
      dashJustRefreshed = false;
      showDashStatus("刷新完成，共 " + (rs.count || 0) + " 条", "ok", 6000);
    } else if (rs.status === "running") {
      showDashStatus(refreshBarHTML(rs.pct, rs.progress), "refreshing");
      setRefreshBar(rs.pct, rs.progress);
    } else if (rs.status === "error") {
      showDashStatus("上次刷新失败：" + esc(rs.error), "err", 8000);
    } else {
      hideDashStatus();
    }
  } catch (e) {
    $("dashTable").innerHTML = '<div class="dash-empty"><span class="em">⚠️</span>加载失败：' + esc(e.message) + "</div>";
  }
}

function renderDashHeader(d) {
  $("dashCount").innerHTML = "显示 <b>" + (d.total || 0) + "</b> / 共 <b>" + (d.all_total || 0) + "</b> 条";
  const snap = d.generated_at ? "快照 " + d.generated_at.slice(0, 10) : "快照 · 暂无";
  $("dashSnap").textContent = snap;
  $("dashSnap").classList.toggle("ok", !!d.generated_at);
}

function renderSrcInfo(d) {
  const gen = d.generated_at ? d.generated_at.replace("T", " ").slice(0, 16) : "无快照";
  $("dashSrcLine").innerHTML = "快照文件：<b>" + esc(baseName(d.source_file)) + "</b><br>抓取时间：" + esc(gen);
}

function kpiCard(label, value, sub, barPct) {
  let bar = "";
  if (barPct !== undefined) {
    const p = Math.min(100, Math.max(0, barPct));
    bar = '<div class="kpi-bar"><i style="width:' + p + '%"></i></div>';
  }
  return '<div class="kpi-card"><div class="kpi-label">' + esc(label) + '</div><div class="kpi-value">' + value + '</div><div class="kpi-sub">' + esc(sub) + '</div>' + bar + "</div>";
}

function renderKpis(st, allTotal) {
  const box = $("dashKpis");
  if (!st || !st.total) {
    box.innerHTML = kpiCard("当前结果", "0", "无匹配内容，调整筛选试试");
    return;
  }
  const dmin = (st.date_min || "").slice(0, 7);
  const dmax = (st.date_max || "").slice(0, 7);
  const span = dmin && dmax ? (dmin === dmax ? dmin : dmin + " ~ " + dmax) : "—";
  box.innerHTML =
    kpiCard("发布时段", span, "最早 ~ 最近发布") +
    kpiCard("赞同合计", fmtNum(st.sum_likes), "篇均 " + fmtNum(st.avg_likes)) +
    kpiCard("阅读合计", fmtNum(st.sum_reads), "篇均 " + fmtNum(st.avg_reads)) +
    kpiCard("有赞占比", st.liked_ratio + "%", "有赞 " + (st.liked || 0) + " / " + st.total + " 篇", st.liked_ratio);
}

function activeFilterItems() {
  const items = [];
  if (dashState.q.trim()) {
    items.push({ key: "q", label: "关键词：" + dashState.q.trim(),
      clean: () => { dashState.q = ""; $("dashQ").value = ""; } });
  }
  if (dashState.start || dashState.end) {
    items.push({ key: "date", label: "时间：" + (dashState.start || "…") + " ~ " + (dashState.end || "…"),
      clean: () => { dashState.start = ""; dashState.end = ""; $("dashStart").value = ""; $("dashEnd").value = ""; syncQuickSel(); } });
  }
  if (dashState.minLikes > 0) {
    items.push({ key: "minLikes", label: "赞同 ≥ " + dashState.minLikes,
      clean: () => { dashState.minLikes = 0; $("dashMinLikes").value = "0"; } });
  }
  if (dashState.minReads > 0) {
    items.push({ key: "minReads", label: "阅读 ≥ " + dashState.minReads,
      clean: () => { dashState.minReads = 0; $("dashMinReads").value = "0"; } });
  }
  if (dashState.minComments > 0) {
    items.push({ key: "minComments", label: "评论 ≥ " + dashState.minComments,
      clean: () => { dashState.minComments = 0; $("dashMinComments").value = "0"; } });
  }
  if (dashState.sort !== "newest") {
    const meta = SORT_META[dashState.sort] || {};
    items.push({ key: "sort", label: "排序：" + (meta.label || dashState.sort),
      clean: () => { dashState.sort = "newest"; dashState.direction = "desc"; $("dashSort").value = "newest"; } });
  }
  return items;
}

function renderFilterChips() {
  const bar = $("dashFilterbar");
  const items = activeFilterItems();
  if (!items.length) { bar.className = "dash-filterbar"; bar.innerHTML = ""; return; }
  bar.className = "dash-filterbar show";
  let html = "";
  for (const it of items) {
    html += '<span class="f-chip">' + esc(it.label) + '<button data-key="' + it.key + '" title="移除条件">×</button></span>';
  }
  html += '<button class="f-btn" id="dashClearAll">清除全部条件</button>';
  bar.innerHTML = html;
  bar.querySelectorAll(".f-chip button").forEach((b) => b.addEventListener("click", () => {
    const it = items.find((x) => x.key === b.dataset.key);
    if (it) { it.clean(); loadDashboard(); }
  }));
  $("dashClearAll").addEventListener("click", () => {
    dashState.q = ""; dashState.start = ""; dashState.end = "";
    dashState.minLikes = 0; dashState.minReads = 0; dashState.minComments = 0;
    dashState.sort = "newest"; dashState.direction = "desc";
    $("dashQ").value = ""; $("dashStart").value = ""; $("dashEnd").value = "";
    $("dashMinLikes").value = "0"; $("dashMinReads").value = "0"; $("dashMinComments").value = "0";
    $("dashSort").value = "newest";
    syncQuickSel();
    loadDashboard();
  });
}

function renderDashTable(rows) {
  const total = rows.length;
  const pageSize = dashState.pageSize;
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (dashState.page > pages) dashState.page = pages;
  const start = (dashState.page - 1) * pageSize;
  const end = Math.min(start + pageSize, total);
  const pageRows = rows.slice(start, end);

  $("dashTableInfo").innerHTML = total
    ? "第 <b>" + (start + 1) + "-" + end + "</b> 条 / 共 <b>" + total + "</b> 条"
    : "无匹配内容";
  $("dashPageNo").textContent = dashState.page + "/" + pages;
  $("dashPrev").disabled = dashState.page <= 1;
  $("dashNext").disabled = dashState.page >= pages;

  const wrap = $("dashTable");
  if (!pageRows.length) {
    wrap.innerHTML = '<div class="dash-empty"><span class="em">🔍</span>无匹配内容，试试放宽筛选条件</div>';
    return;
  }
  const maxBy = (k) => Math.max(1, ...rows.map((r) => r[k] || 0));
  const maxLikes = maxBy("likes"), maxReads = maxBy("reads");
  const maxComments = maxBy("comments"), maxCollects = maxBy("collects"), maxFavors = maxBy("favors");
  const medals = {};
  [...rows].sort((a, b) => b.likes - a.likes).slice(0, 3).forEach((r, i) => {
    medals[r.aid] = ["🥇", "🥈", "🥉"][i];
  });

  let html = '<table class="dash-table"><thead><tr>'
    + "<th>标题</th><th>发布时间</th>"
    + '<th class="t-num">阅读</th><th class="t-num">赞同</th>'
    + '<th class="t-num">评论</th><th class="t-num">收藏</th><th class="t-num">喜欢</th>'
    + '<th class="t-num" title="发布后日均赞同（互动分=赞+3×评+2.5×藏+2×喜欢）">日均赞</th>'
    + "</tr></thead><tbody>";
  for (const r of pageRows) {
    html += "<tr>"
      + '<td class="t-title"><div class="t-title-line">'
      + (medals[r.aid] ? '<span class="t-medal">' + medals[r.aid] + "</span>" : "")
      + '<span class="title-text"><a href="' + esc(r.url) + '" target="_blank" rel="noopener" title="' + esc(r.title) + '">' + esc(r.title || "(无标题)") + "</a></span>"
      + (r.genre ? '<span class="genre-chip">' + esc(r.genre) + "</span>" : "")
      + "</div></td>"
      + '<td class="t-date">' + esc(r.publish_date || "-") + "</td>"
      + metricCell(r.reads, maxReads)
      + metricCell(r.likes, maxLikes, true)
      + metricCell(r.comments, maxComments)
      + metricCell(r.collects, maxCollects)
      + metricCell(r.favors, maxFavors)
      + '<td class="t-num"><span class="v">'
      + (r.likes_per_day == null ? "0.00" : r.likes_per_day.toFixed(2))
      + '</span><span class="rate-hint" title="日均互动 ' + (r.engagement_per_day || 0) + '">/天</span></td>'
      + "</tr>";
  }
  html += "</tbody></table>";
  wrap.innerHTML = html;
}

function metricCell(v, maxv, highlight) {
  const pct = maxv > 0 ? Math.min(100, Math.round((v / maxv) * 100)) : 0;
  const cls = highlight ? "cell-metric likes" : "cell-metric";
  return '<td class="t-num"><span class="' + cls + '"><span class="v">' + fmtNum(v || 0) + '</span><span class="b"><i style="width:' + pct + '%"></i></span></span></td>';
}

async function refreshDashboard() {
  const btns = [$("btnDashRefresh"), $("btnDashRefresh2")].filter(Boolean);
  btns.forEach((b) => (b.disabled = true));
  setRefreshBar(null, "正在后台抓取…（约 1-3 分钟，可离开此页）");
  showDashStatus(refreshBarHTML(null, "正在后台抓取最新内容…"), "refreshing");
  try {
    const r = await fetch("/api/dashboard/refresh", { method: "POST" });
    const d = await r.json();
    if (!d.ok) {
      setRefreshBar(null, d.message || "刷新未启动");
      showDashStatus(d.message || "刷新未启动", "err", 6000);
      return;
    }
    if (dashPollTimer) clearInterval(dashPollTimer);
    dashPollTimer = setInterval(async () => {
      try {
        const sr = await fetch("/api/dashboard/refresh/status");
        const s = await sr.json();
        if (s.status === "running") {
          const stage = s.progress || "正在抓取…";
          setRefreshBar(s.pct ?? null, stage);
          showDashStatus(refreshBarHTML(s.pct, stage), "refreshing");
        } else if (s.status === "done") {
          clearInterval(dashPollTimer); dashPollTimer = null;
          setRefreshBar(100, "刷新完成，共 " + (s.count || 0) + " 条", 8000);
          dashJustRefreshed = true;
          loadDashboard(true);
        } else if (s.status === "error") {
          clearInterval(dashPollTimer); dashPollTimer = null;
          setRefreshBar(null, "刷新失败：" + (s.error || ""));
          showDashStatus("刷新失败：" + esc(s.error), "err", 8000);
          loadDashboard(true);
        }
      } catch (e) { /* ignore */ }
    }, 3000);
  } finally {
    btns.forEach((b) => (b.disabled = false));
  }
}

function resetDashboard() {
  dashState.q = ""; dashState.start = ""; dashState.end = "";
  dashState.minLikes = 0; dashState.minReads = 0; dashState.minComments = 0;
  dashState.sort = "newest"; dashState.direction = "desc";
  $("dashQ").value = ""; $("dashStart").value = ""; $("dashEnd").value = "";
  $("dashMinLikes").value = "0"; $("dashMinReads").value = "0"; $("dashMinComments").value = "0";
  $("dashSort").value = "newest";
  syncQuickSel();
  if (dashDebounce) clearTimeout(dashDebounce);
  loadDashboard();
}

function isoDate(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return y + "-" + m + "-" + dd;
}
function rangeFor(from) {
  const now = new Date();
  if (from === "year") return { start: now.getFullYear() + "-01-01", end: isoDate(now) };
  if (from === "today-7") { const d = new Date(now); d.setDate(d.getDate() - 7); return { start: isoDate(d), end: isoDate(now) }; }
  if (from === "today-30") { const d = new Date(now); d.setDate(d.getDate() - 30); return { start: isoDate(d), end: isoDate(now) }; }
  return { start: "", end: "" };
}
function applyQuick(from) {
  const r = rangeFor(from);
  dashState.start = r.start; dashState.end = r.end;
  $("dashStart").value = r.start; $("dashEnd").value = r.end;
  syncQuickSel();
  loadDashboard();
}
function syncQuickSel() {
  document.querySelectorAll(".df-quick .chip-btn").forEach((b) => {
    const r = rangeFor(b.dataset.from);
    const active = b.dataset.from === "all"
      ? (!dashState.start && !dashState.end)
      : (r.start === dashState.start && r.end === dashState.end);
    b.classList.toggle("sel", active);
  });
}

/* ---------- 已发布内容看板：图表 ---------- */

const EC_TXT = "#dbe3f0", EC_MUTED = "#8a97b3", EC_BORDER = "#2b3550",
      EC_SPLIT = "#1f2740", EC_PALETTE =
        ["#6366f1", "#a78bfa", "#34d399", "#fbbf24", "#fb923c",
         "#f87171", "#60a5fa", "#f472b6"];
let ecCharts = {};

function ecInit(id) {
  if (typeof echarts === "undefined") return null;
  if (!ecCharts[id]) ecCharts[id] = echarts.init(document.getElementById(id));
  return ecCharts[id];
}
function ecBase() {
  return {
    color: EC_PALETTE,
    textStyle: { color: EC_TXT },
    tooltip: {
      backgroundColor: "#1a2130", borderColor: EC_BORDER,
      textStyle: { color: EC_TXT }, confine: true,
    },
  };
}

const CHART_TABS = ["trend", "dist", "funnel", "scatter", "top", "genre"];
function switchDashTab(tab) {
  if (!CHART_TABS.includes(tab)) return;
  dashTab = tab;
  document.querySelectorAll("#chartTabs .chart-tab").forEach((b) => b.classList.toggle("sel", b.dataset.tab === tab));
  CHART_TABS.forEach((t) => { $("chartPane-" + t).hidden = t !== tab; });
  const c = ecCharts["chart" + tab[0].toUpperCase() + tab.slice(1)];
  if (c) setTimeout(() => c.resize(), 30);
}

function _monthCounts(rows) {
  const map = {};
  rows.forEach((r) => {
    const m = (r.publish_date || "").slice(0, 7);
    if (m) map[m] = (map[m] || 0) + 1;
  });
  const keys = Object.keys(map).sort();
  return { labels: keys, values: keys.map((k) => map[k]) };
}

function _bucket(likes) {
  if (likes <= 0) return "0";
  if (likes <= 10) return "1-10";
  if (likes <= 50) return "11-50";
  if (likes <= 100) return "51-100";
  if (likes <= 500) return "101-500";
  return "500+";
}

function renderCharts(rows) {
  if (typeof echarts === "undefined") { $("dashChartSection").hidden = true; return; }
  const emptyEl = $("dashChartEmpty");
  if (!rows.length) {
    emptyEl.hidden = false;
    Object.keys(ecCharts).forEach((id) => { const c = ecCharts[id]; if (c) c.clear(); });
    return;
  }
  emptyEl.hidden = true;

  // 1) 发布量趋势（按月）
  const tr = _monthCounts(rows);
  ecInit("chartTrend").setOption({
    ...ecBase(), grid: { left: 40, right: 16, top: 22, bottom: 34 },
    tooltip: { ...ecBase().tooltip, trigger: "axis" },
    xAxis: { type: "category", data: tr.labels,
      axisLine: { lineStyle: { color: EC_BORDER } },
      axisLabel: { color: EC_MUTED, interval: Math.max(0, Math.ceil(tr.labels.length / 12) - 1) } },
    yAxis: { type: "value", axisLabel: { color: EC_MUTED },
      splitLine: { lineStyle: { color: EC_SPLIT } } },
    series: [{ type: "bar", data: tr.values, name: "发布数",
      barMaxWidth: 42,
      itemStyle: { borderRadius: [4, 4, 0, 0] } }],
  }, true);

  // 2) 互动分布（赞同分桶）
  const buckets = ["0", "1-10", "11-50", "51-100", "101-500", "500+"];
  const counts = buckets.map((b) => rows.filter((r) => _bucket(r.likes) === b).length);
  ecInit("chartDist").setOption({
    ...ecBase(), grid: { left: 40, right: 16, top: 22, bottom: 34 },
    xAxis: { type: "category", data: buckets,
      axisLine: { lineStyle: { color: EC_BORDER } }, axisLabel: { color: EC_MUTED } },
    yAxis: { type: "value", axisLabel: { color: EC_MUTED },
      splitLine: { lineStyle: { color: EC_SPLIT } } },
    series: [{ type: "bar", data: counts, name: "篇数",
      barMaxWidth: 42,
      itemStyle: { borderRadius: [4, 4, 0, 0] } }],
  }, true);

  // 3) 互动转化漏斗（篇均）
  const n = Math.max(rows.length, 1);
  const avg = (k) => rows.reduce((s, r) => s + (r[k] || 0), 0) / n;
  ecInit("chartFunnel").setOption({
    ...ecBase(), tooltip: { ...ecBase().tooltip, formatter: "{b}: {c}" },
    series: [{ type: "funnel", left: "6%", width: "88%", top: 10, bottom: 10,
      minSize: "15%", maxSize: "95%", sort: "descending",
      label: { color: EC_TXT, formatter: "{b} {c}" },
      data: [
        { value: Math.round(avg("reads")), name: "阅读" },
        { value: Math.round(avg("likes")), name: "赞同" },
        { value: Math.round(avg("comments")), name: "评论" },
        { value: Math.round(avg("collects")), name: "收藏" },
        { value: Math.round(avg("favors")), name: "喜欢" },
      ] }],
  }, true);

  // 4) 阅读 · 赞同 散点
  const scatter = rows.slice(0, 600).map((r) => [r.reads, r.likes]);
  ecInit("chartScatter").setOption({
    ...ecBase(), grid: { left: 46, right: 20, top: 22, bottom: 40 },
    xAxis: { type: "value", name: "阅读", nameTextStyle: { color: EC_MUTED },
      axisLabel: { color: EC_MUTED }, splitLine: { lineStyle: { color: EC_SPLIT } } },
    yAxis: { type: "value", name: "赞同", nameTextStyle: { color: EC_MUTED },
      axisLabel: { color: EC_MUTED }, splitLine: { lineStyle: { color: EC_SPLIT } } },
    series: [{ type: "scatter", data: scatter, symbolSize: 6,
      itemStyle: { color: "rgba(99,102,241,0.65)" } }],
  }, true);

  // 5) Top 20（按赞同）
  const top = [...rows].sort((a, b) => b.likes - a.likes).slice(0, 20);
  const topLabels = top.map((r) => (r.title || "(无标题)").slice(0, 20));
  ecInit("chartTop").setOption({
    ...ecBase(), grid: { left: 130, right: 40, top: 8, bottom: 22 },
    xAxis: { type: "value", axisLabel: { color: EC_MUTED },
      splitLine: { lineStyle: { color: EC_SPLIT } } },
    yAxis: { type: "category", data: topLabels,
      axisLabel: { color: EC_TXT, fontSize: 11 }, axisLine: { lineStyle: { color: EC_BORDER } } },
    series: [{ type: "bar", data: top.map((r) => r.likes),
      barMaxWidth: 22,
      itemStyle: { borderRadius: [0, 4, 4, 0] } }],
  }, true);

  // 6) 题材分布（后端已按标题 + 正文识别）
  const gmap = {};
  rows.forEach((r) => { const g = r.genre || "其他"; gmap[g] = (gmap[g] || 0) + 1; });
  const keys = Object.keys(gmap);
  ecInit("chartGenre").setOption({
    ...ecBase(), tooltip: { ...ecBase().tooltip, formatter: "{b}: {c} 篇 ({d}%)" },
    series: [{ type: "pie", radius: ["38%", "68%"], center: ["50%", "50%"],
      label: { color: EC_TXT, formatter: "{b} {c}" },
      data: keys.map((k) => ({ name: k, value: gmap[k] })) }],
  }, true);
}

/* ---------- 已发布内容：筛选待清理 / 删除 ---------- */
let clRows = [];
let _clArmPrune = false, _clArmDel = false;

function openClearModal() {
  if (!$("clBefore").value) {
    const d = new Date(); d.setMonth(d.getMonth() - 6);
    $("clBefore").value = d.toISOString().slice(0, 10);
  }
  _clArmPrune = _clArmDel = false;
  $("clPruneBtn").textContent = "移除出看板（本地）";
  $("clDeleteZhihuBtn").textContent = "从知乎删除（不可逆）";
  $("clList").innerHTML = '<div class="empty-note">设置条件后点「开始筛选」</div>';
  $("clCount").textContent = ""; $("clStatus").textContent = "";
  updateClButtons();
  $("dashClearMask").classList.add("show");
}

function clReset() {
  $("clBefore").value = ""; $("clLikes").value = 5; $("clReads").value = 100;
  $("clComments").value = 1; $("clCollects").value = 0; $("clFavors").value = 0;
  openClearModal();
}

async function runClFilter() {
  const p = new URLSearchParams({
    before: $("clBefore").value,
    max_likes: $("clLikes").value || 0,
    max_reads: $("clReads").value || 0,
    max_comments: $("clComments").value || 0,
    max_collects: $("clCollects").value || 0,
    max_favors: $("clFavors").value || 0,
  });
  _clArmPrune = _clArmDel = false;
  $("clList").innerHTML = '<div class="empty-note">筛选中…</div>';
  try {
    const r = await fetch("/api/dashboard/poor?" + p.toString());
    const d = await r.json();
    clRows = d.rows || [];
    renderClList();
    $("clCount").textContent = "共 " + d.count + " 条（全部 " + d.all_total + "）";
  } catch (e) {
    $("clList").innerHTML = '<div class="empty-note">筛选失败：' + esc(e.message) + "</div>";
  }
}

function renderClList() {
  const box = $("clList");
  if (!clRows.length) {
    box.innerHTML = '<div class="empty-note">无匹配内容，试试调低阈值/放宽范围</div>';
    updateClButtons(); return;
  }
  box.innerHTML = clRows.map((r) =>
    '<div class="cl-item"><label><input type="checkbox" class="cl-check" value="' + esc(r.aid) + '">'
    + '<span class="ct" title="' + esc(r.title) + '">' + esc(r.title) + "</span>"
    + (r.genre ? '<span class="genre-chip">' + esc(r.genre) + "</span>" : "")
    + "</label>"
    + '<span class="cm">' + esc(r.publish_date) + " · 赞" + fmtNum(r.likes) + " · 读" + fmtNum(r.reads) + " · 评" + fmtNum(r.comments) + "</span></div>"
  ).join("");
  box.querySelectorAll(".cl-check").forEach((cb) => cb.addEventListener("change", updateClButtons));
  updateClButtons();
}

function selectedAids() {
  return Array.from(document.querySelectorAll(".cl-check"))
    .filter((c) => c.checked).map((c) => c.value);
}
function setAll(v) {
  document.querySelectorAll(".cl-check").forEach((c) => (c.checked = v));
  updateClButtons();
}
function updateClButtons() {
  const n = selectedAids().length;
  $("clPruneBtn").disabled = n === 0;
  $("clDeleteZhihuBtn").disabled = n === 0;
  $("clStatus").textContent = n ? "已选 " + n + " 条" : "";
}

async function clPrune() {
  const aids = selectedAids();
  if (!aids.length) return;
  if (!_clArmPrune) {
    _clArmPrune = true; _clArmDel = false;
    $("clPruneBtn").textContent = "再点一次确认移除 " + aids.length + " 篇";
    $("clDeleteZhihuBtn").textContent = "从知乎删除（不可逆）";
    $("clStatus").textContent = "将从看板移除 " + aids.length + " 篇（仅本地，可重新刷新恢复）";
    return;
  }
  try {
    const r = await fetch("/api/dashboard/prune", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ aids }) });
    const d = await r.json();
    $("clStatus").textContent = "已移除 " + d.removed + " 条";
    $("dashClearMask").classList.remove("show");
    loadDashboard();
  } catch (e) { $("clStatus").textContent = "移除失败：" + e.message; }
}

async function clDeleteZhihu() {
  const aids = selectedAids();
  if (!aids.length) return;
  if (!_clArmDel) {
    _clArmDel = true; _clArmPrune = false;
    $("clDeleteZhihuBtn").textContent = "再点一次确认删除 " + aids.length + " 篇";
    $("clPruneBtn").textContent = "移除出看板（本地）";
    $("clStatus").textContent = "⚠ 将从知乎删除 " + aids.length + " 篇，不可恢复！";
    return;
  }
  const r = await fetch("/api/dashboard/delete-zhihu", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ aids }) });
  const d = await r.json();
  if (!d.ok) { $("clStatus").textContent = d.message || "未启动"; return; }
  $("clStatus").textContent = "删除中…"; _clArmDel = false;
  $("clDeleteZhihuBtn").textContent = "从知乎删除（不可逆）";
  const poll = setInterval(async () => {
    try {
      const sr = await fetch("/api/dashboard/delete-zhihu/status");
      const s = await sr.json();
      $("clStatus").textContent = s.status === "running"
        ? "删除中：" + s.progress : (s.status === "done"
          ? "已删除 " + s.deleted + "/" + s.count + " 条" : "删除失败：" + s.error);
      if (s.status === "done" || s.status === "error") { clearInterval(poll); loadDashboard(); }
    } catch (e) { /* ignore */ }
  }, 3000);
}

/* ---------- 草稿箱素材管理 ---------- */

let draftState = { q: "", start: "", end: "", minChars: 0, maxChars: 0, sort: "updated" };
let draftData = { rows: [], total: 0, all_total: 0, stats: {} };
let draftSel = new Set();
let draftDebounce = null;
let draftPollTimer = null;
// 进度条隐藏计时器使用共享的 draftHideTimer 对象（见上方仪表盘区域）

function draftParams() {
  const p = new URLSearchParams();
  p.set("q", draftState.q.trim());
  p.set("start", draftState.start);
  p.set("end", draftState.end);
  p.set("min_chars", draftState.minChars || 0);
  p.set("max_chars", draftState.maxChars || 0);
  p.set("sort", draftState.sort);
  p.set("direction", "desc");
  return p.toString();
}

async function loadDrafts() {
  try {
    const r = await fetch("/api/drafts?" + draftParams());
    const d = await r.json();
    draftData = d;
    renderDraftHeader(d);
    renderDraftKpis(d.stats || {});
    renderDraftChips();
    renderDraftList();
    renderDraftSrc(d);
    const rs = d.refresh || {};
    if (rs.status === "running") {
      showDraftStatus('<span class="spin"></span>刷新中：' + esc(rs.progress || "…"), "refreshing");
      setDrfBar(rs.pct, rs.progress);
    } else if (rs.status === "error") {
      showDraftStatus("上次刷新失败：" + esc(rs.error), "err", 8000);
    } else if (rs.status === "done") {
      hideDraftStatus();
    }
    // refresh/delete 完成提示由各自流程的 showDraftStatus 负责保留并自动隐藏
  } catch (e) {
    $("draftList").innerHTML = '<div class="dft-empty">⚠️ 加载失败：' + esc(e.message) + "</div>";
  }
}

function renderDraftHeader(d) {
  $("draftCount").innerHTML = "显示 <b>" + (d.total || 0) + "</b> / 共 <b>" + (d.all_total || 0) + "</b> 个";
  const gen = d.generated_at ? d.generated_at.slice(0, 10) : "";
  $("draftSnap").textContent = gen ? "快照 " + gen : "快照 · 暂无";
  $("draftSnap").classList.toggle("ok", !!gen);
}

function renderDraftSrc(d) {
  const gen = d.generated_at ? d.generated_at.replace("T", " ").slice(0, 16) : "无快照";
  $("draftSrcLine").innerHTML = "快照文件：<b>" + esc(baseName(d.source_file)) + "</b><br>抓取时间：" + esc(gen);
}

function dtkpi(label, value, sub) {
  return '<div class="kpi-card"><div class="kpi-label">' + esc(label) + '</div><div class="kpi-value">' + value + '</div><div class="kpi-sub">' + esc(sub) + '</div></div>';
}

function renderDraftKpis(st) {
  const box = $("draftKpis");
  if (!st || !st.total) {
    box.innerHTML = dtkpi("草稿数", "0", "还未抓取草稿");
    return;
  }
  const lo = (st.date_min || "").slice(0, 7);
  const hi = (st.date_max || "").slice(0, 7);
  box.innerHTML =
    dtkpi("草稿数", fmtNum(st.total), "快照共 " + fmtNum(draftData.all_total) + " 个") +
    dtkpi("总字数", fmtNum(st.sum_chars), "") +
    dtkpi("平均字数", fmtNum(st.avg_chars), "") +
    dtkpi("更新范围", (lo && hi) ? lo + " ~ " + hi : "—", "最早 ~ 最近");
}

function draftFilterItems() {
  const items = [];
  if (draftState.q.trim()) {
    items.push({ key: "q", label: "关键词：" + draftState.q.trim(),
      clean: () => { draftState.q = ""; $("draftQ").value = ""; } });
  }
  if (draftState.start || draftState.end) {
    items.push({ key: "date", label: "时间：" + (draftState.start || "…") + " ~ " + (draftState.end || "…"),
      clean: () => { draftState.start = ""; draftState.end = ""; $("draftStart").value = ""; $("draftEnd").value = ""; syncDraftQuick(); } });
  }
  if (draftState.minChars > 0) {
    items.push({ key: "minC", label: "字数 ≥ " + draftState.minChars,
      clean: () => { draftState.minChars = 0; $("draftMinChars").value = "0"; } });
  }
  if (draftState.maxChars > 0) {
    items.push({ key: "maxC", label: "字数 ≤ " + draftState.maxChars,
      clean: () => { draftState.maxChars = 0; $("draftMaxChars").value = "0"; } });
  }
  if (draftState.sort !== "updated") {
    items.push({ key: "sort", label: "排序：字数最多",
      clean: () => { draftState.sort = "updated"; $("draftSort").value = "updated"; } });
  }
  return items;
}

function renderDraftChips() {
  const bar = $("draftFilterbar");
  const items = draftFilterItems();
  if (!items.length) { bar.className = "dash-filterbar"; bar.innerHTML = ""; return; }
  bar.className = "dash-filterbar show";
  let html = "";
  for (const it of items) {
    html += '<span class="f-chip">' + esc(it.label) + '<button data-key="' + it.key + '" title="移除条件">×</button></span>';
  }
  html += '<button class="f-btn" id="draftClearAll">清除全部条件</button>';
  bar.innerHTML = html;
  bar.querySelectorAll(".f-chip button").forEach((b) => b.addEventListener("click", () => {
    const it = items.find((x) => x.key === b.dataset.key);
    if (it) { it.clean(); loadDrafts(); }
  }));
  $("draftClearAll").addEventListener("click", resetDrafts);
}

function renderDraftList() {
  const rows = draftData.rows || [];
  const wrap = $("draftList");
  $("draftTableInfo").innerHTML = "共 <b>" + rows.length + "</b> 个草稿";
  if (!rows.length) {
    wrap.innerHTML = '<div class="dft-empty">🔍 无匹配草稿，调整筛选或先「从知乎刷新草稿箱」</div>';
    updateDraftSelAll();
    return;
  }
  let html = "";
  for (const r of rows) {
    const cb = draftSel.has(r.qid) ? " checked" : "";
    html += '<div class="dft-row' + (draftSel.has(r.qid) ? " sel" : "") + '" data-qid="' + esc(r.qid) + '">'
      + '<input type="checkbox" class="cb" data-qid="' + esc(r.qid) + '"' + cb + '>'
      + '<div class="dft-main"><div class="dft-title" title="点击在浏览器中打开知乎编辑页">' + esc(r.title || "(无标题)") + '</div>'
      + '<div class="dft-excerpt">' + esc((r.content || "").slice(0, 90)) + '</div></div>'
      + '<div class="dft-meta"><span>' + esc(r.updated_date || "-") + "</span><span class='chars'>" + fmtNum(r.chars) + " 字</span>"
      + '<button class="dft-view" type="button" title="查看本地全文">详情</button></div>'
      + "</div>";
  }
  wrap.innerHTML = html;
  updateDraftSelAll();
}

function onDraftListClick(e) {
  const row = e.target.closest(".dft-row");
  if (!row) return;
  const qid = row.dataset.qid;
  if (e.target.classList.contains("cb")) {
    toggleDraftSel(qid, e.target.checked);
    return;
  }
  const r = (draftData.rows || []).find((x) => x.qid === qid);
  if (!r) return;
  if (e.target.closest(".dft-view")) {
    openDraftView(r);
    return;
  }
  // 点击条目 → 在浏览器新标签打开知乎对应草稿编辑页
  if (r.url) {
    window.open(r.url, "_blank");
  }
}

function toggleDraftSel(qid, on) {
  if (on) draftSel.add(qid); else draftSel.delete(qid);
  const row = document.querySelector('.dft-row[data-qid="' + CSS.escape(qid) + '"]');
  if (row) row.classList.toggle("sel", on);
  updateDraftSelAll();
}

function selectedDraftQids() { return Array.from(draftSel); }

function updateDraftSelAll() {
  const n = selectedDraftQids().length;
  $("draftSelStatus").textContent = n ? "已选 " + n + " 个" : "";
  $("btnDraftsDelete").disabled = n === 0;
  const totalRows = document.querySelectorAll("#draftList .dft-row").length;
  const checked = document.querySelectorAll("#draftList .cb:checked").length;
  $("draftSelAll").checked = totalRows > 0 && checked === totalRows;
}

function openDraftView(r) {
  $("draftViewTitle").textContent = r.title || "(无标题)";
  const head = ((r.updated_date ? "更新于 " + r.updated_date : "") + (r.chars ? " · " + r.chars + " 字" : ""));
  $("draftViewBody").textContent = (head ? head + "\n\n" : "") + (r.content || "(无正文)");
  $("draftViewMask").classList.add("show");
}

function closeDraftView() {
  $("draftViewMask").classList.remove("show");
}

async function refreshDrafts() {
  const btns = [$("btnDraftsRefresh"), $("btnDraftsRefresh2")].filter(Boolean);
  btns.forEach((b) => (b.disabled = true));
  setDrfBar(null, "正在后台抓取草稿箱…（约 1-3 分钟）");
  showDraftStatus('<span class="spin"></span>正在后台抓取草稿箱…', "refreshing");
  try {
    const r = await fetch("/api/drafts/refresh", { method: "POST" });
    const d = await r.json();
    if (!d.ok) {
      setDrfBar(null, d.message || "刷新未启动");
      showDraftStatus(d.message || "刷新未启动", "err", 6000);
      return;
    }
    if (draftPollTimer) clearInterval(draftPollTimer);
    draftPollTimer = setInterval(async () => {
      try {
        const sr = await fetch("/api/drafts/refresh/status");
        const s = await sr.json();
        if (s.status === "running") {
          const stage = s.progress || "正在抓取…";
          setDrfBar(s.pct ?? null, stage);
          showDraftStatus(refreshBarHTML(s.pct, stage), "refreshing");
        } else if (s.status === "done") {
          clearInterval(draftPollTimer); draftPollTimer = null;
          setDrfBar(100, "刷新完成，共 " + (s.count || 0) + " 个", 8000);
          draftSel.clear();
          loadDrafts();
          showDraftStatus("刷新完成，共 " + (s.count || 0) + " 个草稿", "ok", 6000);
        } else if (s.status === "error") {
          clearInterval(draftPollTimer); draftPollTimer = null;
          setDrfBar(null, "刷新失败：" + (s.error || ""));
          showDraftStatus("刷新失败：" + esc(s.error), "err", 8000);
          loadDrafts(true);
        }
      } catch (e) { /* ignore */ }
    }, 3000);
  } finally {
    btns.forEach((b) => (b.disabled = false));
  }
}

function syncDraftQuick() {
  document.querySelectorAll("#pane-drafts .df-quick .chip-btn").forEach((b) => {
    const r = rangeFor(b.dataset.from);
    const active = b.dataset.from === "all"
      ? (!draftState.start && !draftState.end)
      : (r.start === draftState.start && r.end === draftState.end);
    b.classList.toggle("sel", active);
  });
}

function applyDraftQuick(from) {
  const r = rangeFor(from);
  draftState.start = r.start;
  draftState.end = r.end;
  $("draftStart").value = r.start;
  $("draftEnd").value = r.end;
  syncDraftQuick();
  loadDrafts();
}

function resetDrafts() {
  draftState = { q: "", start: "", end: "", minChars: 0, maxChars: 0, sort: "updated" };
  $("draftQ").value = ""; $("draftStart").value = ""; $("draftEnd").value = "";
  $("draftMinChars").value = "0"; $("draftMaxChars").value = "0"; $("draftSort").value = "updated";
  syncDraftQuick();
  if (draftDebounce) clearTimeout(draftDebounce);
  loadDrafts();
}

async function draftsDeleteSelected() {
  const qids = selectedDraftQids();
  if (!qids.length) return;
  if (!confirm("确认从知乎草稿箱删除 " + qids.length + " 个草稿？\n此操作不可恢复！")) return;
  try {
    const r = await fetch("/api/drafts/delete", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ qids }) });
    const d = await r.json();
    if (!d.ok) { showDraftStatus(d.message || "删除未启动", "err", 8000); return; }
    showDraftStatus('<span class="spin"></span>删除中…', "refreshing");
    if (draftPollTimer) clearInterval(draftPollTimer);
    draftPollTimer = setInterval(async () => {
      try {
        const sr = await fetch("/api/drafts/delete/status");
        const s = await sr.json();
        if (s.status === "running") {
          showDraftStatus('<span class="spin"></span>' + esc(s.progress || "删除中…"), "refreshing");
        } else if (s.status === "done") {
          clearInterval(draftPollTimer); draftPollTimer = null;
          showDraftStatus(s.progress || ("已删除 " + (s.deleted || 0) + " 个"), "ok", 8000);
          draftSel.clear();
          loadDrafts();
        } else if (s.status === "error") {
          clearInterval(draftPollTimer); draftPollTimer = null;
          showDraftStatus("删除失败：" + esc(s.error), "err", 8000);
          loadDrafts();
        }
      } catch (e) { /* ignore */ }
    }, 3000);
  } catch (e) {
    showDraftStatus("删除请求失败：" + esc(e.message), "err", 8000);
  }
}

/* ---------- 文风提炼 ---------- */

const profileSourceSel = $("profileSourceSel");
const btnProfile = $("btnProfile");
const btnGeneralProfile = $("btnGeneralProfile");

async function loadProfileSources() {
  try {
    const r = await fetch("/api/profile-sources");
    const data = await r.json();
    const hint = $("profileLibHint");
    if (!data.authors.length) {
      profileSourceSel.innerHTML = '<option value="">（采集库为空）</option>';
      if (hint) hint.textContent = '采集库为空，请先在上方「故事采集」添加样本';
      return;
    }
    const total = data.authors.reduce((s, a) => s + (a.records || 0), 0);
    if (hint) hint.textContent = `采集库现有 ${data.authors.length} 位作者、共 ${total} 篇样本`;
    profileSourceSel.innerHTML = "";
    data.authors.forEach((a) => {
      const opt = document.createElement("option");
      opt.value = a.name;
      opt.textContent = `${a.name}（${a.records} 条记录）`;
      profileSourceSel.appendChild(opt);
    });
    profileSourceSel.disabled = false;
    btnProfile.disabled = false;
  } catch (e) { /* ignore */ }
}

function runProfileMode(mode, author) {
  const body = { mode };
  if (author) body.author = author;
  $("logBox").innerHTML = "";
  genChars = 0;
  formatScore = null;
  fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => {
    if (!r.ok) {
      return r.json().catch(() => ({})).then((err) => {
        addLog("启动失败：" + (err.detail || r.status), "error");
        return null;
      });
    }
    connectSSE();
    return null;
  }).catch((e) => addLog("无法连接服务：" + e, "error"));
}

btnProfile.addEventListener("click", () => {
  if (profileSourceSel.disabled || !profileSourceSel.value) return;
  runProfileMode("profile", profileSourceSel.value);
});

btnGeneralProfile.addEventListener("click", () => {
  runProfileMode("general_profile");
});

/* ---------- 故事采集 ---------- */

const btnCollect = $("btnCollect");

function runCollect() {
  const url = $("collectUrl").value.trim();
  if (!url) {
    alert("请输入作者回答列表 URL（形如 https://www.zhihu.com/people/xxx/answers）");
    return;
  }
  if (!/^https?:\/\//.test(url)) {
    alert("URL 须以 http(s):// 开头");
    return;
  }
  const count = parseInt($("collectCount").value, 10) || 10;
  const body = {
    mode: "collect",
    url,
    count: Math.min(500, Math.max(1, count)),
  };
  $("logBox").innerHTML = "";
  genChars = 0;
  formatScore = null;
  btnCollect.disabled = true;
  btnCollect.textContent = "采集中…";
  fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => {
    if (!r.ok) {
      return r.json().catch(() => ({})).then((err) => {
        addLog("启动失败：" + (err.detail || r.status), "error");
        return null;
      });
    }
    connectSSE();
    return null;
  }).catch((e) => addLog("无法连接服务：" + e, "error"))
    .finally(() => {
      btnCollect.disabled = false;
      btnCollect.textContent = "开始采集";
    });
}

btnCollect.addEventListener("click", runCollect);

/* ---------- 采集库管理 ---------- */

const libMask = $("libMask");
const libBody = $("libBody");
let libAuthors = [];

function escAttr(s) {
  return String(s ?? "").replace(/"/g, "&quot;");
}

async function loadStoryLib() {
  try {
    const r = await fetch("/api/storylib");
    const data = await r.json();
    libAuthors = data.authors || [];
    renderStoryLib();
  } catch (e) {
    libBody.innerHTML = '<div class="empty-note">采集库加载失败</div>';
  }
}

function renderStoryLib() {
  if (!libAuthors.length) {
    libBody.innerHTML = '<div class="empty-note">采集库为空（还没有采集过任何故事）</div>';
    return;
  }
  const total = libAuthors.reduce((n, a) => n + a.records, 0);
  let html = `<div class="slib-total" style="margin-bottom:10px">共 ${libAuthors.length} 位作者，${total} 条记录。删除后不可恢复。</div>`;
  libAuthors.forEach((a) => {
    const hp = a.has_profile
      ? `<div class="has-profile">⚠ 已有文风签名（data/authors/）——删除样本不会删签名，可另行处理</div>` : "";
    html += `
      <div class="slib-author" data-author="${escAttr(a.name)}">
        <div class="info">
          <div class="nm">${esc(a.name)}</div>
          <div class="dt">${a.records} 条记录${hp ? "" : ""}</div>
          ${hp}
        </div>
        <button class="btn-sm" data-act="toggle">展开</button>
        <button class="btn-sm" data-act="del">删除全部</button>
      </div>`;
  });
  libBody.innerHTML = html;
}

async function loadAuthorDetails(author) {
  try {
    const r = await fetch("/api/storylib?author=" + encodeURIComponent(author));
    return (await r.json()).records || [];
  } catch (e) { return []; }
}

libBody.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const row = btn.closest(".slib-author");
  const author = row.dataset.author;
  const act = btn.dataset.act;

  if (act === "toggle") {
    const open = (btn.textContent === "收起");
    btn.textContent = open ? "展开" : "收起";
    const detail = row.querySelector(".slib-detail");
    if (open) { if (detail) detail.remove(); return; }
    const records = await loadAuthorDetails(author);
    const items = records.map((rec) => `
      <div class="slib-item" data-url="${escAttr(rec.answer_url)}">
        <span class="t">${esc(rec.title)}</span>
        <span class="m">${rec.chars} 字 · ${esc(rec.collected_at)}</span>
        <button class="btn-sm" data-act="delOne">删除</button>
      </div>`).join("");
    const d = document.createElement("div");
    d.className = "slib-detail";
    d.innerHTML = items || '<div class="empty-note">（无记录）</div>';
    row.appendChild(d);
    return;
  }

  if (act === "del") {
    const n = row.querySelector(".dt").textContent;
    if (!confirm(`删除作者「${author}」的全部采集记录？\n（${n}，不可恢复）`)) return;
    const r = await fetch("/api/storylib", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ author }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      alert("删除失败：" + (err.detail || r.status));
      return;
    }
    loadStoryLib();
    loadProfileSources();
    return;
  }

  if (act === "delOne") {
    const url = btn.closest(".slib-item").dataset.url;
    const t = btn.closest(".slib-item").querySelector(".t").textContent;
    if (!confirm(`删除单条记录「${t}」？\n（不可恢复）`)) return;
    const r = await fetch("/api/storylib", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      alert("删除失败：" + (err.detail || r.status));
      return;
    }
    // 刷新列表 + 重新展开该作者详情
    loadStoryLib();
    loadProfileSources();
    btn.closest(".slib-item").remove();
    const left = row.querySelectorAll(".slib-item").length;
    if (!left) { const d = row.querySelector(".slib-detail"); if (d) d.remove(); }
    return;
  }
});

$("btnManageLib").addEventListener("click", () => {
  libMask.classList.add("show");
  loadStoryLib();
});
$("libClose").addEventListener("click", () => libMask.classList.remove("show"));
libMask.addEventListener("click", (e) => {
  if (e.target === libMask) libMask.classList.remove("show");
});

/* ---------- 首启引导（Edge → API Key → 知乎登录） ---------- */

let setupNeeded = false;
let setupTimer = null;

async function fillSetupProviders() {
  try {
    const r = await fetch("/api/models");
    const data = await r.json();
    const sel = $("setupProviderSel");
    (data.providers || []).forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.name;
      opt.textContent = p.name;
      sel.appendChild(opt);
    });
    if (data.current) sel.value = data.current.provider;
  } catch (e) { /* 服务不可用时保持空 */ }
}

async function setModeChoice(mode) {
  try {
    const r = await fetch("/api/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    if (!r.ok) return;
    const data = await r.json();
    genMode = data.effective.mode;
    document.querySelectorAll('input[name="genMode"]').forEach((el) => {
      el.checked = (el.value === genMode);
    });
    loadConfig();
  } catch (e) { /* 旧服务无 /api/mode */ }
}

/* 用户手动点选的卡片（null=未点选，按后端状态取默认）。
   轮询每 2.5s 重算默认选中，用户点选后不得被覆盖——
   否则未配置 API 时刚点开的输入框会被轮询切走。 */
let cardUserPick = null;

/* 卡片选中状态：用户点选优先；否则默认选中已配置的一方；
   都未配置时默认 Web（免费）。 */
function selectModeCard(st) {
  const mcApi = $("mcApi"), mcWeb = $("mcWeb");
  if (cardUserPick === "api") {
    mcApi.classList.add("sel"); mcWeb.classList.remove("sel");
    return;
  }
  if (cardUserPick === "web") {
    mcWeb.classList.add("sel"); mcApi.classList.remove("sel");
    return;
  }
  mcApi.classList.toggle("sel", !!st.llm_configured && !st.web_llm_logged_in);
  mcWeb.classList.toggle("sel", !mcApi.classList.contains("sel"));
}

function renderModeCardState(st) {
  const mcApi = $("mcApi"), mcWeb = $("mcWeb");
  mcApi.classList.toggle("done", !!st.llm_configured);
  mcApi.querySelector(".mc-d").textContent =
    st.llm_configured ? "✓ 已配置 API Key，可随时切换使用。" :
    "付费、响应快。DeepSeek 开放平台申请 API Key。";
  mcWeb.classList.toggle("done", !!st.web_llm_logged_in);
  mcWeb.querySelector(".mc-d").textContent =
    st.web_llm_logged_in ? "✓ 已登录 DeepSeek 网页版，可直接使用。" :
    "免费。登录 chat.deepseek.com 后由浏览器自动操作。";
  if (st.login_running && st.login_kind === "deepseek") {
    const b = $("btnWebLogin");
    b.disabled = true;
    b.textContent = "登录引导中，请在弹出的 Edge 窗口完成…";
    $("webErr").textContent = "";
  } else {
    $("btnWebLogin").disabled = false;
    $("btnWebLogin").textContent = "打开 Edge 登录 DeepSeek";
  }
  if (st.login_error && st.login_kind === "deepseek") {
    $("webErr").textContent = "登录未完成：" + st.login_error;
  }
}

async function loadSetupStatus() {
  try {
    const r = await fetch("/api/setup/status");
    const st = await r.json();
    setupCache = st;
    if (st.version) $("setupVersion").textContent = "v" + st.version;
    setupNeeded = !!st.setup_needed;
    if (!st.setup_needed && !manualGuideOpen) { hideSetupWizard(); return; }

    const edge = $("stepEdge");
    const mode = $("stepMode");
    const zhihu = $("stepZhihu");

    if (st.edge_ok) {
      edge.classList.add("done");
      edge.querySelector(".s-badge").textContent = "✓";
      $("stepEdgeDesc").textContent = "已检测到 Microsoft Edge。";
    } else {
      edge.classList.remove("done");
      edge.querySelector(".s-badge").textContent = "1";
      $("stepEdgeDesc").innerHTML =
        "未找到 Microsoft Edge。AutoQuill 需要 Edge 来操作知乎。<br>" +
        "请安装：<a href=\"https://www.microsoft.com/edge\" target=\"_blank\" rel=\"noopener\">microsoft.com/edge</a>，安装完成后点「重新检测」。";
    }
    edge.style.display = "";

    const modeDone = st.llm_configured || st.web_llm_logged_in;
    mode.classList.toggle("done", modeDone);
    mode.querySelector(".s-badge").textContent = modeDone ? "✓" : "2";
    mode.style.display = "";
    renderModeCardState(st);
    selectModeCard(st);

    if (st.zhihu_logged_in) {
      zhihu.classList.add("done");
      zhihu.querySelector(".s-badge").textContent = "✓";
      $("stepZhihuDesc").textContent = "已保存知乎登录态。";
      zhihu.querySelector(".s-ctl").style.display = "none";
    } else {
      zhihu.classList.remove("done");
      zhihu.querySelector(".s-badge").textContent = "3";
      zhihu.querySelector(".s-ctl").style.display = "";
      $("stepZhihuDesc").innerHTML =
        "用于采集素材与发布草稿。点击后弹出 Edge 窗口，扫码或短信登录即可，检测到登录会自动保存。";
      if (st.login_running && st.login_kind === "zhihu") {
        $("btnZhihuLogin").disabled = true;
        $("btnZhihuLogin").textContent = "登录引导中，请在弹出的 Edge 窗口完成…";
      } else {
        $("btnZhihuLogin").disabled = false;
        $("btnZhihuLogin").textContent = "打开 Edge 登录知乎";
      }
      if (st.login_error && st.login_kind === "zhihu") {
        $("zhihuErr").textContent = "登录未完成：" + st.login_error;
      }
    }
    zhihu.style.display = "";

    $("setupMask").classList.add("show");
    const allDone = st.edge_ok && st.zhihu_logged_in
      && (st.llm_configured || st.web_llm_logged_in);
    // allDone 收尾只适用于首启引导；按需引导（manual）等用户点
    // 「进入控制台」关闭——用户可能整体配置完成但当前模式仍缺配置
    if (allDone && !manualGuideOpen) hideSetupWizard();
    return st;
  } catch (e) { /* 旧服务无 setup 端点 */ }
}

function hideSetupWizard() {
  manualGuideOpen = false;
  $("setupMask").classList.remove("show");
  if (setupTimer) { clearInterval(setupTimer); setupTimer = null; }
}

async function pollSetupStatus() {
  try {
    const st = await loadSetupStatus();
    if (st && st.setup_needed) {
      if (!st.login_running) {
        $("btnZhihuLogin").disabled = false;
        $("btnZhihuLogin").textContent = "打开 Edge 登录知乎";
      }
    } else if (st && !st.setup_needed) {
      if (!manualGuideOpen) hideSetupWizard();  // 按需引导窗由用户关闭
    }
  } catch (e) { /* ignore */ }
}

$("btnSetup").addEventListener("click", () => {
  // 首次引导未完成 → 打开引导向导；已完成 → 打开设置弹窗
  if (setupNeeded) { loadSetupStatus(); return; }
  $("settingsMask").classList.add("show");
  loadConfig(); loadMode(); loadBrowserMode(); loadWebPreset();
  loadAuthors(); loadQuestionSource();
});

$("settingsClose").addEventListener("click", () => $("settingsMask").classList.remove("show"));
$("settingsMask").addEventListener("click", (e) => {
  if (e.target === $("settingsMask")) $("settingsMask").classList.remove("show");
});

$("btnSetupFinish").addEventListener("click", hideSetupWizard);

$("mcApi").addEventListener("click", () => {
  cardUserPick = "api";
  selectModeCard(setupCache || {});
  if (!$("setupApiKey").value && !$("mcApi").classList.contains("done")) {
    $("setupApiKey").focus();
  }
});
$("mcWeb").addEventListener("click", () => {
  cardUserPick = "web";
  selectModeCard(setupCache || {});
  if (!$("mcWeb").classList.contains("done")) {
    $("btnWebLogin").focus();
  }
});

/* ---------- 检查更新 ---------- */

async function checkUpdate() {
  const btn = $("btnUpdate");
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = "检查中…";
  try {
    const r = await fetch("/api/update/check");
    const d = await r.json();
    if (d.error) {
      btn.textContent = orig;
      alert("检查更新失败：" + d.error);
      return;
    }
    if (d.has_update) {
      btn.textContent = "有新版本";
      if (confirm(`发现新版本 ${d.latest}（当前 ${d.current}）\n\n点击「确定」前往下载页。下载后运行安装包即可升级，数据会自动保留。`)) {
        window.open(d.url, "_blank");
      }
    } else {
      btn.textContent = "已是最新版本";
      setTimeout(() => { btn.textContent = orig; }, 3000);
    }
  } catch (e) {
    btn.textContent = orig;
    alert("检查更新失败：" + e.message);
  } finally {
    btn.disabled = false;
  }
}

$("btnUpdate").addEventListener("click", checkUpdate);

$("btnEdgeOk").addEventListener("click", () => {
  loadSetupStatus();
});

$("btnKeySave").addEventListener("click", async () => {
  const key = $("setupApiKey").value.trim();
  $("keyErr").textContent = "";
  $("keyOk").textContent = "";
  if (!key) { $("keyErr").textContent = "请粘贴 API Key"; return; }
  const btn = $("btnKeySave");
  btn.disabled = true;
  btn.textContent = "保存中…";
  try {
    const r = await fetch("/api/setup/apikey", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: $("setupProviderSel").value, api_key: key }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || "保存失败");
    const t = await fetch("/api/setup/test-api", { method: "POST" });
    const td = await t.json();
    if (td.ok) {
      $("keyOk").textContent = "已保存，连接测试成功：" + td.detail;
      $("setupApiKey").value = "";
      await setModeChoice("api");
      loadSetupStatus();
      loadModels(); loadConfig();
    } else {
      $("keyErr").textContent = "已保存，但连接测试未通过：" + td.detail;
      loadSetupStatus();
    }
  } catch (e) {
    $("keyErr").textContent = "保存失败：" + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "保存并测试连接";
  }
});

$("btnWebLogin").addEventListener("click", async () => {
  const btn = $("btnWebLogin");
  btn.disabled = true;
  btn.textContent = "登录引导中，请在弹出的 Edge 窗口完成…";
  $("webErr").textContent = "";
  $("webOk").textContent = "";
  try {
    const r = await fetch("/api/setup/web-login", { method: "POST" });
    if (!r.ok) throw new Error((await r.json()).detail || "启动失败");
    await setModeChoice("web");
    if (setupTimer) clearInterval(setupTimer);
    setupTimer = setInterval(pollSetupStatus, 2500);
  } catch (e) {
    $("webErr").textContent = e.message;
    btn.disabled = false;
    btn.textContent = "打开 Edge 登录 DeepSeek";
  }
});

$("btnZhihuLogin").addEventListener("click", async () => {
  const btn = $("btnZhihuLogin");
  btn.disabled = true;
  btn.textContent = "登录引导中，请在弹出的 Edge 窗口完成…";
  $("zhihuErr").textContent = "";
  $("zhihuOk").textContent = "";
  try {
    const r = await fetch("/api/setup/zhihu-login", { method: "POST" });
    if (!r.ok) throw new Error((await r.json()).detail || "启动失败");
    if (setupTimer) clearInterval(setupTimer);
    setupTimer = setInterval(pollSetupStatus, 2500);
  } catch (e) {
    $("zhihuErr").textContent = e.message;
    btn.disabled = false;
    btn.textContent = "打开 Edge 登录知乎";
  }
});

/* ---------- 任务运行 ---------- */

function connectSSE() {
  if (es) es.close();
  es = new EventSource("/api/events");
  es.addEventListener("stage", (e) => handleLogEvent(e, "stage"));
  es.addEventListener("result", (e) => handleLogEvent(e, "result"));
  es.addEventListener("error", (e) => handleLogEvent(e, "error"));
  es.addEventListener("log", (e) => handleLogEvent(e, ""));
  es.addEventListener("run_end", (e) => handleLogEvent(e, "run_end"));
  es.addEventListener("progress", (e) => {
    const p = JSON.parse(e.data);
    if (p.task) {
      // 阶段进度（文风提炼等）：pct 有值=定宽；无值=不确定（LLM 剖析中）
      applyTaskProgress(p);
      return;
    }
    $("progressWrap").classList.add("show");
    if (p.think) {
      // 思考阶段：无字数目标，用不确定动画 + 思考字符数
      $("progressFill").classList.add("indeterminate");
      $("progressText").textContent = `模型思考中… 已思考 ${p.chars} 字符`;
      return;
    }
    genChars = p.chars;
    const pct = Math.min(100, Math.round((genChars / 8000) * 100));
    $("progressFill").classList.remove("indeterminate");
    $("progressFill").style.width = pct + "%";
    $("progressText").textContent = `故事生成中… 已生成 ${genChars} 字（估算目标 8000）`;
    // 顺手从日志行里抓格式分
    const m = (p.text || "").match(/格式检测[：:]\s*(\d+)\s*\/\s*10/);
    if (m) formatScore = parseInt(m[1], 10);
  });
  es.addEventListener("state", (e) => {
    const s = JSON.parse(e.data);
    setState(s.state, s.message, s.guide_needed);
    if (["done", "error", "stopped", "timeout"].includes(s.state)) {
      genChars = 0;
      refreshStatus();
      loadStories();
      loadAuthors();  // 文风提炼完成后下拉即时更新
      loadProfileSources();  // 采集完成后来源下拉即时更新
    }
  });
  es.onopen = () => {
    addLog("已连接实时日志通道", "result");
    if (!window.__logHistoryLoaded) {
      window.__logHistoryLoaded = true;
      loadLogHistory();  // 页面刷新后回放最近日志（SSE 不补历史）
    }
  };
}

async function refreshStatus() {
  try {
    const r = await fetch("/api/status");
    const st = await r.json();
    if (st.context && (st.context.title || st.context.profile || st.context.collect)) {
      renderContext(st.context);
    }
    if (st.story && st.story.text) {
      const ctx = st.context || {};
      renderStory(st.story, ctx.title || null);
    }
  } catch (e) { /* ignore */ }
}

async function run() {
  const body = { mode: currentMode };
  if (currentMode === "batch") {
    body.gen_count = parseInt($("genCount").value, 10) || 5;
    body.publish_count = parseInt($("pubCount").value, 10) || 3;
  }
  if (currentMode === "clean") {
    body.rounds = parseInt($("cleanRounds").value, 10) || 1;
  }
  if (currentMode === "single") {
    body.rounds = parseInt($("classicRounds").value, 10) || 1;
  }
  $("logBox").innerHTML = "";
  genChars = 0;
  formatScore = null;
  try {
    const r = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      addLog("启动失败：" + (err.detail || r.status), "error");
      return;
    }
    connectSSE();
  } catch (e) {
    addLog("无法连接服务：" + e, "error");
  }
}

async function stop() {
  try {
    await fetch("/api/stop", { method: "POST" });
    addLog("已发送停止请求…", "error");
  } catch (e) { /* ignore */ }
}

/* ---------- 初始化 ---------- */

document.querySelectorAll("#modeGroup input[name=mode]").forEach((el) => {
  el.addEventListener("change", () => {
    currentMode = el.value;
    document.querySelectorAll(".mode-opt").forEach((o) =>
      o.classList.toggle("sel", o.querySelector("input").checked));
    $("batchParams").style.display = (currentMode === "batch") ? "" : "none";
    $("cleanParams").style.display = (currentMode === "clean") ? "" : "none";
    $("classicParams").style.display = (currentMode === "single") ? "" : "none";
  });
});

$("btnRun").onclick = run;
$("btnStop").onclick = stop;
$("modalClose").onclick = () => $("modalMask").classList.remove("show");
$("modalMask").addEventListener("click", (e) => {
  if (e.target === $("modalMask")) $("modalMask").classList.remove("show");
});

(async () => {
  renderLeftMode();
  $("leftModeSel").addEventListener("change", (e) => applyLeftMode(e.target.value));
  initTunables();
  loadConfig();
  loadModels();
  loadMode();
  loadBrowserMode();
  loadWebPreset();
  loadAuthors();
  loadProfileSources();
  // 已发布内容看板
  $("btnDashRefresh").addEventListener("click", refreshDashboard);
  $("btnDashRefresh2").addEventListener("click", refreshDashboard);
  $("btnDashClean").addEventListener("click", openClearModal);
  $("btnDashClean2").addEventListener("click", openClearModal);
  $("btnDashApply").addEventListener("click", () => {
    if (dashDebounce) clearTimeout(dashDebounce);
    loadDashboard();
  });
  $("btnDashReset").addEventListener("click", resetDashboard);
  ["dashQ", "dashStart", "dashEnd", "dashMinLikes", "dashMinReads", "dashMinComments", "dashSort"].forEach((id) => {
    const el = $(id);
    el.addEventListener("change", () => {
      if (id === "dashQ") dashState.q = el.value;
      else if (id === "dashStart") dashState.start = el.value;
      else if (id === "dashEnd") dashState.end = el.value;
      else if (id === "dashMinLikes") dashState.minLikes = parseInt(el.value || "0", 10) || 0;
      else if (id === "dashMinReads") dashState.minReads = parseInt(el.value || "0", 10) || 0;
      else if (id === "dashMinComments") dashState.minComments = parseInt(el.value || "0", 10) || 0;
      else if (id === "dashSort") { dashState.sort = el.value; dashState.direction = (SORT_META[el.value] || {}).dir || "desc"; }
      syncQuickSel();
      if (dashDebounce) clearTimeout(dashDebounce);
      dashDebounce = setTimeout(() => loadDashboard(), 400);
    });
  });
  $("dashQ").addEventListener("input", () => {
    dashState.q = $("dashQ").value;
    if (dashDebounce) clearTimeout(dashDebounce);
    dashDebounce = setTimeout(() => loadDashboard(), 600);
  });
  $("dashQ").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      if (dashDebounce) clearTimeout(dashDebounce);
      loadDashboard();
    }
  });
  document.querySelectorAll("#pane-dashboard .df-quick .chip-btn").forEach((b) =>
    b.addEventListener("click", () => applyQuick(b.dataset.from)));
  document.querySelectorAll("#chartTabs .chart-tab").forEach((b) =>
    b.addEventListener("click", () => switchDashTab(b.dataset.tab)));
  $("dashPrev").addEventListener("click", () => {
    if (dashState.page > 1) { dashState.page--; renderDashTable(dashData.rows || []); }
  });
  $("dashNext").addEventListener("click", () => {
    const pages = Math.max(1, Math.ceil((dashData.rows || []).length / dashState.pageSize));
    if (dashState.page < pages) { dashState.page++; renderDashTable(dashData.rows || []); }
  });
  $("dashPageSize").addEventListener("change", (e) => {
    dashState.pageSize = parseInt(e.target.value, 10) || 50;
    dashState.page = 1;
    renderDashTable(dashData.rows || []);
  });
  window.addEventListener("resize", () => {
    Object.values(ecCharts).forEach((c) => c && c.resize());
  });
  // 已发布内容·筛选待清理
  $("dashClearClose").addEventListener("click", () => $("dashClearMask").classList.remove("show"));
  $("dashClearMask").addEventListener("click", (e) => {
    if (e.target === $("dashClearMask")) $("dashClearMask").classList.remove("show");
  });
  $("clFilterBtn").addEventListener("click", runClFilter);
  $("clResetBtn").addEventListener("click", clReset);
  $("clAllBtn").addEventListener("click", () => setAll(true));
  $("clNoneBtn").addEventListener("click", () => setAll(false));
  $("clPruneBtn").addEventListener("click", clPrune);
  $("clDeleteZhihuBtn").addEventListener("click", clDeleteZhihu);
  // 草稿箱素材
  $("btnDraftsRefresh").addEventListener("click", refreshDrafts);
  $("btnDraftsRefresh2").addEventListener("click", refreshDrafts);
  $("btnDraftsApply").addEventListener("click", () => {
    if (draftDebounce) clearTimeout(draftDebounce);
    loadDrafts();
  });
  $("btnDraftsReset").addEventListener("click", resetDrafts);
  ["draftQ", "draftStart", "draftEnd", "draftMinChars", "draftMaxChars", "draftSort"].forEach((id) => {
    const el = $(id);
    el.addEventListener("change", () => {
      if (id === "draftQ") draftState.q = el.value;
      else if (id === "draftStart") draftState.start = el.value;
      else if (id === "draftEnd") draftState.end = el.value;
      else if (id === "draftMinChars") draftState.minChars = parseInt(el.value || "0", 10) || 0;
      else if (id === "draftMaxChars") draftState.maxChars = parseInt(el.value || "0", 10) || 0;
      else if (id === "draftSort") draftState.sort = el.value;
      syncDraftQuick();
      if (draftDebounce) clearTimeout(draftDebounce);
      draftDebounce = setTimeout(() => loadDrafts(), 400);
    });
  });
  $("draftQ").addEventListener("input", () => {
    draftState.q = $("draftQ").value;
    if (draftDebounce) clearTimeout(draftDebounce);
    draftDebounce = setTimeout(() => loadDrafts(), 600);
  });
  $("draftQ").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      if (draftDebounce) clearTimeout(draftDebounce);
      loadDrafts();
    }
  });
  document.querySelectorAll("#pane-drafts .df-quick .chip-btn").forEach((b) =>
    b.addEventListener("click", () => applyDraftQuick(b.dataset.from)));
  $("draftSelAll").addEventListener("change", (e) => {
    const on = e.target.checked;
    document.querySelectorAll("#draftList .cb").forEach((cb) => {
      cb.checked = on;
      const qid = cb.dataset.qid;
      if (on) draftSel.add(qid); else draftSel.delete(qid);
    });
    document.querySelectorAll("#draftList .dft-row").forEach((row) => row.classList.toggle("sel", on));
    updateDraftSelAll();
  });
  $("draftList").addEventListener("click", onDraftListClick);
  $("btnDraftsDelete").addEventListener("click", draftsDeleteSelected);
  $("draftViewClose").addEventListener("click", closeDraftView);
  $("draftViewMask").addEventListener("click", (e) => {
    if (e.target === $("draftViewMask")) closeDraftView();
  });
  loadStories();
  loadLogHistory().then(() => { window.__logHistoryLoaded = true; });  // 页面加载即回放最近日志；标记防首次运行重复回放
  fillSetupProviders();
  loadSetupStatus();  // 首启引导（未配置时弹出遮罩）
  try {
    const r = await fetch("/api/status");
    const st = await r.json();
    setState(st.state, st.message);
    if (st.state === "running") {
      connectSSE();
      if (st.progress) applyTaskProgress(st.progress);  // SSE 连接前的进度
    }
    else refreshStatus();  // 空闲时恢复上次运行的结果卡片
  } catch (e) {
    setState("error", "无法连接后端");
  }
})();

/* ---------- 意见反馈 ---------- */
(function initFeedback() {
  const mask = $("feedbackMask");
  if (!mask) return;  // 旧版 HTML 无此元素时静默跳过

  const cat = $("feedbackCat");
  const text = $("feedbackText");
  const ctx = $("feedbackContext");
  const msg = $("feedbackMsg");
  const submit = $("feedbackSubmit");

  function open() {
    msg.textContent = "";
    mask.classList.add("show");
    setTimeout(() => text.focus(), 30);
  }
  function close() { mask.classList.remove("show"); }

  $("btnFeedback").addEventListener("click", open);
  $("feedbackClose").addEventListener("click", close);
  mask.addEventListener("click", (e) => { if (e.target === mask) close(); });

  submit.addEventListener("click", async () => {
    const t = text.value.trim();
    if (!t) { msg.textContent = "请先填写问题描述"; text.focus(); return; }
    submit.disabled = true;
    msg.textContent = "提交中…";
    try {
      const r = await fetch("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: t,
          category: cat.value,
          context: ctx.value.trim(),
        }),
      });
      const d = await r.json();
      if (d && d.ok) {
        msg.textContent = "✓ 已记录，感谢反馈";
        text.value = "";
        ctx.value = "";
        setTimeout(close, 900);
      } else {
        msg.textContent = "提交失败：" + ((d && d.error) || "未知错误");
      }
    } catch (e) {
      msg.textContent = "提交失败：" + e.message;
    } finally {
      submit.disabled = false;
    }
  });
})();
