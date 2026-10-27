"use strict";

// 版本时间窗和真实反馈的本地可视化。网络抓取只由用户主动点击触发。
window.AutoQuillEvolution = (() => {
  let data = null, view = "release", selected = null, distribution = null, requestNumber = 0;
  const metricNames = {likes: "赞同", reads: "阅读", comments: "评论", collects: "收藏", favors: "喜欢"};
  const reasons = {daily_release_window: "按发布日推定", time_window: "按发布时间推定", boundary_ambiguous: "处于版本边界", before_history: "早于可用版本历史", missing_publish_time: "缺少发布时间", missing_public_snapshot: "缺少公开快照", verified_chain: "证据已确认", manual: "人工确认", manual_clear: "人工清除", missing_content_evidence: "缺少正文一致性证据", missing_generation: "缺少生成记录", ambiguous_generation: "生成记录有歧义", conflicting_receipt: "发布回执冲突"};
  const el = (id) => document.getElementById(id);
  const h = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const num = (v) => v == null ? "—" : Number(v).toLocaleString("zh-CN", {maximumFractionDigits: 1});
  const key = () => data?.metric || el("evoMetric").value || "likes";
  const name = () => metricNames[key()] || "赞同";
  const releases = () => data?.releases || [], nodes = () => data?.nodes || [];
  const metric = (a) => a.horizon_observation?.metrics || a.horizon_observation || {};
  const selectedRelease = () => releases().find((r) => r.tag === selected);
  const selectedNode = () => nodes().find((n) => n.id === selected);
  const item = () => view === "release" ? selectedRelease() : selectedNode();
  const currentMetrics = () => view === "release" ? (selectedRelease()?.time_metrics || {}) : (selectedNode()?.metrics || {});
  const releaseDate = (r) => r.released_at || r.date || "时间未知";
  const sourceLabel = (source) => ({"github-release":"GitHub Release", "git-tag":"Git 标签时间（估计）", "git-commit":"Git 提交时间（估计）", "git-date-estimate":"Git 日期（时间未知）", "synthetic-ui":"合成测试时间", "build-time-estimate":"构建时间（估计）", "working-tree":"本地待发布"}[source] || source || "本地 Git 历史");
  const versionOf = (a) => a.attributed_version || a.time_attribution?.version || "";
  const belongs = (a) => view === "release"
    ? (selectedRelease()?.time_article_ids || []).includes(a.aid)
    : a.node_id === selected;
  const publicLink = (url) => { try { const p = new URL(url); return p.protocol === "https:" && /(^|\.)zhihu\.com$/.test(p.hostname) ? p.href : ""; } catch (_) { return ""; } };

  async function load() {
    const request = ++requestNumber;
    el("evoStatus").textContent = "正在读取本地版本、时间窗和反馈记录…";
    el("evoStatus").classList.remove("evo-error");
    try {
      const q = new URLSearchParams({horizon: el("evoHorizon").value, metric: el("evoMetric").value, since: el("evoSince").value, until: el("evoUntil").value, attribution_basis: "publication"});
      const response = await fetch("/api/evolution/feedback?" + q);
      if (!response.ok) throw new Error("读取失败（HTTP " + response.status + "）");
      const result = await response.json();
      if (request !== requestNumber) return false;
      data = result;
      const ids = (view === "release" ? releases() : nodes()).map((x) => view === "release" ? x.tag : x.id);
      if (!ids.includes(selected)) selected = ids.at(-1) || null;
      render(); return true;
    } catch (error) { if (request === requestNumber) { el("evoStatus").textContent = "读取未完成：" + error.message + "。可点击刷新本地记录重试。"; el("evoStatus").classList.add("evo-error"); } return false; }
  }

  function render() {
    const quality = data.quality || {}, records = data.articles || [];
    el("evoStatus").textContent = "当前 v" + data.current_version + " · " + releases().length + " 个每日版本节点 · " + num(quality.public_articles || records.filter((a) => a.observations?.length).length) + " 篇公开作品 · 按发布日推定 " + num(quality.time_attributed || 0) + " 篇 · 方案已确认 " + num(quality.confirmed || 0) + " 篇";
    const warnings = [...(data.warnings || [])];
    if (quality.time_ambiguous) warnings.push("版本边界未归属 " + num(quality.time_ambiguous) + " 篇");
    if (quality.time_unassigned) warnings.push("未能按时间窗归属 " + num(quality.time_unassigned) + " 篇");
    if (quality.unavailable?.followers) warnings.push("账号关注数不可用：" + quality.unavailable.followers);
    if (quality.unavailable?.per_run_hour) warnings.push("每小时回报不可用：" + quality.unavailable.per_run_hour);
    el("evoWarnings").hidden = !warnings.length; el("evoWarnings").textContent = warnings.join("；");
    el("evoWindowNote").textContent = "阶段成绩使用发布后第 " + data.horizon + "～" + (data.horizon + 3) + " 天内最接近目标日的一次实际观测。没有窗口内观测时保持缺失，不反推历史成绩。";
    const sync = data.release_sync || {};
    el("evoSyncStatus").textContent = sync.synced_at ? "版本时间最近同步：" + String(sync.synced_at).replace("T", " ").slice(0, 16) + " · " + num(sync.count) + " 个版本" : "版本时间来自随包历史；可同步 GitHub 发布时间";
    feedback(); timeline(); detail(); articles();
  }

  function feedback() {
    const refreshes = data.refreshes || {}, good = refreshes.last_success, latest = refreshes.latest, names = {done:"完成", error:"失败", running:"进行中", stopped:"已停止", interrupted:"上次中断"};
    el("evoFeedbackStatus").textContent = good ? "最近成功采集：" + String(good.observed_at || "时间未知").replace("T", " ").slice(0, 19) + " · " + num(good.count) + " 篇；从知乎更新只会新增当前观测，不改写过往成绩。" : latest ? "最新采集尝试：" + (names[latest.status] || latest.status) + " · " + (latest.message || "尚无成功观测") : "尚无每日采集节点；可先从知乎更新反馈，或浏览已保存的历史快照。";
    if (good && latest && latest.id !== good.id) {
      el("evoFeedbackStatus").textContent += " 最新尝试：" + (names[latest.status] || latest.status) + " · " + (latest.message || "");
    }
    const q = data.quality || {};
    el("evoFeedbackStatus").textContent += " 最新观测：" + (q.latest_observed_at || "暂无") + "；距今 " + num(q.stale_days) + " 天。";
    el("evoRefreshNodes").innerHTML = (refreshes.history || []).map((x) => "<p>" + h(String(x.observed_at || x.finished_at || x.started_at || "").replace("T", " ").slice(0,19)) + " · " + (x.source === "automation" ? "每日自动采集" : "手动采集") + " · " + h(names[x.status] || x.status) + " · " + (x.status === "done" ? num(x.count) + " 篇" : h(x.message || "")) + "</p>").join("") || "暂无采集记录。";
  }

  function timeline() {
    const values = view === "release" ? [...releases()].reverse() : [...nodes()].reverse();
    el("evoListTitle").textContent = view === "release" ? "每日版本节点" : "写作方案阶段";
    el("evoListHint").textContent = view === "release" ? "最小评价间隔为自然日；同日发布合并到当天最后版本，作品按发布日归属。" : "方案视图只展示有明确生成／人工归属的证据链。";
    el("evoNodeList").innerHTML = values.map((x) => {
      const id = view === "release" ? x.tag : x.id;
      const m = view === "release" ? (x.time_metrics || {}) : (x.metrics || {});
      const count = view === "release" ? (x.time_article_ids || []).length : (x.article_ids || []).length;
      const when = view === "release" ? String(x.evaluation_date || releaseDate(x)).replace("T", " ").slice(0, 16)
        : (x.source === "runtime" ? "已记录试用" : "历史代码");
      const sampleLabel = view === "release" ? num(x.release_count || 1) + " 版合并 · " + num(count) + " 篇归属" : num(count) + " 篇证据归属";
      return '<button class="evo-time-item ' + (id === selected ? "sel" : "") + '" data-evo-node="' + h(id) + '"><span class="evo-time-dot"></span><span class="evo-time-main"><b>' + h(view === "release" ? x.tag : (x.label || x.id)) + '</b><small>' + h(when) + '</small></span><span class="evo-time-score">' + h(m.median == null ? "等待样本" : name() + "中位数 " + num(m.median)) + '<small>' + sampleLabel + '</small></span></button>';
    }).join("") || '<p class="evo-muted">尚无可展示的版本或方案记录。</p>';
  }

  function detail() {
    const x = item(); if (!x) { el("evoDetail").innerHTML = '<p class="evo-muted">尚无可查看的记录。</p>'; return; }
    const m = currentMetrics(), release = selectedRelease(), ids = view === "release" ? (release.time_article_ids || []) : (x.article_ids || []);
    const timing = view === "release" ? '<p class="evo-detail-time">评价日：' + h(x.evaluation_date || "未知") + ' · 当天最后发布：' + h(String(releaseDate(x)).replace("T"," ").slice(0,19)) + ' · ' + h(sourceLabel(x.release_time_source)) + '<br>评价区间：' + h(String(x.interval_start || "未知").replace("T"," ").slice(0,19)) + " 至 " + h(String(x.interval_end || "暂无后续发布日").replace("T"," ").slice(0,19)) + "</p>" : "";
    const link = view === "release" ? '<a class="evo-release-link" target="_blank" rel="noopener" href="https://github.com/large-su/AutoQuill/releases/tag/' + encodeURIComponent(x.tag) + '">查看 GitHub Release ↗</a>' : "";
    const included = (x.included_releases || []).map((release) => '<li><a target="_blank" rel="noopener" href="https://github.com/large-su/AutoQuill/releases/tag/' + encodeURIComponent(release.tag) + '">' + h(release.tag || release.version) + ' ↗</a> · ' + h(String(release.released_at || "").replace("T", " ").slice(0,19)) + ' · ' + h(release.summary || "无摘要") + '</li>').join("");
    const releaseDetails = view === "release" ? '<details><summary>当天合并的 ' + num(x.release_count || 1) + ' 次发布</summary><ul>' + (included || '<li>仅保留当天最后版本记录。</li>') + '</ul></details>' : "";
    el("evoDetail").innerHTML = '<div class="evo-detail-kicker">' + (view === "release" ? "按发布日推定" : "证据链归属") + '</div><h3>' + h(view === "release" ? x.tag : (x.label || x.id)) + '</h3>' + timing + '<p class="evo-muted">' + h(view === 'release' && x.release_count > 1 ? '同日 ' + x.release_count + ' 个版本的全部改动已合并，可展开下方记录查看。' : (x.summary || x.summaries?.[0] || '暂无改动摘要')) + '</p><dl><div><dt>' + name() + '中位数</dt><dd>' + num(m.median) + '</dd></div><div><dt>P25 · 较低四分位</dt><dd>' + num(m.p25) + '</dd></div><div><dt>阶段有效观测</dt><dd>' + num(m.observed) + '</dd></div><div><dt>归属作品</dt><dd>' + num(ids.length) + '</dd></div></dl>' + link + releaseDetails + '<details><summary>数据口径与缺失</summary><p>最小评价间隔为自然日：同日所有版本并入当天最后发布的版本节点，作品按发布日归属。已归属 ' + num(m.assigned ?? ids.length) + ' 篇；有效观测 ' + num(m.observed) + ' 篇；尚未成熟 ' + num(m.immature) + ' 篇；缺少阶段指标 ' + num(m.missing) + ' 篇。篇均 ' + num(m.mean) + '，最高篇占比 ' + (m.top_share == null ? '—' : num(m.top_share * 100) + '%') + '。方案视图保留生成证据与人工方案归属。缺失指标与未成熟作品不按零处理。</p></details>';
  }

  function articles() {
    const all = data.articles || [], values = el("evoOnlySelected").checked ? all.filter(belongs) : all, options = ['<option value="">未确认方案</option>'].concat(nodes().map((n) => '<option value="' + h(n.id) + '">' + h(n.label || n.id) + '</option>'));
    if (!values.length) { el("evoArticles").innerHTML = '<p class="evo-muted">当前筛选没有作品。可取消“只看所选版本或方案”查看全部记录。</p>'; chart([]); return; }
    el("evoArticles").innerHTML = '<table class="evo-table"><thead><tr><th>作品 / 发布时间</th><th>版本归属</th><th class="evo-num">当前' + name() + '</th><th class="evo-num">阶段' + name() + '</th><th>观测状态</th><th>方案证据</th></tr></thead><tbody>' + values.map((a) => { const title = publicLink(a.url) ? '<a class="evo-title" href="' + h(publicLink(a.url)) + '" target="_blank" rel="noopener">' + h(a.title || "未命名作品") + " ↗</a>" : '<span class="evo-title">' + h(a.title || "未命名作品") + "</span>"; const time = a.time_attribution || {}, version = versionOf(a), status = a.horizon_observation && metric(a)[key()] != null ? "第 " + num(a.horizon_observation.age_days) + " 天实际观测" : a.age_days != null && a.age_days < data.horizon ? "尚未到观察日" : "缺少窗口内有效观测"; const inference = (reasons[time.reason] || reasons[a.attribution_reason] || "待核对") + (time.precision ? " · " + ({day:"日", minute:"分钟", second:"秒"}[time.precision] || time.precision) + "精度" : "") + (time.source ? " · " + sourceLabel(time.source) : ""); const evidence = reasons[a.attribution_reason] || "生成证据待确认"; return '<tr><td>' + title + '<span class="evo-muted">' + h(a.publish_time || a.publish_date || "发布时间未知") + (a.generation_version ? " · 生成记录 v" + h(String(a.generation_version).replace(/^v/, "")) : "") + '</span></td><td><span class="evo-state ' + (version ? "confirmed" : "") + '">' + h(version || reasons[time.reason] || reasons[a.attribution_reason] || "待核对") + '</span><small class="evo-muted">' + h(inference) + '</small></td><td class="evo-num">' + num((a.latest?.metrics || a.latest || {})[key()]) + '</td><td class="evo-num">' + num(metric(a)[key()]) + '</td><td>' + h(status) + '</td><td><div class="evo-assignment-box"><select class="evo-assignment" data-evo-aid="' + h(a.aid) + '">' + options.join("") + '</select><button class="btn ghost" data-evo-save="' + h(a.aid) + '">确认</button></div><small class="evo-muted">' + h(evidence) + '</small></td></tr>'; }).join("") + "</tbody></table>";
    values.forEach((a) => { const s = [...el("evoArticles").querySelectorAll("select[data-evo-aid]")].find((x) => x.dataset.evoAid === String(a.aid)); if (s) s.value = a.node_id || ""; }); chart(values);
  }

  function chart(values) { const samples = values.filter((a) => belongs(a) && metric(a)[key()] != null).sort((a,b) => metric(a)[key()] - metric(b)[key()]); el("evoDistribution").hidden = !samples.length; if (!samples.length || !window.echarts) return; if (!distribution) distribution = echarts.init(el("evoDistribution")); distribution.setOption({animation:false,title:{text:"所选" + (view === "release" ? "版本" : "方案") + " · 每篇作品阶段" + name(),textStyle:{color:"#dbe3f0",fontSize:12}},grid:{left:48,right:18,top:42,bottom:30},xAxis:{type:"category",data:samples.map((_,i)=>i+1),axisLabel:{color:"#7d8aa3"}},yAxis:{type:"value",axisLabel:{color:"#7d8aa3"},splitLine:{lineStyle:{color:"#232b3d"}}},series:[{type:"bar",data:samples.map((a)=>metric(a)[key()]),itemStyle:{color:"#818cf8"}}]},true); distribution.resize(); }
  async function save(aid, button) { const select = [...el("evoArticles").querySelectorAll("select[data-evo-aid]")].find((x)=>x.dataset.evoAid===aid); if (!select) return; button.disabled=true; button.textContent="保存中…"; try { const r=await fetch("/api/evolution/assign",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({aid,node_id:select.value||null})}), result=await r.json(); if (!r.ok) throw new Error(result.detail||"保存失败"); await load(); } catch(error) { el("evoStatus").textContent="方案归属未保存："+error.message; el("evoStatus").classList.add("evo-error"); button.disabled=false; button.textContent="确认"; } }
  el("evoRefresh").addEventListener("click",load); ["evoHorizon","evoMetric","evoSince","evoUntil"].forEach((id)=>el(id).addEventListener("change",load)); el("evoOnlySelected").addEventListener("change",()=>data&&articles());
  el("evoReleaseRefresh").addEventListener("click",async()=>{const b=el("evoReleaseRefresh"); b.disabled=true;b.textContent="同步中…";try{const r=await fetch("/api/evolution/releases/refresh",{method:"POST"}),v=await r.json();if(!r.ok||!v.ok)throw new Error(v.message||"同步未完成");await load();}catch(e){el("evoSyncStatus").textContent="版本时间同步未完成："+e.message;}finally{b.disabled=false;b.textContent="同步版本时间";}});
  el("evoFeedbackRefresh").addEventListener("click",async()=>{const b=el("evoFeedbackRefresh");b.disabled=true;try{const r=await fetch("/api/dashboard/refresh",{method:"POST"}),v=await r.json();if(!r.ok||!v.ok)throw new Error(v.message||"刷新未启动");el("evoFeedbackStatus").textContent="正在从知乎采集反馈，可离开此页；完成后会保存新的观测节点。";while(true){await new Promise((ok)=>setTimeout(ok,3000));const s=await(await fetch("/api/dashboard/refresh/status")).json();if(s.status==="running"){el("evoFeedbackStatus").textContent=s.progress||"正在采集…";continue;}if(!await load()||s.status!=="done")throw new Error(s.error||"本次采集未完成，已有观测仍保留");break;}}catch(e){el("evoFeedbackStatus").textContent="反馈采集未完成："+e.message;}finally{b.disabled=false;}});
  el("evoViewSwitch").addEventListener("click",(e)=>{const b=e.target.closest("[data-evo-view]");if(!b)return;view=b.dataset.evoView;el("evoViewSwitch").querySelectorAll("button").forEach((x)=>{x.classList.toggle("sel",x===b);x.setAttribute("aria-selected",String(x===b));});el("evoOnlySelected").parentElement.lastChild.textContent="只看所选"+(view==="release"?"版本":"方案");selected=null;if(data){selected=(view==="release"?releases().at(-1)?.tag:nodes().at(-1)?.id)||null;render();}}); el("evoNodeList").addEventListener("click",(e)=>{const b=e.target.closest("[data-evo-node]");if(b){selected=b.dataset.evoNode;timeline();detail();articles();}}); el("evoArticles").addEventListener("click",(e)=>{const b=e.target.closest("[data-evo-save]");if(b)save(b.dataset.evoSave,b);}); window.addEventListener("resize",()=>{if(!el("pane-evolution").hidden)distribution?.resize();}); return {load};
})();
