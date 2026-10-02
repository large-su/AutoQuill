"use strict";

// 本地历史与反馈的可视化；抓取仍沿用已有的内容看板入口。
window.AutoQuillEvolution = (() => {
  let data = null, view = "scheme", selected = null, graph = null, distribution = null;
  let requestNumber = 0;
  const el = (id) => document.getElementById(id);
  const h = (value) => String(value ?? "").replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const num = (value) => value == null ? "—" : Number(value).toLocaleString("zh-CN", {maximumFractionDigits: 1});
  const nodes = () => data?.nodes || [];
  const releases = () => data?.releases || [];
  const articleMetric = (article) => article.horizon_observation?.metrics || article.horizon_observation || {};
  const nodeLabel = (node) => node.source === "history"
    ? (node.releases?.length > 1 ? node.releases[0] + " ～ " + node.releases.at(-1) : node.label)
    : node.label;
  const selectedScheme = () => view === "scheme" ? nodes().find((n) => n.id === selected)
    : nodes().find((n) => n.id === releases().find((r) => r.tag === selected)?.node_id);
  function publicLink(url) {
    try {
      const parsed = new URL(url);
      return parsed.protocol === "https:" && /(^|\.)zhihu\.com$/.test(parsed.hostname) ? parsed.href : "";
    } catch (_) { return ""; }
  }
  const releaseLink = (tag) => "https://github.com/large-su/AutoQuill/releases/tag/" + encodeURIComponent(tag);

  async function load() {
    const number = ++requestNumber;
    el("evoStatus").textContent = "正在读取本地版本与反馈记录…";
    el("evoStatus").classList.remove("evo-error");
    try {
      const response = await fetch("/api/evolution?horizon=" + el("evoHorizon").value);
      if (!response.ok) throw new Error("读取失败（HTTP " + response.status + "）");
      const result = await response.json();
      if (number !== requestNumber) return;
      data = result;
      const available = view === "scheme" ? nodes().map((n) => n.id) : releases().map((r) => r.tag);
      if (!available.includes(selected)) selected = available.at(-1) || null;
      render();
    } catch (error) {
      if (number !== requestNumber) return;
      el("evoStatus").textContent = "读取未完成：" + error.message + "。可点击刷新重试。";
      el("evoStatus").classList.add("evo-error");
    }
  }

  function render() {
    const articles = data.articles || [];
    const publicCount = articles.filter((a) => a.observations?.length).length;
    const observedDates = articles.map((a) => a.latest?.observed_at).filter(Boolean).sort();
    el("evoStatus").textContent = releases().length + " 个近期版本 · " + nodes().length + " 个方案阶段 · "
      + publicCount + " 篇公开作品 · 当前 v" + data.current_version
      + (observedDates.length ? " · 本地观测更新至 " + observedDates.at(-1).slice(0, 10) : "");
    const warnings = data.warnings || [];
    el("evoWarnings").hidden = !warnings.length;
    el("evoWarnings").textContent = warnings.join("；");
    el("evoWindowNote").textContent = "对齐文章发布后的年龄：采用第 " + data.horizon + "～"
      + (data.horizon + 3) + " 天内最接近目标日的一次实际观测。错过窗口的历史数据保留展示，不反推当时成绩。";
    renderGraph(); renderDetail(); renderArticles();
  }

  function renderGraph() {
    const items = view === "scheme" ? nodes() : releases();
    const key = (item) => view === "scheme" ? item.id : item.tag;
    el("evoNodeList").innerHTML = items.map((item) => '<button class="btn ghost '
      + (key(item) === selected ? "sel" : "") + '" data-evo-node="' + h(key(item)) + '">'
      + h(view === "scheme" ? nodeLabel(item) : item.tag) + "</button>").join("");
    if (!window.echarts) {
      el("evoGraph").textContent = "图表库暂不可用，可通过下方按钮查看节点。";
      return;
    }
    if (!graph) {
      graph = echarts.init(el("evoGraph"));
      graph.on("click", (params) => {
        if (params.data?.reference) selectNode(params.data.reference);
      });
    }
    const columns = Math.min(4, Math.max(1, items.length));
    const chartNodes = items.map((item, index) => {
      const row = Math.floor(index / columns), offset = index % columns;
      const column = row % 2 ? columns - 1 - offset : offset;
      const scheme = view === "scheme" ? item : nodes().find((n) => n.id === item.node_id);
      const active = key(item) === selected;
      const color = scheme?.source === "runtime" ? "#34d399" : "#818cf8";
      const name = view === "scheme" ? nodeLabel(item) : item.tag;
      const m = scheme?.metrics || {};
      const subtitle = view === "release" ? ({engineering: "工程改动", writing: "写作改动", mixed: "混合改动"}[item.change_kind] || "改动记录")
        : m.observed ? "中位数 " + num(m.median) + " · " + m.observed + " 篇观测"
          : m.assigned ? "已有作品 · 等待阶段观测" : "尚无已确认样本";
      return {id: key(item), reference: key(item), name, subtitle,
        x: column * 220, y: row * 115, symbol: "roundRect", symbolSize: [148, 54],
        itemStyle: {color: active ? "#283247" : "#182131", borderColor: color, borderWidth: active ? 3 : 1},
        label: {show: true, color: "#dbe3f0", fontSize: 11, formatter: () => name + "\n" + subtitle, lineHeight: 20}};
    });
    const graphEdges = view === "scheme" ? (data.edges || [])
      : items.slice(1).map((item, index) => ({source: items[index].tag, target: item.tag}));
    const valid = new Set(chartNodes.map((n) => n.id));
    graph.setOption({animation: false,
      tooltip: {formatter: (params) => params.data?.reference ? h(params.data.name) + "<br>" + h(params.data.subtitle) : "方案或版本的改动顺序"},
      series: [{type: "graph", layout: "none", roam: true,
        edgeSymbol: ["none", "arrow"], edgeSymbolSize: 9,
        lineStyle: {color: "#a2b3d3", width: 2, opacity: 1},
        data: chartNodes, links: graphEdges.filter((edge) => valid.has(edge.source) && valid.has(edge.target))}]
    }, true);
    graph.resize();
  }

  function selectNode(id) {
    selected = id;
    renderGraph(); renderDetail(); renderArticles();
  }

  function renderDetail() {
    const node = selectedScheme();
    if (!node) { el("evoDetail").innerHTML = '<p class="evo-muted">尚无节点记录。</p>'; return; }
    const release = view === "release" ? releases().find((r) => r.tag === selected) : null;
    const m = node.metrics || {};
    const metric = (label, value) => "<div><dt>" + label + "</dt><dd>" + num(value) + "</dd></div>";
    let html = "<h3>" + h(release ? release.tag : nodeLabel(node)) + "</h3>";
    html += '<p class="evo-muted">' + h(release?.date || node.start_date || "") + "</p>";
    if (release) {
      html += "<p>" + h(Array.isArray(release.summary) ? release.summary.join("；") : release.summary || "版本记录") + "</p>";
      html += '<p class="evo-muted">属于 ' + h(nodeLabel(node)) + "。下面是这个方案阶段的作品反馈。</p>";
    } else html += "<p>" + h(node.description || "同一写作方案连续使用的阶段") + "</p>";
    html += "<dl>" + metric("赞同中位数", m.median) + metric("P25 · 较低四分位", m.p25)
      + metric("篇均赞同", m.mean) + metric("有效阶段观测", m.observed || 0) + "</dl>";
    const share = m.top_share == null ? "—" : (m.top_share * 100).toFixed(1) + "%";
    html += '<p class="evo-muted">已关联 ' + (m.assigned || 0) + " 篇 · 已到观察日 " + (m.mature || 0)
      + " 篇<br>最大单篇占比 " + share + "；用于查看成绩是否集中在一篇作品。</p>";
    html += '<p class="evo-note">' + h(m.observed ? (m.observed < 5 ? "样本较少，保留观察结果，暂不足以判断稳定改善。" : "优先结合中位数、P25 与样本分布判断；这些结果尚不能证明改动的因果效果。")
      : m.assigned ? "尚未获得这个阶段的有效观测，暂不评分。" : "尚无已确认归属的公开作品，暂不评分。") + "</p>";
    html += '<p class="evo-muted">包含版本</p>' + (node.releases || []).map((tag) => '<a class="evo-release-link" href="'
      + h(releaseLink(tag)) + '" target="_blank" rel="noopener noreferrer">' + h(tag) + " ↗</a>").join("");
    if (node.source === "history") html += '<p class="evo-muted">历史节点按写作代码归并；当时的模型、文风和运行配置尚未完整记录。</p>';
    if (node.settings) {
      const labels = {LLM_MODE: "生成通道", LLM_PROVIDER: "模型提供商", LLM_MODEL_ID: "API 模型",
        WEB_LLM_DRIVER: "网页模型", AUTHOR_PROFILE: "作者风格", STORY_MATERIAL_MODE: "素材方式",
        QUESTION_SELECT_MODE: "选题方式", QUESTION_SOURCE: "选题来源", LLM_API_TEMPERATURE: "生成温度"};
      html += '<details><summary>记录的方案设置</summary>' + Object.entries(node.settings)
        .filter(([key, value]) => labels[key] && value != null)
        .map(([key, value]) => '<p class="evo-muted">' + h(labels[key]) + "：" + h(value) + "</p>").join("") + "</details>";
    }
    if (release) html += '<details><summary>改动来源与文件</summary><p>' + h(release.commit || "待发布") + "</p><ul>"
      + (release.changed_files || []).slice(0, 30).map((file) => "<li>" + h(file) + "</li>").join("") + "</ul></details>";
    el("evoDetail").innerHTML = html;
  }

  function renderArticles() {
    const node = selectedScheme();
    const all = [...(data.articles || [])].sort((a, b) => (b.publish_date || "").localeCompare(a.publish_date || ""));
    const articles = el("evoOnlySelected").checked ? all.filter((a) => a.node_id === node?.id) : all;
    if (!articles.length) {
      el("evoArticles").innerHTML = '<p class="evo-muted">' + (all.length ? "所选方案尚无已关联作品。取消筛选后，可从已有作品中确认归属。"
        : "尚无公开作品快照。可先到已发布内容看板刷新，再回到此页查看。") + "</p>";
    } else {
      el("evoArticles").innerHTML = '<table class="evo-table"><thead><tr><th>作品 / 发布日期</th><th>方案归属</th>'
        + '<th class="evo-num">当前赞同</th><th class="evo-num">阶段赞同</th><th>观测状态</th></tr></thead><tbody>'
        + articles.map((article) => {
          const href = publicLink(article.url);
          const title = href ? '<a class="evo-title" href="' + h(href) + '" target="_blank" rel="noopener noreferrer">' + h(article.title || article.aid) + " ↗</a>"
            : '<span class="evo-title">' + h(article.title || article.aid) + "</span>";
          const chosen = article.node_id || article.suggested_node_id || "";
          const options = '<option value="">暂不关联</option>' + nodes().map((n) => '<option value="' + h(n.id) + '"'
            + (chosen === n.id ? " selected" : "") + ">" + h(nodeLabel(n)) + "</option>").join("");
          const isPublic = !!article.observations?.length;
          const state = !isPublic ? "发布回执 · 待公开确认" : article.node_id ? "已确认归属"
            : article.suggested_node_id ? "归属建议 · 待确认" : "待关联";
          const observation = article.horizon_observation;
          const hasValue = articleMetric(article).likes != null;
          const status = !isPublic ? "等待公开内容快照" : !article.node_id ? "确认归属后参与评价" : !article.publish_date ? "缺少发布日期"
            : observation && hasValue ? "第 " + num(observation.age_days) + " 天观测"
              : article.age_days != null && article.age_days < data.horizon ? "尚未到观察日" : "缺少窗口内有效观测";
          return "<tr><td>" + title + '<span class="evo-muted">' + h(article.publish_date || "发布日期未知") + "</span></td><td>"
            + '<div class="evo-assignment-box"><select class="evo-assignment" data-evo-aid="' + h(article.aid) + '" aria-label="作品所属方案">'
            + options + '</select><button class="btn ghost" data-evo-save="' + h(article.aid) + '"'
            + (isPublic ? "" : " disabled") + '>确认归属</button></div>'
            + '<span class="evo-state ' + (article.node_id ? "confirmed" : "") + '">' + state + "</span></td>"
            + '<td class="evo-num">' + num((article.latest?.metrics || article.latest || {}).likes) + "</td>"
            + '<td class="evo-num">' + num(articleMetric(article).likes) + "</td><td>" + h(status) + "</td></tr>";
        }).join("") + "</tbody></table>";
    }
    renderDistribution(node, all);
  }

  function renderDistribution(node, articles) {
    const samples = articles.filter((a) => a.node_id === node?.id && articleMetric(a).likes != null)
      .sort((a, b) => articleMetric(a).likes - articleMetric(b).likes);
    el("evoDistribution").hidden = !samples.length;
    if (!samples.length || !window.echarts) return;
    if (!distribution) distribution = echarts.init(el("evoDistribution"));
    distribution.setOption({animation: false,
      title: {text: "所选方案 · 每篇作品的阶段赞同（由低到高）", textStyle: {color: "#dbe3f0", fontSize: 12}},
      grid: {left: 50, right: 20, top: 42, bottom: 32},
      tooltip: {trigger: "item", formatter: (params) => h(samples[params.dataIndex].title || "作品") + "<br>阶段赞同：" + num(params.value)},
      xAxis: {type: "category", data: samples.map((_, i) => i + 1), name: "作品", axisLabel: {color: "#7d8aa3"}},
      yAxis: {type: "value", name: "赞同", axisLabel: {color: "#7d8aa3"}, splitLine: {lineStyle: {color: "#232b3d"}}},
      series: [{type: "bar", data: samples.map((a) => articleMetric(a).likes), itemStyle: {color: "#818cf8"},
        markLine: {symbol: "none", lineStyle: {color: "#34d399"}, label: {color: "#34d399"},
          data: node?.metrics?.median == null ? [] : [{yAxis: node.metrics.median, name: "中位数", label: {formatter: "中位数"}}]}}]
    }, true);
    distribution.resize();
  }

  async function saveAssignment(aid, button) {
    const select = [...el("evoArticles").querySelectorAll("select[data-evo-aid]")].find((s) => s.dataset.evoAid === aid);
    if (!select) return;
    button.disabled = true;
    button.textContent = "保存中…";
    try {
      const response = await fetch("/api/evolution/assign", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({aid, node_id: select.value || null})});
      if (!response.ok) {
        const result = await response.json();
        throw new Error(result.detail || "保存失败");
      }
      await load();
    } catch (error) {
      el("evoStatus").textContent = "归属未保存：" + error.message;
      el("evoStatus").classList.add("evo-error");
      button.disabled = false;
      button.textContent = "确认归属";
    }
  }

  el("evoRefresh").addEventListener("click", load);
  el("evoHorizon").addEventListener("change", load);
  el("evoReset").addEventListener("click", () => { if (data) renderGraph(); });
  el("evoOnlySelected").addEventListener("change", () => { if (data) renderArticles(); });
  el("evoViewSwitch").addEventListener("click", (event) => {
    const button = event.target.closest("[data-evo-view]");
    if (!button) return;
    view = button.dataset.evoView;
    el("evoViewSwitch").querySelectorAll("button").forEach((b) => {
      b.classList.toggle("sel", b === button); b.setAttribute("aria-selected", String(b === button));
    });
    selected = null;
    if (data) { selected = (view === "scheme" ? nodes().at(-1)?.id : releases().at(-1)?.tag) || null; render(); }
  });
  el("evoNodeList").addEventListener("click", (event) => {
    const button = event.target.closest("[data-evo-node]");
    if (button) selectNode(button.dataset.evoNode);
  });
  el("evoArticles").addEventListener("click", (event) => {
    const button = event.target.closest("[data-evo-save]");
    if (button) saveAssignment(button.dataset.evoSave, button);
  });
  const resize = () => { if (!el("pane-evolution").hidden) { graph?.resize(); distribution?.resize(); } };
  if (window.ResizeObserver) new ResizeObserver(resize).observe(el("evoGraph"));
  window.addEventListener("resize", resize);
  return {load};
})();
