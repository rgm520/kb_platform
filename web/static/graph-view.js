/* 知识图谱（Obsidian 式）：活体力仿真 + 右侧控制面板。
 *
 * 核心认知（2026-10-26，用户参照 Obsidian 图谱视图）：
 *   1. **节点位置是活的** —— 一直有斥力/弹簧在推，节点缓慢漂移、簇会呼吸，
 *      不是"布局一次就定死"。cytoscape 的 cose 是一次性布局，做不到这个，
 *      所以这里自己跑一个逐帧的力仿真（斥力 + 弹簧 + 向心 + 阻尼）。
 *   2. **图要大** —— 一屏上千个点，而不是几十个；点小、线细、大部分不写字。
 *   3. **右侧有控制面板** —— 筛选 / 分组 / 显示（节点大小、连线粗细、
 *      文字淡出阈值、箭头），像 Obsidian 那样现场调。
 *
 * 性能护栏：
 *   * 斥力用空间网格加速（不是 O(n²)），1200 个节点也能跑；
 *   * 每帧位移上限很小 → 缓慢平滑，不会抖；
 *   * 页面不可见时停；系统设置"减少动态效果"时只做静态布局。
 */
window.KB = window.KB || {};
(function () {
  const $ = (s, r) => (r || document).querySelector(s);

  const CFG = {
    // 显示参数（右侧面板可调）
    nodeScale: 1.0,       // 节点大小倍率
    linkWidth: 0.7,       // 连线粗细
    textFade: 0.55,       // 文字淡出阈值：缩放低于它就不画字
    showArrows: false,    // 是否显示箭头
    showLabels: false,    // 是否总是显示文字
    // 物理参数
    repulsion: 5200,      // 斥力强度
    maxForce: 400,       // 单对节点的斥力上限（防 1/d² 爆掉）
    maxSpeed: 40,        // 速度上限
    spring: 0.010,        // 弹簧强度（边把两端拉近）
    idealLen: 90,         // 期望边长
    gravity: 0.0016,      // 向心力（防止整体飘走）
    damping: 0.86,        // 阻尼
    maxStep: 0.75,        // 每帧最大位移（px）—— 小 = 缓慢平滑
    fps: 30,              // 仿真帧率
  };

  const GROUPS = [
    { key: "entity", label: "实体", color: "#4a9eff" },
    { key: "knowledge_point", label: "知识点", color: "#3fb950" },
    { key: "document", label: "文档", color: "#d29922" },
    { key: "category", label: "分类", color: "#a371f7" },
  ];

  const S = {
    cy: null,
    nodes: [],
    edges: [],
    running: false,
    raf: 0,
    last: 0,
    vx: new Map(),         // 节点速度
    grid: new Map(),       // 空间网格（斥力加速）
    cell: 90,
    types: new Set(["entity", "knowledge_point", "document", "category"]),
    hideOrphans: false,
    query: "",
    pinned: null,          // 正在拖拽的节点
  };

  const reduced = () => {
    try {
      return window.matchMedia
        && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch (e) { return false; }
  };

  /* ------------------------------------------------------------ 样式 */
  function applyStyle(cy) {
    const st = cy.style();
    // 分组上色：每个类型一个类
    GROUPS.forEach((g) => {
      st.selector(`node.g-${g.key}`)
        .style({ "background-color": g.color, "shape": g.key === "document" ? "round-rectangle" : "ellipse" });
    });
    st.selector("node")
      .style({
        "width": "data(size)",
        "height": "data(size)",
        "border-width": 0,
        "label": "data(label)",
        "color": "#e6edf3",
        "font-size": 10,
        "text-valign": "bottom",
        "text-margin-y": 4,
        "text-opacity": 0,
        "text-background-color": "#0f1419",
        "text-background-opacity": 0.8,
        "text-background-padding": "2px",
        "text-background-shape": "roundrectangle",
        "z-index": 10,
      });
    // 文字淡出：缩放低于阈值就不画字（Obsidian 的 "Text fade threshold"）
    st.selector("node")
      .style("min-zoomed-font-size", 0);
    st.selector("edge")
      .style({
        "line-color": "#5b6672",
        "width": CFG.linkWidth,
        "opacity": 0.35,
        "curve-style": "straight",
        "target-arrow-shape": CFG.showArrows ? "triangle" : "none",
        "z-index": 1,
      });
    st.selector("node:selected")
      .style({ "border-width": 2, "border-color": "#fff", "text-opacity": 1 });
    st.selector(".lit")
      .style({ "opacity": 1, "text-opacity": 1, "z-index": 30 });
    st.selector(".dimmed")
      .style({ "opacity": 0.08, "text-opacity": 0 });
  }

  /** 按当前显示参数刷新画布样式（面板滑块一动就调它）。 */
  function restyle() {
    const cy = S.cy;
    if (!cy) return;
    applyStyle(cy);
    // 节点大小：按度数（对数压缩），再乘面板倍率
    cy.nodes().forEach((n) => {
      const d = n.data("degree") || 0;
      const base = 3.2 + Math.min(9, Math.sqrt(d) * 1.9);
      n.data("size", base * CFG.nodeScale);
      n.style("width", base * CFG.nodeScale);
      n.style("height", base * CFG.nodeScale);
    });
    cy.edges().style("width", CFG.linkWidth);
    cy.edges().style("target-arrow-shape", CFG.showArrows ? "triangle" : "none");
    // 文字：淡出阈值（按缩放）→ 用 zoom 事件控制
    updateLabels();
  }

  function updateLabels() {
    const cy = S.cy;
    if (!cy) return;
    const z = cy.zoom();
    const showAll = CFG.showLabels;
    const fade = CFG.textFade;
    cy.nodes().forEach((n) => {
      if (n.hasClass("lit") || n.selected()) {
        n.style("text-opacity", 1);
        return;
      }
      n.style("text-opacity", showAll || z >= fade ? 1 : 0);
    });
  }

  /* ------------------------------------------------------------ 力仿真 */
  function rebuildGrid() {
    S.grid.clear();
    const nodes = S.cy.nodes();
    for (const n of nodes) {
      const p = n.position();
      const key = ((p.x / S.cell) | 0) + ":" + ((p.y / S.cell) | 0);
      let bucket = S.grid.get(key);
      if (!bucket) { bucket = []; S.grid.set(key, bucket); }
      bucket.push(n);
    }
  }

  function step(dt) {
    const cy = S.cy;
    if (!cy || !cy.nodes().length) return;
    const nodes = cy.nodes();
    const N = nodes.length;
    rebuildGrid();

    // 斥力：只算相邻格子里的节点（够用且快）
    for (const n of nodes) {
      const p = n.position();
      const gx = (p.x / S.cell) | 0, gy = (p.y / S.cell) | 0;
      let fx = 0, fy = 0;
      for (let ox = -1; ox <= 1; ox++) {
        for (let oy = -1; oy <= 1; oy++) {
          const bucket = S.grid.get((gx + ox) + ":" + (gy + oy));
          if (!bucket) continue;
          for (const m of bucket) {
            if (m === n) continue;
            const q = m.position();
            let dx = p.x - q.x, dy = p.y - q.y;
            let d2 = dx * dx + dy * dy;
            if (d2 < 1e-4) { dx = (Math.random() - 0.5); dy = (Math.random() - 0.5); d2 = 0.5; }
            if (d2 > S.cell * S.cell * 4) continue;
            const d = Math.sqrt(d2);
            // 斥力要限幅：两节点贴太近时 1/d² 会算出上万，阻尼压不住，
            // 速度会涨到 Infinity → Infinity/Infinity = NaN → 整张图坐标变废。
            const f = Math.min(S.repulsion / d2, S.maxForce);
            fx += dx / d * f; fy += dy / d * f;
          }
        }
      }
      const v = S.vx.get(n.id()) || { x: 0, y: 0 };
      v.x = (v.x + fx * dt) * S.damping;
      v.y = (v.y + fy * dt) * S.damping;
      // 速度也要限幅（万一某帧的力异常，也不能让速度爆掉）
      const sp = Math.hypot(v.x, v.y);
      if (sp > S.maxSpeed) { v.x = v.x / sp * S.maxSpeed; v.y = v.y / sp * S.maxSpeed; }
      S.vx.set(n.id(), v);
    }

    // 弹簧：边把两端拉向期望长度
    cy.edges().forEach((e) => {
      const a = e.source(), b = e.target();
      const pa = a.position(), pb = b.position();
      let dx = pb.x - pa.x, dy = pb.y - pa.y;
      let d = Math.sqrt(dx * dx + dy * dy);
      if (d < 1e-4) return;
      const f = (d - S.idealLen) * S.spring;
      const ux = dx / d * f, uy = dy / d * f;
      const va = S.vx.get(a.id()) || { x: 0, y: 0 };
      const vb = S.vx.get(b.id()) || { x: 0, y: 0 };
      va.x += ux; va.y += uy;
      vb.x -= ux; vb.y -= uy;
      S.vx.set(a.id(), va); S.vx.set(b.id(), vb);
    });

    // 向心 + 位移
    let cx = 0, cy2 = 0;
    const ps = nodes.map((n) => n.position());
    for (const p of ps) { cx += p.x; cy2 += p.y; }
    cx /= N; cy2 /= N;
    nodes.forEach((n, i) => {
      if (n === S.pinned) return;               // 拖拽中的节点钉住
      const p = ps[i];
      const v = S.vx.get(n.id()) || { x: 0, y: 0 };
      v.x += (cx - p.x) * S.gravity;
      v.y += (cy2 - p.y) * S.gravity;
      let dx = v.x * dt, dy = v.y * dt;
      const d = Math.hypot(dx, dy);
      // 兜底：坏帧不写位置（forEach 里不能写 continue，会是语法错误）
      if (isFinite(d) && isFinite(dx) && isFinite(dy)) {
        if (d > S.maxStep) { dx = dx / d * S.maxStep; dy = dy / d * S.maxStep; }
        n.position({ x: p.x + dx, y: p.y + dy });
      }
    });
  }

  function tick() {
    if (!S.running) return;
    if (document.hidden) return;                 // 后台别烧 CPU
    const now = performance.now();
    const dt = Math.min(0.05, (now - (S.last || now)) / 1000);
    S.last = now;
    step(dt);
  }

  function start() {
    if (S.running || reduced()) return;
    S.running = true;
    S.last = 0;
    // 用**定时器**驱动物理，而不是 requestAnimationFrame：
    // rAF 只在页面真正被合成时才回调（不可见/无合成的环境里永远不触发），
    // 物理不该依赖"有没有画出来"。渲染由 cytoscape 按位置变化自行触发。
    S.timer = setInterval(tick, 1000 / CFG.fps);
  }

  function stop() {
    S.running = false;
    if (S.timer) clearInterval(S.timer);
    S.timer = 0;
  }

  /* ------------------------------------------------------------ 载入数据 */
  function load(view) {
    const cy = S.cy;
    if (!cy) return;
    stop();
    cy.elements().remove();
    S.vx.clear();
    S.nodes = view.nodes || [];      // 面板要用它统计各类型数量
    const els = view.nodes.map((n) => ({
      data: {
        id: String(n.id), label: n.name, type: n.type,
        mentions: n.mentions || 0, degree: n.degree || 0,
        size: 6,
      },
      classes: "g-" + (n.type || "entity"),
    }));
    const ids = new Set(els.map((e) => e.data.id));
    for (const e of view.edges) {
      if (!ids.has(String(e.src)) || !ids.has(String(e.dst))) continue;
      els.push({ data: { id: e.src + "->" + e.dst + "|" + e.rel + "|" + Math.random(),
                         source: String(e.src), target: String(e.dst), rel: e.rel } });
    }
    cy.add(els);
    applyStyle(cy);
    // 初始位置：螺旋（phyllotaxis）撒开 —— 圆周撒开会留一个空环，
    // 螺旋覆盖均匀，仿真随后自己把它长成网络。
    const nodeEls = cy.nodes();
    const N = nodeEls.length;
    nodeEls.forEach((n, i) => {
      const a = i * 2.399963;                       // 黄金角
      const r = 26 * Math.sqrt(i + 0.5);
      n.position({ x: Math.cos(a) * r, y: Math.sin(a) * r });
    });
    restyle();
    // 筛选：类型 / 孤立节点 / 搜索
    applyFilters();
    renderPanel();          // 面板要显示各类型数量，载入后得重绘一次
    start();
    fit();
  }

  /* ------------------------------------------------------------ 筛选 */
  function applyFilters() {
    const cy = S.cy;
    if (!cy) return;
    const q = (S.query || "").trim().toLowerCase();
    cy.nodes().forEach((n) => {
      const t = n.data("type") || "entity";
      let show = S.types.has(t);
      if (show && S.hideOrphans && (n.data("degree") || 0) <= 0) show = false;
      if (show && q) {
        const label = String(n.data("label") || "").toLowerCase();
        show = label.indexOf(q) >= 0;
      }
      n.style("display", show ? "element" : "none");
    });
    // 节点藏了，连到它的边也要藏
    cy.edges().forEach((e) => {
      const s = e.source().style("display") !== "none";
      const d = e.target().style("display") !== "none";
      e.style("display", (s && d) ? "element" : "none");
    });
  }

  function fit() {
    const cy = S.cy;
    if (!cy) return;
    const vis = cy.nodes(":visible");
    if (!vis.length) return;
    cy.fit(vis, 30, 200);
  }

  /* ------------------------------------------------------------ 交互 */
  function bind(cy) {
    if (cy.__kbBound) return;
    cy.__kbBound = true;
    // 拖拽时钉住节点
    cy.on("grab", "node", (e) => { S.pinned = e.target; });
    cy.on("free", "node", () => { S.pinned = null; });
    // 悬停：出字 + 邻接高亮（Obsidian 的 hover 行为）
    cy.on("mouseover", "node", (e) => {
      const n = e.target;
      if (!n.visible()) return;
      const hood = n.connectedEdges().connectedNodes();
      cy.nodes().not(hood).not(n).addClass("dimmed");
      cy.edges().not(n.connectedEdges()).addClass("dimmed");
      n.connectedEdges().addClass("lit");
      hood.not(n).addClass("lit");
      n.addClass("lit");
    });
    cy.on("mouseout", "node", () => {
      cy.elements().removeClass("dimmed");
      cy.elements().removeClass("lit");
      updateLabels();
    });
    cy.on("tap", "node", (e) => {
      if (window.KB && KB.showNode) KB.showNode(e.target.id());
    });
    cy.on("zoom", () => updateLabels());
    cy.on("pan", () => updateLabels());
  }

  /* ------------------------------------------------------------ 面板 */
  function renderPanel() {
    const panel = $("#g-panel");
    if (!panel) return;
    const counts = {};
    S.nodes.forEach((n) => { counts[n.type] = (counts[n.type] || 0) + 1; });
    panel.innerHTML = `
      <div class="gp-sec">
        <div class="gp-head">筛选 <span class="muted">${S.nodes.length} 个节点</span></div>
        <input id="gp-q" class="gp-search" placeholder="搜索节点…" value="${(S.query || "").replace(/"/g, "&quot;")}">
        ${GROUPS.map((g) => `
          <label class="chk gp-chk">
            <input type="checkbox" data-type="${g.key}" ${S.types.has(g.key) ? "checked" : ""}>
            <span class="gp-dot" style="background:${g.color}"></span>${g.label}
            <span class="muted">${counts[g.key] || 0}</span>
          </label>`).join("")}
        <label class="chk gp-chk"><input type="checkbox" id="gp-orphan" ${S.hideOrphans ? "checked" : ""}>只显示有连接的</label>
      </div>
      <div class="gp-sec">
        <div class="gp-head">显示</div>
        <label class="gp-slider">节点大小 <span id="gp-size-v">${CFG.nodeScale.toFixed(1)}</span>
          <input type="range" id="gp-size" min="0.5" max="2" step="0.1" value="${CFG.nodeScale}"></label>
        <label class="gp-slider">连线粗细 <span id="gp-link-v">${CFG.linkWidth.toFixed(1)}</span>
          <input type="range" id="gp-link" min="0.2" max="2.5" step="0.1" value="${CFG.linkWidth}"></label>
        <label class="gp-slider">文字淡出阈值 <span id="gp-fade-v">${CFG.textFade.toFixed(2)}</span>
          <input type="range" id="gp-fade" min="0.1" max="2" step="0.05" value="${CFG.textFade}"></label>
        <label class="chk gp-chk"><input type="checkbox" id="gp-arrows" ${CFG.showArrows ? "checked" : ""}>显示箭头</label>
        <label class="chk gp-chk"><input type="checkbox" id="gp-labels" ${CFG.showLabels ? "checked" : ""}>总是显示文字</label>
      </div>
      <div class="gp-sec">
        <div class="gp-head">导出</div>
        <button class="sm" onclick="location.href='/api/v1/graph/export/vault?limit=800'">导出 Obsidian 库 (.zip)</button>
        <button class="sm" onclick="location.href='/api/v1/graph/export/canvas?limit=200'">导出 Obsidian 画布 (.canvas)</button>
        <p class="muted" style="font-size:11px;margin:6px 0 0">
          导出的库用 Obsidian 打开即可用上它现成的图谱视图、双链与插件。</p>
      </div>`;
    bindPanel();
  }

  function bindPanel() {
    const panel = $("#g-panel");
    if (!panel) return;
    panel.querySelectorAll("[data-type]").forEach((cb) => {
      cb.onchange = () => {
        const t = cb.dataset.type;
        if (cb.checked) S.types.add(t); else S.types.delete(t);
        applyFilters();
      };
    });
    const q = $("#gp-q");
    if (q) q.oninput = () => { S.query = q.value; applyFilters(); };
    const orphan = $("#gp-orphan");
    if (orphan) orphan.onchange = () => { S.hideOrphans = orphan.checked; applyFilters(); };
    const size = $("#gp-size");
    if (size) size.oninput = () => {
      CFG.nodeScale = parseFloat(size.value);
      $("#gp-size-v").textContent = CFG.nodeScale.toFixed(1);
      restyle();
    };
    const link = $("#gp-link");
    if (link) link.oninput = () => {
      CFG.linkWidth = parseFloat(link.value);
      $("#gp-link-v").textContent = CFG.linkWidth.toFixed(1);
      restyle();
    };
    const fade = $("#gp-fade");
    if (fade) fade.oninput = () => {
      CFG.textFade = parseFloat(fade.value);
      $("#gp-fade-v").textContent = CFG.textFade.toFixed(2);
      updateLabels();
    };
    const arrows = $("#gp-arrows");
    if (arrows) arrows.onchange = () => { CFG.showArrows = arrows.checked; restyle(); };
    const labels = $("#gp-labels");
    if (labels) labels.onchange = () => { CFG.showLabels = labels.checked; updateLabels(); };
  }

  /* ------------------------------------------------------------ 重新布局 */
  /** 把节点位置打散（活体仿真会重新把它们长开 —— 这就是"动态"的由来）。 */
  function scatter() {
    const cy = S.cy;
    if (!cy) return;
    const N = cy.nodes().length;
    const R = 120 * Math.sqrt(Math.max(1, N));
    cy.nodes().forEach((n, i) => {
      const a = 2 * Math.PI * i / Math.max(1, N);
      n.position({ x: Math.cos(a) * R * (0.7 + Math.random() * 0.6),
                   y: Math.sin(a) * R * (0.7 + Math.random() * 0.6) });
    });
    S.vx.clear();
    start();
  }

  /* ------------------------------------------------------------ 对外 */
  KB.graphView = {
    init(cy) {
      S.cy = cy;
      bind(cy);
      renderPanel();
    },
    load,
    fit,
    scatter,
    restyle,
    updateLabels,
    start,
    stop,
    get cfg() { return CFG; },
    debug() {
      return {
        running: S.running, hasCy: !!S.cy, raf: !!S.raf,
        vx: S.vx.size, grid: S.grid.size,
        last: Math.round(S.last || 0), acc: Math.round(S.acc || 0),
        nodes: S.cy ? S.cy.nodes().length : 0,
        reduced: reduced(), hidden: document.hidden,
      };
    },
  };

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop(); else start();
  });
})();
