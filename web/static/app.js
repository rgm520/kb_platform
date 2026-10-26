/* 企业知识平台 前端（无构建步骤，原生 ES Module 风格单页应用） */
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

const state = {
  user: null, role: null, page: "desktop",
  docs: { page: 1, size: 50, total: 0 },
  filters: { status: "", share: "" },
  boardView: "docs", nonDocByExt: [],
  cy: null, models: [], adminTab: "users",
  restrictedDirs: [],
  tokenUserFilter: null, tokenLabel: "", tokenCounts: {},
  // 建号表单的草稿：面板每次重渲染都会重建 DOM，不存草稿的话，
  // 管理员填到一半点别处（授权/密钥/标记…）回来就发现输入框被清空了
  newUserDraft: {},
};

function toast(msg, ms) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), ms || 3200);
}

async function api(path, opts) {
  opts = opts || {};
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, init);
  if (r.status === 401) { showLogin(); throw new Error("未登录"); }
  const txt = await r.text();
  let data = null;
  try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = { detail: txt }; }
  if (!r.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === "string" ? d : JSON.stringify(d || ("HTTP " + r.status)));
  }
  return data;
}

/* ---------------------------------------------------------- 登录 */
/* 换用户时必须清空"上一个用户的数据"所在容器。

   共享电脑上：A 退出、B 登录后，A 刚才的提问、回答、引用片段、看板清单、
   文件列表全都还留在 DOM 里 —— B 一进来就看得见。SPA 换用户只切显隐，
   所以这里要显式清空（清单化是为了能测试、也免得以后漏）。 */
const USER_SCOPED_CONTAINERS = [
  // AI 问答：上一个人的提问、回答、引用片段、提示、反馈
  "#a-answer", "#a-cites", "#a-q", "#a-notice", "#a-feedback",
  // 文档看板：统计卡片、**表格的行容器**（不是外层表格！清掉外层会把
  // <table> 整个删掉，之后 loadDocs() 找不到 #doc-table tbody → 列表加载失败）、
  // 统计视图、计数与页码
  "#stat-cards", "#doc-table tbody", "#doc-stats", "#doc-total", "#pg-info",
  "#f-share",
  // 文件页：目录行、图标视图、面包屑、两种搜索结果、预览面板、右键菜单
  "#fm-list", "#fm-icons", "#fm-crumbs", "#fm-search", "#fm-search-results",
  "#fm-content-results", "#fm-preview", "#fm-ctx", "#fm-upload-panel",
  // 知识图谱：侧栏与列表（画布 #cy 由 cytoscape 实例管，用 cy.clear()，
  // 清 DOM 会把画布拆掉）
  "#g-list", "#g-side",
  // 管理台与弹窗
  "#admin-body", "#modal-body",
];

function clearUserScopedUI() {
  USER_SCOPED_CONTAINERS.forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
      if (el.type !== "checkbox" && el.type !== "radio") el.value = "";
    } else {
      el.innerHTML = "";
    }
  });
  // 图谱画布是 cytoscape 实例，不是普通 DOM
  if (state.cy) { try { state.cy.clear(); } catch (e) { /* 忽略 */ } }
  // 弹窗里可能留着上一个用户的内容（详情、编辑表单），连弹窗一起关掉
  const modal = $("#modal");
  if (modal) modal.classList.add("hidden");
  // 状态里同样不能留上一个用户的数据
  state.docs = { page: 1, size: 50, total: 0 };
  state.duplicates = []; state.dupSel = {}; state.dupCollapsed = {};
  state.adminUsers = []; state.tokenCounts = {};
  state.scopeSelected = new Set();
  state.contentSearch = [];
  if (window.KB && KB.files && typeof KB.files.clear === "function") KB.files.clear();
}

function showLogin() {
  clearUserScopedUI();
  $("#login").classList.remove("hidden");
  $("#main").classList.add("hidden");
  state.user = null;
}
function showMain(user) {
  clearUserScopedUI();
  state.user = user; state.role = user.role;
  window.KB = window.KB || {}; window.KB.me = user;
  $("#login").classList.add("hidden");
  $("#main").classList.remove("hidden");
  $("#who").textContent = (user.display_name || user.username) + (user.role === "admin" ? "（管理员）" : "");
  $$(".admin-only").forEach(e => e.classList.toggle("hidden", user.role !== "admin"));
  // 管理台默认页签：管理员进「用户与权限」，普通用户进「API 密钥」
  setAdminTab(user.role === "admin" ? "users" : "tokens");
  loadModels();
  if (!location.hash || location.hash === "#/board") location.hash = "#/desktop";
  route();
}
function setAdminTab(tab) {
  state.adminTab = tab;
  $$("#p-admin .tabs button").forEach(x => x.classList.toggle("active", x.dataset.t === tab));
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-err").textContent = "";
  try {
    const r = await api("/api/v1/auth/login", {
      method: "POST",
      body: { username: $("#login-user").value, password: $("#login-pass").value },
    });
    showMain(r.user);
  } catch (err) { $("#login-err").textContent = err.message; }
});
$("#logout").addEventListener("click", async () => {
  try { await api("/api/v1/auth/logout", { method: "POST" }); } catch (e) { }
  showLogin();
});

/* ---------------------------------------------------------- 返回主菜单 */
$("#btn-home").addEventListener("click", () => { location.hash = "#/desktop"; });

/* ---------------------------------------------------------- 修改密码 */
$("#btn-chpwd").addEventListener("click", () => {
  openModal(`<h3>修改密码</h3>
    <p class="muted">新密码至少 8 位。修改后当前会话仍然有效。</p>
    <div style="display:flex;flex-direction:column;gap:8px;margin-top:10px">
      <label>原密码<input id="cp-old" type="password" autocomplete="current-password"></label>
      <label>新密码<input id="cp-new" type="password" autocomplete="new-password"></label>
      <label>确认新密码<input id="cp-new2" type="password" autocomplete="new-password"></label>
    </div>
    <div id="cp-err" class="err"></div>
    <button class="primary" onclick="doChangePwd()">确认修改</button>`);
});
window.doChangePwd = async () => {
  const err = $("#cp-err");
  err.textContent = "";
  const o = $("#cp-old").value, n = $("#cp-new").value, n2 = $("#cp-new2").value;
  if (!o || !n) { err.textContent = "请填写原密码与新密码"; return; }
  if (n !== n2) { err.textContent = "两次输入的新密码不一致"; return; }
  if (n.length < 8) { err.textContent = "新密码至少 8 位"; return; }
  try {
    await api("/api/v1/auth/change-password", { method: "POST",
      body: { old_password: o, new_password: n } });
    closeModal();
    toast("密码已修改，下次登录请用新密码");
  } catch (e) { err.textContent = e.message; }
};

/* ---------------------------------------------------------- 路由 */
function route() {
  const h = (location.hash || "#/desktop").replace("#/", "");
  const p = ["desktop", "files", "trash", "board", "graph", "ask", "admin"].includes(h) ? h : "desktop";
  state.page = p;
  // 顶部只留「返回主菜单」；在桌面上它自己就是主菜单，藏掉免得重复
  const home = $("#btn-home");
  if (home) home.classList.toggle("hidden", p === "desktop");
  $$(".page").forEach(s => s.classList.toggle("active", s.id === "p-" + p));
  if (p === "desktop") window.KB && KB.desktop && KB.desktop.mount();
  if (p === "files") window.KB && KB.files && KB.files.load();
  if (p === "trash") window.KB && KB.trash && KB.trash.load();
  if (p === "board") loadStats(), loadDocs();
  if (p === "graph") initGraph();
  if (p === "ask") loadModels();
  if (p === "admin") loadAdmin();
}
window.addEventListener("hashchange", route);

/* ---------------------------------------------------------- 看板 */
async function loadStats() {
  try {
    const s = await api("/api/v1/documents/stats");
    const bs = s.by_status || {};
    // 卡片 → 下方清单的过滤条件（filter: {status, tier2}）
    const cards = [
      { l: "监控范围内文档", n: s.total, c: "", filter: { status: "", tier2: 0 } },
      { l: "已完成解析 (Tier 1)", n: s.tier1_done, c: "ok", filter: { status: "done", tier2: 0 } },
      { l: "已进入知识图谱 (Tier 2)", n: s.tier2_done, c: "ok", filter: { status: "", tier2: 1 } },
      { l: "待处理", n: bs.pending || 0, c: "warn", filter: { status: "pending", tier2: 0 } },
      { l: "处理失败", n: bs.failed || 0, c: "err", filter: { status: "failed", tier2: 0 } },
      { l: "已跳过(扫描件/源文件已删)", n: bs.skipped || 0, c: "",
        filter: { status: "skipped", tier2: 0 } },
      { l: "跳过的非文档文件", n: s.non_document_files || 0, c: "", filter: { stats: 1 } },
      { l: "重复文档", n: (s.duplicates || {}).group_count || 0, c: "warn",
        filter: { dup: 1 }, sub: `${(s.duplicates || {}).file_count || 0} 份文件 · 可省 ${fmtBytes((s.duplicates || {}).wasted_bytes || 0)}` },
    ];
    state.nonDocByExt = s.non_doc_by_ext || [];
    $("#stat-cards").innerHTML = cards.map((x, i) =>
      `<div class="card-stat ${x.c}${x.filter ? " clickable" : ""}" data-i="${i}"
            ${x.filter ? 'title="点击查看对应清单"' : 'title="点击查看分类统计"'}>
         <div class="n">${x.n}</div><div class="l">${x.l}</div>
         ${x.sub ? `<div class="sub muted">${x.sub}</div>` : ""}</div>`).join("");
    $("#stat-cards").querySelectorAll(".card-stat").forEach((el) => {
      el.addEventListener("click", () => {
        const card = cards[Number(el.dataset.i)];
        if (!card.filter) return;
        if (card.filter.stats) {
          // 切换到"非文档文件分类统计"视图
          state.boardView = state.boardView === "stats" ? "docs" : "stats";
          markDocFilter();
          renderBoardBody();
          return;
        }
        if (card.filter.dup) {
          // 切换到"重复文档"视图
          state.boardView = state.boardView === "dups" ? "docs" : "dups";
          markDocFilter();
          renderBoardBody();
          if (state.boardView === "dups") loadDuplicates();
          return;
        }
        const f = card.filter;
        const same = state.filters.status === f.status && (state.filters.tier2 || 0) === f.tier2;
        setDocFilter(same ? { status: "", tier2: 0 } : f);
      });
    });
    const tabs = { all: s.total, done: bs.done || 0, pending: bs.pending || 0,
                   failed: bs.failed || 0, skipped: bs.skipped || 0 };
    for (const k in tabs) { const el = $("#tab-" + k); if (el) el.textContent = tabs[k]; }
    // 扫描件批量排队 OCR 的入口
    const ocrHint = $("#board-ocr");
    if (ocrHint) {
      const n = (s.skipped_reasons || []).filter(x => x.reason === "扫描件待 OCR")
        .reduce((a, b) => a + (b.count || 0), 0);
      ocrHint.classList.toggle("hidden", !n);
      if (n) {
        ocrHint.innerHTML = `有 <b>${n}</b> 个扫描件待 OCR（文件在 NAS 上，但 PDF 没有文字层）。
          <button class="sm" onclick="reparseAllScanned()">排队 OCR 全部扫描件</button>
          <span class="muted">约 136 秒/页，在后台队列里跑，不影响平台使用。</span>`;
      }
    }
    markDocFilter();
  } catch (e) { toast("统计加载失败: " + e.message); }
}

/** 统一设置文档清单过滤：状态 / Tier2，并同步卡片与页签的选中态。 */
function setDocFilter(filter) {
  const f = filter || { status: "", tier2: 0 };
  state.filters.status = f.status || "";
  state.filters.tier2 = f.tier2 || 0;
  state.docs.page = 1;
  state.boardView = "docs";
  markDocFilter();
  renderBoardBody();
  loadDocs();
}
/** 重复文档视图：按 MD5 分组，组内列出每一份，可勾选要删的。 */
async function loadDuplicates() {
  const box = $("#doc-stats");
  if (!box) return;
  box.innerHTML = '<p class="muted">正在统计重复文件…</p>';
  try {
    const r = await api("/api/v1/documents/duplicates");
    state.duplicates = r.groups || [];
    state.dupSel = {};
    state.dupCollapsed = {};      // 默认全部折叠
    renderDuplicates(box, r);
  } catch (e) {
    box.innerHTML = `<p class="err">重复文件统计失败：${esc(e.message)}</p>`;
  }
}
function fmtBytes(n) {
  n = Number(n || 0);
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
  return (n / 1024 / 1024 / 1024).toFixed(2) + " GB";
}
function renderDuplicates(box, r) {
  const groups = state.duplicates || [];
  if (!groups.length) {
    box.innerHTML = `<div class="fm-empty">没有发现重复文件<br>
      <span class="muted">（按 MD5 内容哈希判定；同名不同内容不算重复）</span></div>`;
    return;
  }
  const head = `<div class="page-head" style="margin:2px 0 10px">
      <div><h3 style="margin:0">重复文档（${r.group_count} 组 / ${r.file_count} 份）</h3>
        <p class="muted" style="margin:4px 0 0">按 <b>MD5 内容哈希</b>判定（不是文件名）。
          展开一组可勾选要删的重复件，每组会保留一份；删除的文件进回收站（可恢复）。</p></div>
      <button onclick="state.boardView='docs'; renderBoardBody(); loadDocs();">← 返回文档清单</button>
    </div>`;
  const body = groups.map((g, gi) => {
    // 默认折叠（只有显式设为 false 才是展开）
    const collapsed = state.dupCollapsed[gi] !== false;
    // 默认选择：保留第一份，其余勾选为"要删"（只在首次进入该组时设定，
    // 否则会把用户的"全不选/手动取消勾选"覆盖回去）
    if (!state.dupSel[gi]) {
      state.dupSel[gi] = { keep: g.items[0] ? g.items[0].id : null, del: {} };
      g.items.forEach((it, i) => { if (i > 0) state.dupSel[gi].del[it.id] = true; });
    }
    const sel = state.dupSel[gi];
    const names = g.items.map((it) => it.name).join("、");
    const preview = names.length > 90 ? names.slice(0, 90) + "…" : names;
    const keepItem = g.items.find((x) => String(x.id) === String(sel.keep)) || g.items[0];
    return `
    <div class="dup-group ${collapsed ? "collapsed" : ""}">
      <div class="dup-head">
        <button class="dup-tw" onclick="dupToggleGroup(${gi})" title="${collapsed ? "展开" : "折叠"}">${collapsed ? "▸" : "▾"}</button>
        <span class="md5">MD5 ${esc(g.md5.slice(0, 12))}…</span>
        <span class="muted">${g.count} 份 · 每份 ${fmtBytes(g.size)}</span>
        <span class="muted dup-preview" title="${esc(names)}">${esc(preview)}</span>
        <span class="muted dup-keep">保留：${esc(keepItem ? keepItem.name : "-")}</span>
        <button class="sm" onclick="dupSelectAll(${gi}, true)">全选</button>
        <button class="sm" onclick="dupSelectAll(${gi}, false)">全不选</button>
        <button class="sm primary" onclick="dupResolve(${gi})">删除所选（保留一份）</button>
      </div>
      ${collapsed ? "" : `
      <table><thead><tr><th>保留这份</th><th>文件</th><th>所在目录</th><th>大小</th><th>修改时间</th><th>状态</th></tr></thead><tbody>
        ${g.items.map((it, ii) => `
          <tr>
            <td><input type="radio" name="dupkeep-${gi}" value="${it.id}" ${String(it.id) === String(sel.keep) ? "checked" : ""}
                       onchange="dupSetKeep(${gi}, ${it.id})"></td>
            <td><label class="chk"><input type="checkbox" ${sel.del[it.id] ? "checked" : ""}
                       onchange="dupToggle(${gi}, ${it.id}, this.checked)"> ${esc(it.name)}</label></td>
            <td class="muted">${esc(it.share)} / ${esc(it.rel_path || "")}</td>
            <td class="muted">${fmtBytes(it.size)}</td>
            <td class="muted">${it.mtime ? new Date(it.mtime * 1000).toLocaleString("zh-CN") : "-"}</td>
            <td class="muted">${statusText(it.status)}</td>
          </tr>`).join("")}
      </tbody></table>`}
    </div>`;
  }).join("");
  box.innerHTML = head + body;
}
window.dupToggleGroup = (gi) => {
  const was = state.dupCollapsed[gi] !== false;   // 现在是折叠的？
  state.dupCollapsed[gi] = was ? false : true;    // 折叠→展开 / 展开→折叠
  renderDuplicates(document.getElementById("doc-stats"), {});
};
window.dupSetKeep = (gi, id) => {
  const sel = state.dupSel[gi] || (state.dupSel[gi] = { keep: null, del: {} });
  sel.keep = id;
  // 保留的这一份不能同时被勾成"要删"
  delete sel.del[id];
  renderDuplicates(document.getElementById("doc-stats"), {});
};
function dupToggle(gi, id, on) {
  state.dupSel[gi] = state.dupSel[gi] || { keep: null, del: {} };
  if (on) state.dupSel[gi].del[id] = true; else delete state.dupSel[gi].del[id];
}
function dupSelectAll(gi, on) {
  const g = (state.duplicates || [])[gi];
  if (!g) return;
  const sel = state.dupSel[gi] || (state.dupSel[gi] = {
    keep: g.items[0] ? g.items[0].id : null, del: {},
  });
  sel.del = {};
  if (on) g.items.forEach((it) => { if (String(it.id) !== String(sel.keep)) sel.del[it.id] = true; });
  renderDuplicates(document.getElementById("doc-stats"), {});
}
window.dupResolve = async (gi) => {
  const g = (state.duplicates || [])[gi];
  if (!g) return;
  const sel = state.dupSel[gi] || { keep: null, del: {} };
  // 保留项从状态里取（折叠时单选按钮不在 DOM 里）
  const keepId = sel.keep || (g.items[0] ? g.items[0].id : null);
  const delIds = Object.keys(sel.del || {}).map(Number).filter((x) => x !== Number(keepId));
  if (!delIds.length) { toast("请先勾选要删除的重复件（每组至少保留一份）"); return; }
  const keep = g.items.find((x) => String(x.id) === String(keepId)) || g.items[0];
  const delNames = g.items.filter((x) => delIds.includes(x.id)).map((x) => x.name);
  if (!confirm(`确认删除这 ${delIds.length} 个重复文件？\n\n`
    + `删除：${delNames.join("、")}\n保留：${keep.name}\n\n`
    + `删除的文件会进回收站（可恢复）。`)) return;
  let shortcut = false;
  if (confirm("是否在【被删文件原来的位置】放一个快捷方式，指向保留的那份？\n\n"
    + "这样别人按老路径打开时依然能打开（打开的是保留的那份）。")) shortcut = true;
  try {
    const r = await api("/api/v1/documents/duplicates/resolve", { method: "POST",
      body: { keep_id: Number(keep.id), delete_ids: delIds, make_shortcut: shortcut } });
    const n = (r.deleted || []).length;
    const sk = (r.skipped || []).length;
    toast(`已删除 ${n} 个重复文件${sk ? `，${sk} 个被跳过` : ""}`
      + `${shortcut ? "，原位置已放快捷方式" : ""}`, 6000);
    loadStats();
    loadDuplicates();
  } catch (e) { toast("处理失败: " + e.message, 5000); }
};

/** 文档清单 ↔ 非文档统计 / 重复文档 的视图切换。 */
function renderBoardBody() {
  const isStats = state.boardView === "stats";
  const isDups = state.boardView === "dups";
  const alt = isStats || isDups;
  const wrap = $("#doc-table-wrap"), pager = $("#doc-pager"), stats = $("#doc-stats");
  const tabs = $("#status-tabs"), toolbar = $("#board-toolbar");
  if (wrap) wrap.classList.toggle("hidden", alt);
  if (pager) pager.classList.toggle("hidden", alt);
  if (tabs) tabs.classList.toggle("hidden", alt);
  if (toolbar) toolbar.classList.toggle("hidden", alt);
  if (stats) {
    stats.classList.toggle("hidden", !alt);
    if (isStats) renderNonDocStats(stats);
    if (isDups) loadDuplicates();
  }
  markDocFilter();
}
function renderNonDocStats(box) {
  const rows = state.nonDocByExt || [];
  const total = rows.reduce((a, b) => a + (b.count || 0), 0);
  if (!rows.length) {
    box.innerHTML = `<div class="fm-empty">没有统计到非文档文件<br>
      <span class="muted">（扫描器完成一轮遍历后，这里会按文件类型列出数量）</span></div>`;
    return;
  }
  const nameOf = (ext) => ({
    ".png": "PNG 图片", ".jpg": "JPG 图片", ".jpeg": "JPG 图片", ".gif": "GIF 图片",
    ".bmp": "BMP 图片", ".webp": "WebP 图片", ".svg": "SVG 矢量图",
    ".mp4": "视频", ".mov": "视频", ".avi": "视频", ".mkv": "视频", ".wmv": "视频",
    ".mp3": "音频", ".wav": "音频", ".m4a": "音频",
    ".zip": "压缩包", ".rar": "压缩包", ".7z": "压缩包", ".tar": "压缩包", ".gz": "压缩包",
    ".db": "数据库文件", ".vsdx": "Visio 流程图", ".vsd": "Visio 流程图",
    ".emf": "图元文件", ".wmf": "图元文件", ".tmp": "临时文件", ".bak": "备份文件",
    "(none)": "无扩展名",
  }[String(ext).toLowerCase()] || "其它文件");
  box.innerHTML = `
    <div class="page-head" style="margin:2px 0 10px">
      <div><h3 style="margin:0">非文档文件统计</h3>
        <p class="muted" style="margin:4px 0 0">这些文件在 NAS 上存在，但平台**不解析**它们
        （不建文档记录、不做 OCR、不进全文/向量/图谱），所以问答检索不到。共 ${total} 个。</p></div>
      <button onclick="state.boardView='docs'; renderBoardBody(); loadDocs();">← 返回文档清单</button>
    </div>
    <table><thead><tr><th>文件类型</th><th>扩展名</th><th>数量</th><th>占比</th></tr></thead><tbody>
      ${rows.map(r => `<tr>
        <td>${esc(nameOf(r.ext))}</td>
        <td><code>${esc(r.ext)}</code></td>
        <td>${r.count}</td>
        <td class="muted">${total ? (r.count * 100 / total).toFixed(1) : "0"}%</td>
      </tr>`).join("")}
    </tbody></table>`;
}
function markDocFilter() {
  const st = state.filters.status || "";
  const t2 = state.filters.tier2 || 0;
  // 状态页签
  $$("#status-tabs .tab").forEach((b) => {
    const bst = b.dataset.status || "";
    b.classList.toggle("active", !t2 && bst === st);
  });
  // 统计卡片
  const cards = $$("#stat-cards .card-stat");
  cards.forEach((el) => {
    const i = Number(el.dataset.i);
    const map = [
      { status: "", tier2: 0 }, { status: "done", tier2: 0 }, { status: "", tier2: 1 },
      { status: "pending", tier2: 0 }, { status: "failed", tier2: 0 },
      { status: "skipped", tier2: 0 }, { stats: 1 }, { dup: 1 },
    ];
    const f = map[i];
    let on = false;
    if (f) {
      if (f.stats) on = state.boardView === "stats";
      else if (f.dup) on = state.boardView === "dups";
      else on = state.boardView === "docs" && f.status === st && (f.tier2 || 0) === t2;
    }
    el.classList.toggle("active", on);
  });
}
/* 文件名下面那行显示**所在目录**（原来把完整路径重复一遍，白占宽度）。 */
function parentRel(rel) {
  const p = String(rel || "").replace(/\//g, "\\");
  if (!p) return "";
  const i = p.lastIndexOf("\\");
  return i < 0 ? "" : p.slice(0, i);
}
async function loadDocs() {
  const q = new URLSearchParams({
    page: state.docs.page, page_size: state.docs.size,
  });
  if (state.filters.status) q.set("status", state.filters.status);
  if (state.filters.tier2) q.set("tier2", state.filters.tier2);
  if (state.filters.share) q.set("share", state.filters.share);
  try {
    const r = await api("/api/v1/documents?" + q.toString());
    const pages = Math.max(1, Math.ceil(r.total / r.page_size));
    if (state.docs.page > pages) { state.docs.page = pages; return loadDocs(); }
    state.docs.total = r.total;
    const tb = $("#doc-table tbody");
    tb.innerHTML = "";
    if (!r.items.length) {
      tb.innerHTML = '<tr><td colspan="9" class="muted">没有符合条件的文档</td></tr>';
    }
    for (const d of r.items) {
      const tr = document.createElement("tr");
      tr.dataset.docId = d.id;
      tr.innerHTML = `
        <td class="doc-name" title="${esc(d.rel_path || d.name)}">
            <div class="doc-file">${esc(d.name)}</div>
            <div class="doc-dir">${esc(parentRel(d.rel_path))}</div></td>
        <td>${esc(d.share)}</td>
        <td><code>${esc(d.ext)}</code></td>
        <td><span class="badge ${esc(d.status)}">${statusText(d.status)}</span>
            ${d.status_detail ? `<div class="muted" style="font-size:11px" title="${esc(d.status_detail)}">${esc(d.status_detail)}</div>` : ""}</td>
        <td><div class="two-tier"><span class="dot ${d.tier1_done ? "on" : ""}"></span>解析
            <span class="dot t2 ${d.tier2_done ? "on" : ""}"></span>提炼</div></td>
        <td>${d.chunk_count || 0}</td>
        <td>${d.kp_count || 0}</td>
        <td class="muted">${d.updated_at ? new Date(d.updated_at * 1000).toLocaleString("zh-CN") : "-"}</td>
        <td><button class="sm" onclick="showDoc(${d.id})">详情</button>
            ${(d.status === "skipped" || d.status === "failed")
              ? `<button class="sm" onclick="reparseDoc(${d.id})">重新解析</button>` : ""}</td>`;
      tr.addEventListener("contextmenu", (ev) => {
        ev.preventDefault();
        showDocMenu(ev, d);
      });
      tb.appendChild(tr);
    }
    $("#doc-total").textContent = "共 " + r.total + " 个文档";
    $("#pg-info").textContent = `第 ${r.page} / ${pages} 页`;
    $("#pg-first").disabled = r.page <= 1;
    $("#pg-prev").disabled = r.page <= 1;
    $("#pg-next").disabled = r.page >= pages;
    $("#pg-last").disabled = r.page >= pages;
  } catch (e) { toast("文档列表加载失败: " + e.message); }
}
function statusText(s) {
  return ({ done: "已解析", pending: "待处理", failed: "失败", skipped: "已跳过" })[s] || s || "待处理";
}

/* ---------------------------------------------------------- 看板右键菜单 */
async function showDocMenu(ev, doc) {
  const menu = $("#doc-ctx");
  if (!menu) return;
  menu.innerHTML = `
    <button data-act="view">查看文件</button>
    <button data-act="locate">打开文档所在路径</button>
    <button data-act="download">下载文件</button>
    <button data-act="copy">复制文件路径</button>`;
  menu.classList.remove("hidden");
  menu.style.left = Math.min(ev.clientX, window.innerWidth - 210) + "px";
  menu.style.top = Math.min(ev.clientY, window.innerHeight - 170) + "px";
  let info = null;
  const setMsg = (m) => { const t = menu.querySelector(".ctx-msg"); if (t) t.textContent = m; };
  menu.querySelectorAll("button").forEach((b) => {
    b.onclick = async () => {
      const act = b.dataset.act;
      hideDocMenu();
      try {
        if (!info) {
          const r = await api(`/api/v1/documents/${doc.id}/file`);
          info = r;
        }
        if (act === "view") {
          if (info.in_fs) {
            // 2.0 存储源里的文件：直接在文件页预览
            location.hash = "#/files";
            setTimeout(() => window.KB && KB.files && KB.files.openAt(info.source_id, info.rel, true), 120);
          } else {
            openDocText(doc, info);
          }
        } else if (act === "locate") {
          if (info.in_fs) {
            location.hash = "#/files";
            setTimeout(() => window.KB && KB.files && KB.files.openAt(info.source_id, info.rel, false), 120);
          } else {
            toast("这个文件还没有接入 2.0 文件页，先在「存储接入」里接入对应共享");
          }
        } else if (act === "download") {
          const a = document.createElement("a");
          a.href = info.download_url;
          a.download = info.name || doc.name;
          document.body.appendChild(a);
          a.click();
          a.remove();
        } else if (act === "copy") {
          const text = info.path || doc.rel_path || "";
          if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast("路径已复制")).catch(() => toast(text));
          else toast(text);
        }
      } catch (e) { toast("操作失败: " + e.message, 5000); }
    };
  });
  // 预取文件位置，失败时在菜单里提示
  try {
    const r = await api(`/api/v1/documents/${doc.id}/file`);
    info = r;
  } catch (e) {
    setMsg("无法定位文件：" + e.message);
  }
}
function hideDocMenu() {
  const menu = $("#doc-ctx");
  if (menu) menu.classList.add("hidden");
}
document.addEventListener("click", (e) => {
  if (!e.target.closest || !e.target.closest("#doc-ctx")) hideDocMenu();
});
async function openDocText(doc, info) {
  openModal(`<h3 style="margin:0 0 8px">${esc(doc.name)}</h3>
    <p class="muted" style="font-size:12px">${esc(info.path || "")}</p>
    <pre id="doc-text" style="max-height:60vh;overflow:auto;white-space:pre-wrap;font-size:12px">加载中…</pre>`);
  try {
    const r = await api(`/api/v1/documents/${doc.id}/content`);
    const el = $("#doc-text");
    if (el) el.textContent = r.text || "（没有解析出文本，可能是扫描件）";
  } catch (e) {
    const el = $("#doc-text");
    if (el) el.textContent = "无法显示原文：" + e.message;
  }
}
function goPage(n) {
  const pages = Math.max(1, Math.ceil(state.docs.total / state.docs.size));
  state.docs.page = Math.min(Math.max(1, n), pages);
  loadDocs();
}
$("#btn-refresh").addEventListener("click", () => {
  loadStats();
  if (state.boardView === "stats") renderBoardBody(); else loadDocs();
});
$$("#status-tabs .tab").forEach(btn => btn.addEventListener("click", () => {
  state.filters.status = btn.dataset.status || "";
  state.filters.tier2 = 0;
  state.docs.page = 1;
  state.boardView = "docs";
  markDocFilter();
  renderBoardBody();
  loadDocs();
}));
$("#f-share").addEventListener("change", e => { state.filters.share = e.target.value.trim(); state.docs.page = 1; loadDocs(); });
$("#f-share").addEventListener("keydown", e => { if (e.key === "Enter") { e.target.dispatchEvent(new Event("change")); } });
$("#pg-size").addEventListener("change", e => { state.docs.size = parseInt(e.target.value, 10) || 50; state.docs.page = 1; loadDocs(); });
$("#pg-first").addEventListener("click", () => goPage(1));
$("#pg-prev").addEventListener("click", () => goPage(state.docs.page - 1));
$("#pg-next").addEventListener("click", () => goPage(state.docs.page + 1));
$("#pg-last").addEventListener("click", () => goPage(Number.MAX_SAFE_INTEGER));

async function showDoc(id) {
  try {
    const d = await api("/api/v1/documents/" + id);
    const kps = (d.knowledge || []).flatMap(k => (k.key_points || []).map(x =>
      `<li><b>${esc(x.title)}</b> <code>${esc(x.category || "")}</code><div class="muted">${esc(x.detail || "")}</div></li>`)).join("");
    openModal(`<h3>${esc(d.name)}</h3>
      <div class="kv">路径: ${esc(d.path)}</div>
      <div class="kv">状态: ${statusText(d.status)} ${d.status_detail ? "— " + esc(d.status_detail) : ""}</div>
      ${d.status === "skipped"
        ? `<div class="src-hint" style="margin:8px 0">「已跳过」不是文件被删了：
             ${d.needs_ocr
               ? "这是<b>扫描件</b>（PDF 没有文字层），平台默认不对存量扫描件自动 OCR（太慢），文件本身在 NAS 上完好，可以正常打开/下载。"
               : "如果是源文件被删，索引与图谱产物已清理。"}
             ${d.needs_ocr ? "<br>点「重新解析」可以手动为它跑一次 OCR。" : ""}</div>`
        : ""}
      <div class="kv">文本块 ${d.chunk_count || 0} · 知识点 ${d.kp_count || 0} · Tier1 ${d.tier1_done ? "✓" : "—"} · Tier2 ${d.tier2_done ? "✓" : "—"}</div>
      <div class="src-actions">
        <button class="sm" onclick="showDoc(${id})">刷新</button>
        ${(d.status === "skipped" || d.status === "failed")
          ? `<button class="sm" onclick="reparseDoc(${id})">重新解析</button>` : ""}
      </div>
      <hr style="border-color:#2a333d">
      <h4>提炼出的知识点</h4><ul>${kps || "<li class='muted'>尚未提炼</li>"}</ul>`);
  } catch (e) { toast("加载文档详情失败: " + e.message); }
}

/* ---------------------------------------------------------- 图谱 */
let cyReady = false;
let autoFit = true;          // 布局结束后是否自动缩放到全图

// 布局参数：节点多、关系密，默认 cose 参数会把节点挤成一团、标签互相压盖。
// 加大斥力与边长、降低重力、开启 tile（密集图更快更整齐），
// 并让布局按"含标签的节点尺寸"计算斥力，标签才不会互相压。
const GRAPH_LAYOUT = {
  name: "cose", animate: false, padding: 40, tile: true,
  nodeRepulsion: (node) => 14000 + 4000 * Math.min(6, node.degree()),
  idealEdgeLength: 150, edgeElasticity: 0.45, gravity: 0.15,
  numIter: 1500, componentSpacing: 120, refresh: 20,
  nodeDimensionsIncludeLabels: true,
};

/** 屏幕上节点的平均最近邻间距（px）。 */
function measureAvgScreenGap() {
  const cy = state.cy;
  if (!cy) return 999;
  const nodes = cy.nodes();
  if (nodes.length < 2) return 999;
  const pos = nodes.map((n) => n.renderedPosition());
  let sum = 0, cnt = 0;
  for (let i = 0; i < pos.length; i++) {
    let best = Infinity;
    for (let j = 0; j < pos.length; j++) {
      if (i === j) continue;
      const dx = pos[i].x - pos[j].x, dy = pos[i].y - pos[j].y;
      const d = Math.sqrt(dx * dx + dy * dy);
      if (d < best) best = d;
    }
    if (best < Infinity) { sum += best; cnt++; }
  }
  return cnt ? sum / cnt : 999;
}

/** 目标屏幕间距：至少 26px，且不小于节点平均直径的 1.4 倍（保证圆点不压圆点）。 */
function targetGap() {
  const cy = state.cy;
  if (!cy) return 26;
  const zoom = cy.zoom();
  let sum = 0, n = 0;
  cy.nodes().forEach((nd) => { sum += (nd.data("size") || 16) * zoom; n++; });
  const avgDia = n ? sum / n : 16;
  return Math.max(26, Math.round(avgDia * 1.4));
}

/** 屏幕上节点是否挤在一起了（平均最近邻屏幕间距小于目标）。 */
function graphTooDense() {
  return measureAvgScreenGap() < targetGap();
}

/** 布局后处理：强制节点之间的最小间距（硬约束）。
 *  等比放大布局并不能提高"节点相对间距"（间距和缩放同步变化），
 *  真正让节点不压在一起的办法是在模型坐标里把它们推开：
 *  这样无论怎么缩放，圆点都不会重叠。 */
function enforceMinDistance() {
  const cy = state.cy;
  if (!cy) return;
  const nodes = cy.nodes();
  if (nodes.length < 2) return;
  const radius = (n) => (n.data("size") || 16) / 2;
  for (let iter = 0; iter < 80; iter++) {
    let moved = 0;
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        const pa = a.position(), pb = b.position();
        let dx = pb.x - pa.x, dy = pb.y - pa.y;
        let d = Math.sqrt(dx * dx + dy * dy);
        if (d < 1e-6) { dx = (i % 7) - 3; dy = (j % 5) - 2; d = Math.sqrt(dx * dx + dy * dy); }
        const need = 1.5 * (radius(a) + radius(b));   // 留出节点自身与标签的余量
        if (d < need) {
          const push = (need - d) / 2;
          const ux = dx / d, uy = dy / d;
          a.position({ x: pa.x - ux * push, y: pa.y - uy * push });
          b.position({ x: pb.x + ux * push, y: pb.y + uy * push });
          moved++;
        }
      }
    }
    if (!moved) break;
  }
}

/** 布局的长宽比常常和画布差很多（竖长条塞进横窗口），白白浪费画布、节点挤成一团。
 *  这里把"瘦的那一维"拉宽，让图形铺满画布，节点在屏幕上就分得开。
 *  注意方向：布局比画布**瘦**（layoutAspect < viewAspect）时要拉宽 x，而不是压窄 x。 */
function fitAspectToCanvas() {
  const cy = state.cy;
  if (!cy || !cy.nodes().length) return;
  const bb = cy.elements().boundingBox();
  const W = cy.width(), H = cy.height();
  if (!bb || !bb.w || !bb.h || !W || !H) return;
  const k = W / H / (bb.w / bb.h);        // >1 表示布局偏瘦
  if (k > 1.12) {
    const s = Math.min(2.0, k);
    cy.nodes().forEach((n) => { const p = n.position(); n.position({ x: p.x * s, y: p.y }); });
  } else if (k < 0.89) {
    const s = Math.min(2.0, 1 / k);
    cy.nodes().forEach((n) => { const p = n.position(); n.position({ x: p.x, y: p.y * s }); });
  }
}

/** 节点直径：随图里节点总数自适应缩放。
 *  点越多，圈越小（按 sqrt 反比），这样整张图"墨量"恒定，看着均衡；
 *  同时重要（提及多）的节点略大一点。 */
function graphNodeSize(mentions, total) {
  const base = 7 + Math.min(6, mentions || 0);
  const k = Math.sqrt(60 / Math.max(20, total || 60));
  return Math.max(4, Math.min(16, Math.round(base * k)));
}

/** 重新排布：斥力调大让图收成一团（而不是拉成长条），再贴合到画布。
 *  只跑一次 cose 收敛 + 一次最小间距松弛，不做循环，保证节点稳定不抖。 */
function relayout(animate) {
  const cy = state.cy;
  if (!cy || !cy.nodes().length) return;
  cy.resize();
  const W = cy.width(), H = cy.height();
  const k = Math.min(2.0, Math.max(0.75, Math.sqrt((W * H) / (900 * 600))));
  cy.layout({
    ...GRAPH_LAYOUT,
    animate: animate === true,
    animationDuration: 300,
    randomize: animate === true ? false : undefined,
    idealEdgeLength: Math.round(120 * k),
    nodeRepulsion: (node) => Math.round((16000 + 4000 * Math.min(6, node.degree())) * k),
  }).run();
  enforceMinDistance();
  fitAspectToCanvas();
  fitGraph();
  declutterLabels();
}

/** 标签去重叠：按重要度贪心放置，装不下的标签先藏起来（悬停/选中的始终显示）。 */
function declutterLabels() {
  const cy = state.cy;
  if (!cy) return;
  const zoom = cy.zoom();
  const fs = 11;
  const W = cy.width(), H = cy.height();
  const placed = [];
  const nodes = Array.prototype.slice.call(
    cy.nodes().filter((n) => n.visible()));
  nodes.sort((a, b) => {
    const sa = a.data("size") || 0, sb = b.data("size") || 0;
    if (sa !== sb) return sb - sa;                 // 重要节点优先拿到标签
    return b.degree() - a.degree();
  });
  nodes.forEach((n) => {
    const label = n.data("label") || "";
    const p = n.renderedPosition();
    if (!label || p.x < -120 || p.x > W + 120 || p.y < -120 || p.y > H + 120) {
      n.removeClass("nolabel");
      return;
    }
    const w = Math.min(130, label.length * fs * 0.62) * zoom;
    const h = (fs + 6) * zoom;
    const box = { x1: p.x - w / 2, x2: p.x + w / 2, y1: p.y + 4 * zoom, y2: p.y + 4 * zoom + h };
    const hit = placed.some((b) => b.x1 < box.x2 && box.x1 < b.x2 && b.y1 < box.y2 && box.y1 < b.y2);
    if (hit) n.addClass("nolabel");
    else { n.removeClass("nolabel"); placed.push(box); }
  });
}

function graphStyle(showRelLabels) {
  return [
    { selector: "node", style: {
        "background-color": "#4a9eff", "label": "data(label)", "color": "#e6edf3",
        "font-size": 11, "text-valign": "bottom", "text-margin-y": 6,
        "text-wrap": "ellipsis", "text-max-width": 130,
        "text-background-color": "#0f1419", "text-background-opacity": 0.75,
        "text-background-padding": "2px", "text-background-shape": "roundrectangle",
        // 缩得太小时直接不画标签，避免一坨字糊在一起
        "min-zoomed-font-size": 9,
        "width": "data(size)", "height": "data(size)",
        "border-width": 1, "border-color": "#2a333d" } },
    { selector: "node[type='knowledge_point']", style: { "background-color": "#3fb950" } },
    { selector: "node[type='category']", style: { "background-color": "#a371f7" } },
    { selector: "node[type='document']", style: { "background-color": "#d29922", "shape": "round-rectangle" } },
    { selector: "node[?restricted]", style: { "border-color": "#f85149", "border-width": 2 } },
    { selector: "node:selected", style: { "border-color": "#fff", "border-width": 3 } },
    { selector: "node:active", style: { "border-color": "#fff", "border-width": 3 } },
    { selector: "edge", style: { "width": 1, "line-color": "#3a4652", "curve-style": "straight",
        "target-arrow-shape": "triangle", "target-arrow-color": "#3a4652",
        "label": showRelLabels ? "data(rel)" : "", "font-size": 9, "color": "#c9d4df",
        "text-rotation": "autorotate", "text-background-color": "#0f1419",
        "text-background-opacity": 0.8, "text-background-padding": "2px" } },
    // 悬停/选中时才显示这条关系叫什么
    { selector: "edge.hot", style: { "label": "data(rel)", "line-color": "#58a6ff",
        "target-arrow-color": "#58a6ff", "width": 1.8,
        "text-background-color": "#0f1419", "text-background-opacity": 0.95 } },
    { selector: "node.dim", style: { "opacity": 0.25 } },
    { selector: "edge.dim", style: { "opacity": 0.12 } },
    // 去重叠时暂时藏掉这个节点的标签
    { selector: "node.nolabel", style: { "label": "" } },
    // 悬停/选中的节点一定要能看清名字
    { selector: "node:active", style: { "label": "data(label)" } },
    { selector: "node:selected", style: { "label": "data(label)" } },
  ];
}

/** 把全部节点缩放到可视范围内（自适应窗口大小）。 */
function fitGraph(animate) {
  const cy = state.cy;
  if (!cy) return;
  cy.resize();
  const els = cy.elements();
  if (!els.length) return;
  cy.fit(els, 36, animate === false ? 0 : 200);
}

function initGraph() {
  if (state.cy) { fitGraph(); return; }
  state.cy = cytoscape({
    container: $("#cy"),
    elements: [],
    layout: GRAPH_LAYOUT,
    style: graphStyle(false),
    wheelSensitivity: 0.25,
  });
  const cy = state.cy;
  cyReady = true;

  // 关系文字默认不画，鼠标悬停/选中时才显示对应那条
  cy.on("mouseover", "edge", (e) => e.target.addClass("hot"));
  cy.on("mouseout", "edge", (e) => e.target.removeClass("hot"));
  cy.on("tap", "node", (e) => showNode(e.target.id()));
  cy.on("layoutstop", () => {
    if (autoFit) fitGraph();
    declutterLabels();
  });

  // 悬停的节点永远显示名字
  cy.on("mouseover", "node", (e) => e.target.removeClass("nolabel"));
  cy.on("mouseout", "node", () => declutterLabels());

  // 缩放/平移：只重新去重叠，**不重排**。
  // （早先这里会"太挤就重排"，而 fit 会触发 zoom 事件 → 重排 → fit → zoom → …
  //   形成自激循环，节点一直抖。现在布局只在加载/窗口变化/手动点击时发生。）
  let dcTimer = 0;
  const scheduleDeclutter = () => {
    clearTimeout(dcTimer);
    dcTimer = setTimeout(() => { if (state.cy) declutterLabels(); }, 120);
  };
  cy.on("zoom", scheduleDeclutter);
  cy.on("pan", scheduleDeclutter);

  // 画布尺寸真的变了才重排（ResizeObserver 会重复触发，用尺寸去重防抖）
  let rzTimer = 0, lastSize = "";
  const refit = () => {
    clearTimeout(rzTimer);
    rzTimer = setTimeout(() => {
      if (!state.cy || !state.cy.nodes().length) return;
      const key = state.cy.width() + "x" + state.cy.height();
      if (key === lastSize) return;       // 尺寸没变就不动，节点保持稳定
      lastSize = key;
      autoFit = true;
      relayout(true);
    }, 260);
  };
  window.addEventListener("resize", refit);
  // 容器自身尺寸变化（侧栏展开、工具栏换行、浏览器页面缩放）同样要重排
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => refit());
    ro.observe($("#cy"));
  }

  loadGraphOverview();
}

async function loadGraphOverview() {
  const cy = state.cy;
  if (!cy) return;
  try {
    const r = await api("/api/v1/graph/overview?limit=60");
    cy.elements().remove();
    const ids = new Set(r.nodes.map(n => String(n.id)));
    const total = r.nodes.length;
    const els = r.nodes.map(n => ({ data: {
      id: String(n.id), label: n.name, type: n.type,
      size: graphNodeSize(n.mentions || 0, total) } }));
    for (const e of r.edges) {
      if (!ids.has(String(e.src)) || !ids.has(String(e.dst))) continue;
      els.push({ data: { id: e.src + "->" + e.dst + "|" + e.rel,
                         source: String(e.src), target: String(e.dst), rel: e.rel } });
    }
    cy.add(els);
    autoFit = true;
    cy.layout(GRAPH_LAYOUT).run();
    enforceMinDistance();
    fitAspectToCanvas();
    fitGraph();
    declutterLabels();
    $("#g-side").innerHTML = r.nodes.length
      ? `<h3>知识图谱</h3>
         <div class="kv">已加载 ${r.nodes.length} 个高频节点、${r.edges.length} 条关系</div>
         ${state.role === "admin" ? "" : '<div class="kv">只显示你授权目录内的图谱</div>'}
         <div class="kv">点击节点查看详情，或用上方搜索定位</div>`
      : `<h3>知识图谱</h3>
         ${state.role === "admin"
            ? '<div class="muted">还没有图谱数据，Tier 2 提炼完成后会自动出现。</div>'
            : '<div class="muted">你的授权目录里还没有图谱数据。<br>授权更多目录，或等文档解析提炼完成。</div>'}`;
  } catch (e) { toast("图谱加载失败: " + e.message); }
}

async function searchNodes() {
  const q = $("#g-q").value.trim();
  if (!q) return;
  try {
    const r = await api("/api/v1/graph/search?q=" + encodeURIComponent(q) + "&limit=30");
    const types = [];
    if ($("#g-entity").checked) types.push("entity");
    if ($("#g-kp").checked) types.push("knowledge_point");
    if ($("#g-cat").checked) types.push("category");
    const list = r.nodes.filter(n => types.includes(n.type));
    $("#g-list").innerHTML = list.length
      ? list.map(n => `<span class="chip" onclick="centerNode(${n.id})">${esc(n.name)} <span class="muted">(${n.type})</span></span>`).join("")
      : '<span class="muted">没有匹配的节点</span>';
    if (list.length) { await expandNode(list[0].id, true); }
  } catch (e) { toast("图谱搜索失败: " + e.message); }
}
$("#g-search").addEventListener("click", searchNodes);
$("#g-refresh").addEventListener("click", loadGraphOverview);
$("#g-q").addEventListener("keydown", e => { if (e.key === "Enter") searchNodes(); });
$("#g-fit").addEventListener("click", () => { fitGraph(); declutterLabels(); });
$("#g-layout").addEventListener("click", () => relayout(true));
$("#g-zoom-in").addEventListener("click", () => {
  const cy = state.cy; if (!cy) return;
  cy.resize();
  cy.zoom({ level: Math.min(4, cy.zoom() * 1.25), renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  declutterLabels();
});
$("#g-zoom-out").addEventListener("click", () => {
  const cy = state.cy; if (!cy) return;
  cy.resize();
  cy.zoom({ level: Math.max(0.05, cy.zoom() / 1.25), renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  declutterLabels();
});
$("#g-rel-label").addEventListener("change", (e) => {
  const cy = state.cy; if (!cy) return;
  cy.style(graphStyle(e.target.checked));
});

async function expandNode(id, replace) {
  try {
    const r = await api("/api/v1/graph/neighbors?node=" + id + "&hops=1");
    const cy = state.cy;
    if (replace) cy.elements().remove();
    const existing = new Set(cy.nodes().map(n => n.id()));
    const els = [];
    const total = cy.nodes().length + r.nodes.length;
    for (const n of r.nodes) {
      if (!existing.has(String(n.id))) {
        els.push({ data: { id: String(n.id), label: n.name, type: n.type,
                           size: graphNodeSize(n.mentions || 0, total) } });
      }
    }
    for (const e of r.edges) {
      els.push({ data: { id: e.src + "->" + e.dst + "|" + e.rel, source: String(e.src), target: String(e.dst), rel: e.rel } });
    }
    cy.add(els);
    cy.layout({ ...GRAPH_LAYOUT, animate: true, animationDuration: 400 }).run();
    enforceMinDistance();
    declutterLabels();
    const node = cy.getElementById(String(id));
    if (node.length) cy.center(node);
    showNode(id);
  } catch (e) { toast("展开节点失败: " + e.message); }
}
window.centerNode = (id) => expandNode(id, false);

async function showNode(id) {
  try {
    const r = await api("/api/v1/graph/neighbors?node=" + id + "&hops=1");
    const self = r.nodes.find(n => String(n.id) === String(id));
    if (!self) return;
    const rels = r.edges.filter(e => String(e.src) === String(id) || String(e.dst) === String(id));
    const relHtml = rels.map(e => {
      const other = String(e.src) === String(id) ? e.dst : e.src;
      const otherName = (r.nodes.find(n => String(n.id) === String(other)) || {}).name || other;
      return `<div class="rel">--${esc(e.rel)}--&rarr; <a href="#" onclick="centerNode(${other});return false">${esc(otherName)}</a></div>`;
    }).join("") || '<div class="muted">没有关系</div>';
    $("#g-side").innerHTML = `<h3>${esc(self.name)}</h3>
      <div class="kv">类型: ${esc(self.type)}</div>
      <div class="kv">被提及: ${self.mentions} 次</div>
      <div class="kv">节点 ID: <code>${self.id}</code></div>
      <hr style="border-color:#2a333d"><h4>关系</h4>${relHtml}`;
  } catch (e) { /* 忽略 */ }
}

/* ---------------------------------------------------------- 问答 */
async function loadModels() {
  try {
    const r = await api("/api/v1/models?only_usable=true");
    state.models = r.models;
    const sel = $("#a-model");
    const cur = sel.value;
    sel.innerHTML = r.models.length
      ? r.models.map(m => `<option value="${m.id}">${esc(m.label || m.name)}</option>`).join("")
      : '<option value="">（没有可用模型，请让管理员配置）</option>';
    if (cur) sel.value = cur;
    if (!sel.value) {
      const def = r.models.find(m => m.is_default) || r.models[0];
      if (def) sel.value = def.id;
    }
  } catch (e) { /* 未登录时忽略 */ }
}
$("#a-go").addEventListener("click", ask);
$("#a-q").addEventListener("keydown", e => { if (e.key === "Enter") ask(); });
$$(".ask-samples a").forEach(a => a.addEventListener("click", e => {
  e.preventDefault(); $("#a-q").value = a.dataset.q; ask();
}));
async function ask() {
  const q = $("#a-q").value.trim();
  if (!q) return;
  const btn = $("#a-go"); btn.disabled = true; btn.textContent = "思考中…";
  $("#a-notice").classList.add("hidden");
  $("#a-feedback").classList.add("hidden");
  try {
    const r = await api("/api/v1/query", { method: "POST", body: { question: q, model: $("#a-model").value, top_k: 8 } });
    $("#a-answer").textContent = r.answer || "(无输出)";
    if (r.restricted) {
      const n = $("#a-notice");
      n.textContent = r.restricted_notice || "本次回答命中涉密内容，已锁定本地模型且未回传片段原文";
      n.classList.remove("hidden");
    }
    // 答案反馈（Task 0.3）：request_id 要原样回传，后台轨迹靠它对上号
    renderAskFeedback(r.request_id);
    const box = $("#a-cites"); box.innerHTML = "";
    (r.citations || []).forEach((c, i) => {
      const d = document.createElement("div");
      d.className = "cite" + (c.restricted ? " restricted" : "");
      d.innerHTML = `<div><span class="f">[片段${i + 1}] ${esc(c.file)}</span><span class="loc">位置 ${esc(c.locator)}</span></div>
        <div class="tags">${(c.matched_by || []).map(x => `<span class="tag">${esc(x)}</span>`).join("")}
        ${c.restricted ? '<span class="tag">涉密·原文不回传</span>' : ""}</div>
        ${c.snippet ? `<div class="sn">${esc(c.snippet)}</div>` : ""}`;
      d.addEventListener("contextmenu", (ev) => {
        ev.preventDefault();
        showCiteMenu(ev, c);
      });
      box.appendChild(d);
    });
  } catch (e) { toast("提问失败: " + e.message); }
  finally { btn.disabled = false; btn.textContent = "提问"; }
}

/* ---------------------------------------------------------- 问答反馈 */
/* 答案下方的三个反馈按钮：点一次就记一条，成功后整排置灰。 */
function renderAskFeedback(requestId) {
  const fb = $("#a-feedback");
  if (!fb) return;
  if (!requestId) { fb.classList.add("hidden"); return; }
  fb.classList.remove("hidden");
  fb.innerHTML = `
    <span class="muted">这个答案怎么样？</span>
    <button class="sm" data-rating="up">👍 有用</button>
    <button class="sm" data-rating="down">👎 没用</button>
    <button class="sm" data-rating="wrong_cite">引用不对</button>
    <span id="a-fb-msg" class="muted"></span>`;
  fb.querySelectorAll("button").forEach(b => {
    b.addEventListener("click", async () => {
      fb.querySelectorAll("button").forEach(x => x.disabled = true);
      const msg = $("#a-fb-msg");
      try {
        await api(`/api/v1/query/${requestId}/feedback`,
                  { method: "POST", body: { rating: b.dataset.rating } });
        if (msg) msg.textContent = "已记录，谢谢";
      } catch (e) {
        if (msg) msg.textContent = "提交失败: " + e.message;
        fb.querySelectorAll("button").forEach(x => x.disabled = false);
      }
    });
  });
}

/* 问答引用卡片右键菜单：打开附件所在文件路径 / 查看 / 下载 / 复制路径 */
function showCiteMenu(ev, c) {
  const menu = $("#cite-ctx");
  if (!menu) return;
  menu.innerHTML = `
    <button data-act="locate">打开附件所在文件路径</button>
    <button data-act="view">查看文件</button>
    <button data-act="download">下载文件</button>
    <button data-act="copy">复制文件路径</button>`;
  menu.classList.remove("hidden");
  menu.style.left = Math.min(ev.clientX, window.innerWidth - 220) + "px";
  menu.style.top = Math.min(ev.clientY, window.innerHeight - 180) + "px";
  let info = null;
  menu.querySelectorAll("button").forEach((b) => {
    b.onclick = async () => {
      const act = b.dataset.act;
      hideCiteMenu();
      try {
        if (!info && c.document_id) {
          info = await api(`/api/v1/documents/${c.document_id}/file`);
        }
        if (act === "locate" || act === "view") {
          if (info && info.in_fs) {
            location.hash = "#/files";
            await new Promise((r) => setTimeout(r, 60));
            await window.KB.files.openAt(info.source_id, info.rel, act === "view");
          } else if (act === "view") {
            openDocText({ id: c.document_id, name: c.file }, { path: c.path });
          } else {
            toast("这个文件还没有接入 2.0 文件页，先在「存储接入」里接入对应共享");
          }
        } else if (act === "download") {
          const url = (info && info.in_fs) ? info.download_url : c.download_url;
          const a = document.createElement("a");
          a.href = url;
          a.download = c.file;
          document.body.appendChild(a);
          a.click();
          a.remove();
        } else if (act === "copy") {
          const text = c.path || "";
          if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast("路径已复制")).catch(() => toast(text));
          else toast(text);
        }
      } catch (e) { toast("操作失败: " + e.message, 5000); }
    };
  });
}
function hideCiteMenu() {
  const menu = $("#cite-ctx");
  if (menu) menu.classList.add("hidden");
}
document.addEventListener("click", (e) => {
  if (!e.target.closest || !e.target.closest("#cite-ctx")) hideCiteMenu();
});

/* ---------------------------------------------------------- 管理台 */
$$("#p-admin .tabs button").forEach(b => b.addEventListener("click", () => {
  setAdminTab(b.dataset.t);
  loadAdmin();
}));

async function loadAdmin() {
  const body = $("#admin-body");
  body.innerHTML = '<p class="muted">加载中…</p>';
  // 普通用户没有管理员专属页签，落在这些页签上时回退到「API 密钥」
  const adminOnlyTabs = ["users", "sources", "models", "restricted", "traces"];
  if (state.role !== "admin" && adminOnlyTabs.includes(state.adminTab)) {
    state.adminTab = "tokens";
  }
  try {
    if (state.adminTab === "tokens") return adminTokens(body);
    if (state.adminTab === "agents") return adminAgents(body);
    if (state.adminTab === "audit") return adminAudit(body);
    if (state.adminTab === "users") return adminUsers(body);
    if (state.adminTab === "sources") return KB.adminSources.render(body);
    if (state.adminTab === "models") return adminModels(body);
    if (state.adminTab === "restricted") return adminRestricted(body);
    if (state.adminTab === "traces") return adminTraces(body);
  } catch (e) { body.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

async function adminUsers(body) {
  const [r, tok] = await Promise.all([
    api("/api/v1/admin/users"),
    api("/api/v1/admin/tokens"),
  ]);
  const cnt = {};
  for (const t of tok.tokens) cnt[t.user_id] = (cnt[t.user_id] || 0) + 1;
  state.tokenCounts = cnt;
  state.adminUsers = r.users || [];
  const nd = state.newUserDraft || (state.newUserDraft = {});
  body.innerHTML = `
    <div class="form-row">
      <label>用户名<input id="nu-name" class="inline" value="${esc(nd.username || "")}"></label>
      <label>显示名<input id="nu-disp" class="inline" value="${esc(nd.display_name || "")}"></label>
      <label>初始密码<input id="nu-pass" class="inline" type="password" value="${esc(nd.password || "")}"></label>
      <label>角色<select id="nu-role" class="inline">
        <option value="user"${nd.role === "admin" ? "" : " selected"}>普通用户</option>
        <option value="admin"${nd.role === "admin" ? " selected" : ""}>管理员</option></select></label>
      <button class="primary" onclick="createUser()">新建用户</button>
    </div>
    <div class="form-row">
      <label>初始可见范围（可选）· 共享<input id="ns-share" class="inline" placeholder="如 信息化管理" value="${esc(nd.share || "")}"></label>
      <label>子目录<input id="ns-sub" class="inline" placeholder="留空=整共享" value="${esc(nd.sub || "")}"></label>
      <span class="muted">建号后立刻生效：文件页与文档看板同一套范围</span>
    </div>
    <table><thead><tr><th>ID</th><th>用户名</th><th>显示名</th><th>角色</th><th>可见范围</th><th>状态</th><th>操作</th></tr></thead><tbody>
    ${r.users.map(u => `<tr>
      <td>${u.id}</td><td>${esc(u.username)}</td><td>${esc(u.display_name || "")}</td>
      <td>${esc(u.role)}</td>
      <td>${(u.scopes || []).map(s => `<span class="scope-chip">${esc(s)}</span>`).join("") ||
            (u.role === "admin" ? '<span class="muted">（全部）</span>'
                                 : '<span class="muted">（未授权）</span>')}</td>
      <td>${u.active ? '<span class="muted">启用</span>' : '<span class="err">已停用</span>'}</td>
      <td><button class="sm" onclick="editUser(${u.id})">编辑</button>
         <button class="sm" onclick="KB.adminSources.openGrants(${u.id})">目录授权</button>
         <button class="sm" onclick="grantUser(${u.id})">旧范围</button>
         <button class="sm" onclick="filterTokensOf(${u.id})">密钥 ${tokenCount(u.id)}</button>
         <button class="sm" onclick="toggleActive(${u.id},${u.active ? 0 : 1})">${u.active ? "停用" : "启用"}</button></td></tr>`).join("")}
    </tbody></table>`;
  // 表单输入随时存草稿，避免面板重渲染把管理员填了一半的内容冲掉
  ["nu-name", "nu-disp", "nu-pass", "nu-role", "ns-share", "ns-sub"].forEach((id) => {
    const el = $("#" + id);
    if (el) el.addEventListener("input", saveNewUserDraft);
  });
}

/** 把建号表单当前内容存进草稿（面板重渲染后据此恢复）。 */
window.saveNewUserDraft = () => {
  const g = (id) => { const el = $("#" + id); return el ? el.value : ""; };
  state.newUserDraft = {
    username: g("nu-name"), display_name: g("nu-disp"), password: g("nu-pass"),
    role: g("nu-role"), share: g("ns-share"), sub: g("ns-sub"),
  };
};

/* 修改用户信息：用户名 / 显示名 / 角色 / 部门 / 启用状态，可选重置密码 */
window.editUser = (id) => {
  const u = (state.adminUsers || []).find((x) => x.id === id);
  if (!u) { toast("找不到该用户"); return; }
  openModal(`<h3 style="margin:0 0 10px">修改用户信息</h3>
    <p class="muted" style="font-size:12px">用户名可以改（改完用新名字登录），用户 ID 不可修改。</p>
    <div class="src-grid">
      <label>用户 ID<input value="${u.id}" disabled></label>
      <label>用户名<input id="eu-name" value="${esc(u.username)}" placeholder="登录用的账号"></label>
      <label>显示名<input id="eu-disp" value="${esc(u.display_name || "")}"></label>
      <label>部门<input id="eu-dept" value="${esc(u.dept || "")}"></label>
      <label>角色<select id="eu-role">
        <option value="user" ${u.role === "user" ? "selected" : ""}>普通用户</option>
        <option value="admin" ${u.role === "admin" ? "selected" : ""}>管理员</option>
      </select></label>
      <label>状态<select id="eu-active">
        <option value="1" ${u.active ? "selected" : ""}>启用</option>
        <option value="0" ${u.active ? "" : "selected"}>停用</option>
      </select></label>
      <label>重置密码（留空=不改）<input id="eu-pass" type="password" placeholder="至少 8 位"></label>
    </div>
    <div class="src-actions">
      <button id="eu-save" class="primary">保存</button>
      <span id="eu-msg" class="err"></span>
    </div>`);
  $("#eu-save").onclick = async () => {
    const btn = $("#eu-save");
    btn.disabled = true;
    const body = {
      username: $("#eu-name").value.trim(),
      display_name: $("#eu-disp").value.trim(),
      dept: $("#eu-dept").value.trim(),
      role: $("#eu-role").value,
      active: $("#eu-active").value === "1",
    };
    const pwd = $("#eu-pass").value;
    if (pwd) body.password = pwd;
    try {
      await api(`/api/v1/admin/users/${id}`, { method: "PUT", body });
      closeModal();
      toast("已保存");
      loadAdmin();
    } catch (e) {
      const msg = $("#eu-msg");
      if (msg) msg.textContent = e.message;
    } finally { btn.disabled = false; }
  };
};
window.createUser = async () => {
  saveNewUserDraft();
  const d = state.newUserDraft;
  const username = (d.username || "").trim();
  const password = d.password || "";
  const focus = (id) => { const el = $("#" + id); if (el) el.focus(); };
  // 就地校验：不给一个必然失败的请求去浪费一次往返
  if (!username) { toast("请填写用户名"); focus("nu-name"); return; }
  if (/\s/.test(username) || username.length > 64) {
    toast("用户名不能包含空格，且不超过 64 个字符"); focus("nu-name"); return;
  }
  if (!password) { toast("请填写初始密码"); focus("nu-pass"); return; }
  if (password.length < 8) { toast("初始密码至少 8 位"); focus("nu-pass"); return; }
  const share = (d.share || "").trim();
  const scopes = share ? [[share, (d.sub || "").trim()]] : [];
  try {
    await api("/api/v1/admin/users", { method: "POST", body: {
      username: username, password: password, role: d.role || "user",
      display_name: (d.display_name || "").trim(), scopes: scopes } });
    state.newUserDraft = {};
    toast(scopes.length ? "用户已创建，并已授予初始可见范围" : "用户已创建");
    loadAdmin();
  } catch (e) { toast("创建失败: " + e.message); }
};
window.toggleActive = async (id, active) => {
  try {
    await api(`/api/v1/admin/users/${id}/active`, { method: "PUT", body: { active: !!active } });
    toast(active ? "已启用" : "已停用"); loadAdmin();
  } catch (e) { toast("操作失败: " + e.message); }
};
window.grantUser = async (id) => {
  let tree;
  try {
    tree = await api("/api/v1/admin/scopes/tree");
  } catch (e) {
    toast("加载目录清单失败: " + e.message);
    return;
  }
  const cur = await api("/api/v1/admin/users");
  const u = (cur.users || []).find(x => x.id === id) || {};
  const granted = new Set((u.scopes || []).map(s => s));

  openModal(`<h3>授权可见目录 — ${esc(u.display_name || u.username || ("用户#" + id))}</h3>
    <p class="muted">先选共享，再勾选要开放的子目录。勾选「整个共享」= 该共享下所有内容都可见。</p>
    <div class="form-row" style="margin:12px 0">
      <label>共享<select id="sc-share" onchange="renderScopeDirs()">${
        tree.shares.map(s => `<option value="${esc(s.name)}">${esc(s.name)}（${s.files} 个已索引文件）</option>`).join("")
      }</select></label>
      <label>当前已授权<input id="sc-current" class="inline" value="${esc((u.scopes || []).join(" ; ") || "（无）")}" readonly></label>
    </div>
    <div id="sc-dirs" style="max-height:260px;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:8px"></div>
    <div id="sc-err" class="err"></div>
    <div class="modal-foot" style="display:flex;gap:8px;justify-content:space-between;align-items:center">
      <span class="muted" id="sc-count"></span>
      <span>
        <button onclick="closeModal()">取消</button>
        <button class="primary" onclick="saveScopes(${id})">保存授权</button>
      </span>
    </div>`);
  state.scopeTree = tree;
  state.scopeUserId = id;
  state.scopeSelected = new Set(granted);
  renderScopeDirs();
};
window.renderScopeDirs = () => {
  const share = $("#sc-share").value;
  const s = (state.scopeTree.shares || []).find(x => x.name === share) || { subdirs: [] };
  const box = $("#sc-dirs");
  const cur = state.scopeSelected;
  const all = cur.has(share);
  const R = '<span class="tag" style="background:#4a1d1d;color:#f85149;border:1px solid #5c2222">涉密</span>';
  let html = `<label class="chk" style="padding:5px 6px;border-bottom:1px solid var(--line)">
      <input type="checkbox" id="sc-all" ${all ? "checked" : ""} onchange="toggleScopeAll(${JSON.stringify(share).replace(/"/g, "&quot;")})">
      <b>整个共享（${esc(share)}）</b> ${s.restricted ? R : ""}
      <span class="muted">— 该共享下所有文件</span></label>`;
  if (!s.subdirs.length) {
    html += `<div class="muted" style="padding:10px">该共享下没有可授权的子目录。勾选上面的「整个共享」即可授权全部。</div>`;
  }
  for (const d of s.subdirs) {
    const key = share + "\\" + d.path;
    const on = cur.has(key);
    html += `<label class="chk" style="padding:4px 6px">
      <input type="checkbox" ${on ? "checked" : ""} onchange="toggleScopeDir(${JSON.stringify(key).replace(/"/g, "&quot;")})">
      <span>${esc(d.path)}</span> ${d.restricted ? R : ""}
      ${d.files ? `<span class="muted">（${d.files} 个已索引文件）</span>` : '<span class="muted">（未索引）</span>'}</label>`;
  }
  box.innerHTML = html;
  updateScopeCount();
};
window.toggleScopeAll = (share) => {
  const on = $("#sc-all").checked;
  const s = (state.scopeTree.shares || []).find(x => x.name === share) || { subdirs: [] };
  if (on) {
    state.scopeSelected.add(share);
    for (const d of s.subdirs) state.scopeSelected.add(share + "\\" + d.path);
  } else {
    state.scopeSelected.delete(share);
    for (const d of s.subdirs) state.scopeSelected.delete(share + "\\" + d.path);
  }
  renderScopeDirs();
};
window.toggleScopeDir = (key) => {
  if (state.scopeSelected.has(key)) state.scopeSelected.delete(key);
  else state.scopeSelected.add(key);
  updateScopeCount();
};
window.updateScopeCount = () => {
  const n = state.scopeSelected.size;
  const el = $("#sc-count");
  if (el) el.textContent = "已选 " + n + " 项";
};
window.saveScopes = async (id) => {
  const scopes = Array.from(state.scopeSelected).map(k => {
    const i = k.indexOf("\\");
    return i < 0 ? [k, ""] : [k.slice(0, i), k.slice(i + 1)];
  });
  if (!scopes.length) {
    $("#sc-err").textContent = "请至少选择一项";
    return;
  }
  try {
    await api(`/api/v1/admin/users/${id}/scopes`, { method: "PUT", body: { scopes } });
    closeModal();
    toast("已保存可见范围（" + scopes.length + " 项）");
    loadAdmin();
  } catch (e) { $("#sc-err").textContent = e.message; }
};
window.issueTokenFor = async (uid, name) => {
  // 兼容旧入口：统一走 API 密钥页的签发表单
  state.tokenUserFilter = uid;
  setAdminTab("tokens");
  await loadAdmin();
  const sel = $("#tk-user");
  if (sel) sel.value = String(uid);
  toast("已切到 API 密钥页，请确认备注与有效期后签发");
};
window.tokenCount = (uid) => (state.tokenCounts && state.tokenCounts[uid]) || 0;

async function adminRestricted(body) {
  const r = await api("/api/v1/admin/restricted");
  state.restrictedDirs = r.dirs || [];
  body.innerHTML = `
    <div class="form-row">
      <label>共享<input id="dr-share" class="inline"></label>
      <label>子目录<input id="dr-sub" class="inline" placeholder="留空=整共享"></label>
      <label>说明<input id="dr-label" class="inline"></label>
      <button class="primary" onclick="addRestricted()">标记为涉密</button>
    </div>
    <table><thead><tr><th>共享</th><th>子目录</th><th>说明</th><th>状态</th><th>操作</th></tr></thead><tbody>
    ${state.restrictedDirs.map((d, i) => `<tr><td>${esc(d.share)}</td><td>${esc(d.subpath || "(整共享)")}</td><td>${esc(d.label || "")}</td>
      <td><span class="badge ${d.restricted ? "failed" : "skipped"}">${d.restricted ? "涉密" : "普通"}</span></td>
      <td>${d.restricted ? `<button class="sm" onclick="clearRestricted(${i})">取消标记</button>`
                          : '<span class="muted">—</span>'}</td></tr>`).join("")}
    </tbody></table>
    <p class="muted">命中涉密目录的查询会自动锁定本地模型，且不向云端回传片段原文。</p>`;
}
window.addRestricted = async () => {
  try {
    await api("/api/v1/admin/restricted", { method: "PUT", body: {
      share: $("#dr-share").value.trim(), subpath: $("#dr-sub").value.trim(),
      restricted: true, label: $("#dr-label").value.trim() } });
    toast("已标记"); loadAdmin();
  } catch (e) { toast("失败: " + e.message); }
};
window.clearRestricted = async (i) => {
  // 取消标记 = 按既有 upsert 语义把 restricted 置 0（保留 share/subpath/label）
  const d = state.restrictedDirs[i];
  if (!d) return;
  try {
    await api("/api/v1/admin/restricted", { method: "PUT", body: {
      share: d.share, subpath: d.subpath || "",
      restricted: false, label: d.label || "" } });
    toast("已取消标记"); loadAdmin();
  } catch (e) { toast("失败: " + e.message); }
};

async function adminModels(body) {
  const [r, pr, pool] = await Promise.all([
    api("/api/v1/models?only_usable=false"),
    api("/api/v1/admin/models/presets"),
    api("/api/v1/admin/parse-pool"),
  ]);
  const usable = r.models.filter(m => m.usable);
  const unusable = r.models.filter(m => !m.usable);
  state.modelPresets = pr.presets;
  const inPool = {};
  for (const m of (pool.members || [])) inPool[m.id] = m;

  body.innerHTML = `
    <div class="form-row">
      <button class="primary" onclick="openAddModel()">+ 添加模型</button>
      ${unusable.length ? `<button onclick="dropUnusable()">清理 ${unusable.length} 个不可用模型</button>` : ""}
      <span class="muted">列表只显示<b>可用</b>的模型；预置厂商在「添加模型」里按需选。</span>
    </div>

    <div class="form-row" style="align-items:center">
      <b>文档解析算力池</b>
      <span class="muted">勾选的模型参与知识提炼（本地+远端分摊算力）。按<b>文档</b>分派，同一份文档固定用一个模型以保证知识点风格一致。</span>
    </div>
    ${pool.mixed_family ? '<div class="err" style="margin-bottom:8px">⚠️ 池内模型族不一致（' + Object.keys(pool.families).join(' / ') + '），不同模型生成的知识点风格会有差异，建议同族模型入池。</div>' : ""}
    <table><thead><tr><th>ID</th><th>名称</th><th>类型</th><th>Base URL</th><th>模型</th><th>num_ctx</th><th>入池</th><th>权重</th><th>状态</th><th>操作</th></tr></thead><tbody>
    ${usable.map(m => `<tr>
      <td>${m.id}</td><td>${esc(m.name)}</td><td>${esc(m.provider)}</td>
      <td class="muted">${esc(m.base_url)}</td><td><code>${esc(m.model_id)}</code></td>
      <td class="muted">${m.num_ctx || "-"}</td>
      <td><input type="checkbox" ${inPool[m.id] ? "checked" : ""} onchange="togglePool(${m.id}, this.checked)"></td>
      <td><input type="number" step="0.5" min="0.5" style="width:70px" value="${inPool[m.id] ? inPool[m.id].weight : 1}"
          onchange="setPoolWeight(${m.id}, this.value)"></td>
      <td><span class="badge done">可用${m.latency_ms ? " · " + m.latency_ms + "ms" : ""}</span>
          ${inPool[m.id] ? `<span class="muted"> 运行中 ${inPool[m.id].inflight}/${inPool[m.id].limit}</span>` : ""}</td>
      <td><button class="sm" onclick="testModel(${m.id})">测试</button>
         <button class="sm" onclick="delModel(${m.id})">删除</button></td></tr>`).join("")}
    ${usable.length ? "" : '<tr><td colspan="10" class="muted">没有可用的模型，点「添加模型」配置一个</td></tr>'}
    </tbody></table>
    <p class="muted" style="margin-top:6px">${esc(pool.note || "")}</p>
    ${unusable.length ? `
      <details style="margin-top:10px">
        <summary class="muted" style="cursor:pointer">不可用模型（${unusable.length}）</summary>
        <table><thead><tr><th>ID</th><th>名称</th><th>原因</th><th>操作</th></tr></thead><tbody>
        ${unusable.map(m => `<tr><td>${m.id}</td><td>${esc(m.name)}</td>
          <td class="err">${esc(m.reason || "不可用")}</td>
          <td><button class="sm" onclick="delModel(${m.id})">删除</button></td></tr>`).join("")}
        </tbody></table>
      </details>` : ""}`;
}
window.togglePool = async (id, on) => {
  // 必须提交**完整**的成员集合：后端是整体替换语义，
  // 只发单个 id 会把池子里其它成员踢掉。
  const members = [];
  const trs = document.querySelectorAll("#admin-body > table tbody tr");
  for (const tr of trs) {
    const tds = tr.children;
    const cb = tds[6] && tds[6].querySelector("input[type=checkbox]");
    const wt = tds[7] && tds[7].querySelector("input[type=number]");
    if (!cb) continue;
    if (cb.checked) {
      members.push({ id: Number(tds[0].textContent.trim()),
                     weight: Number((wt && wt.value) || 1) });
    }
  }
  try {
    await api("/api/v1/admin/parse-pool", { method: "PUT", body: { members } });
    toast("算力池已更新（" + members.length + " 个成员）");
    loadAdmin();
  } catch (e) { toast("失败: " + e.message); }
};
window.setPoolWeight = async (id, w) => {
  const members = [];
  const trs = document.querySelectorAll("#admin-body > table tbody tr");
  for (const tr of trs) {
    const tds = tr.children;
    const cb = tds[6] && tds[6].querySelector("input[type=checkbox]");
    const wt = tds[7] && tds[7].querySelector("input[type=number]");
    if (!cb || !cb.checked) continue;
    members.push({ id: Number(tds[0].textContent.trim()),
                   weight: Number((wt && wt.value) || 1) });
  }
  try {
    await api("/api/v1/admin/parse-pool", { method: "PUT", body: { members } });
    loadAdmin();
  } catch (e) { toast("失败: " + e.message); }
};

window.openAddModel = async () => {
  let presets = state.modelPresets;
  if (!presets) {
    try { presets = (await api("/api/v1/admin/models/presets")).presets; }
    catch (e) { toast("加载预置配置失败: " + e.message); return; }
  }
  state.modelPresets = presets;
  openModal(`<h3>添加模型</h3>
    <div class="form-row" style="margin:10px 0">
      <label style="min-width:260px">从预置配置选择
        <select id="am-preset" onchange="applyPreset()">
          <option value="">— 自定义 —</option>
          ${presets.map((p, i) => `<option value="${i}">${esc(p.name)}${p.note ? " · " + esc(p.note) : ""}</option>`).join("")}
        </select>
      </label>
    </div>
    <div id="am-fields" style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      <label>名称<input id="am-name"></label>
      <label>类型<select id="am-prov">
        <option value="ollama">Ollama(内网自建)</option>
        <option value="openai_compatible">OpenAI 兼容(云端)</option></select></label>
      <label>Base URL<input id="am-url" placeholder="http://host:11434"></label>
      <label>模型名<input id="am-mid" placeholder="qwen3.5:9b"></label>
      <label>API Key<input id="am-key" type="password" placeholder="云端模型必填"></label>
      <label>上下文 num_ctx<input id="am-ctx" type="number" placeholder="远端 Ollama 必填，建议 65536"></label>
      <label class="chk"><input type="checkbox" id="am-local">内网自建（涉密内容可路由到此）</label>
    </div>
    <div id="am-note" class="muted" style="margin-top:8px"></div>
    <div id="am-err" class="err"></div>
    <div class="modal-foot"><button onclick="closeModal()">取消</button>
      <button class="primary" onclick="submitAddModel()">添加</button></div>`);
};
window.applyPreset = () => {
  const i = $("#am-preset").value;
  const note = $("#am-note");
  if (i === "") { note.textContent = "自定义：所有字段手填。"; return; }
  const p = state.modelPresets[Number(i)];
  $("#am-name").value = p.name;
  $("#am-prov").value = p.provider;
  $("#am-url").value = p.base_url;
  $("#am-mid").value = p.model_id;
  if (p.provider === "ollama") {
    $("#am-local").checked = true;
    $("#am-ctx").value = 65536;
    note.innerHTML = "Ollama 预置：需填 <b>num_ctx</b>（runner 默认 4096 会静默截断长提示）。" +
      "如果这台机器上建过 <code>qwen3.5-9b-64k</code> 这样的变体，模型名直接用变体名并把 num_ctx 留空。";
  } else {
    $("#am-local").checked = false;
    $("#am-ctx").value = "";
    note.innerHTML = "云端模型：填 <b>API Key</b> 才能用。涉密目录的内容不会被发往云端（平台会自动改用内网模型）。";
  }
};
window.submitAddModel = async () => {
  const ctx = $("#am-ctx").value.trim();
  const body = {
    name: $("#am-name").value.trim(), provider: $("#am-prov").value,
    base_url: $("#am-url").value.trim(), model_id: $("#am-mid").value.trim(),
    api_key: $("#am-key").value || null, is_local: $("#am-local").checked,
    extra: ctx ? { num_ctx: Number(ctx) } : {},
  };
  if (!body.name || !body.base_url || !body.model_id) {
    $("#am-err").textContent = "名称、Base URL、模型名都要填";
    return;
  }
  if (body.provider === "openai_compatible" && !body.api_key) {
    $("#am-err").textContent = "云端模型必须填 API Key，否则不可用";
    return;
  }
  try {
    await api("/api/v1/admin/models", { method: "POST", body });
    closeModal();
    toast("已添加");
    loadAdmin(); loadModels();
  } catch (e) { $("#am-err").textContent = e.message; }
};
window.dropUnusable = async () => {
  if (!confirm("删除所有不可用的模型（没配 Key 的云端模型、连不上的 Ollama）？")) return;
  try {
    const r = await api("/api/v1/admin/models/unusable", { method: "DELETE" });
    toast("已清理 " + r.count + " 个");
    loadAdmin(); loadModels();
  } catch (e) { toast("失败: " + e.message); }
};
window.testModel = async (id) => {
  toast("测试中…");
  try {
    const r = await api("/api/v1/admin/models/test?model_id=" + id, { method: "POST" });
    openModal(`<h3>模型测试结果</h3>
      <div class="kv">延迟: ${r.latency_sec}s</div>
      <div class="kv">实际处理的 prompt token: ${r.prompt_tokens}</div>
      <div class="kv">输出 token: ${r.output_tokens}</div>
      <div class="kv">配置的 num_ctx: ${r.num_ctx ?? "(未配置)"}</div>
      ${r.truncated ? '<div class="err">⚠️ 提示被截断了——请把 num_ctx 调大（远端 Ollama 必填）</div>' : ''}
      <div class="kv" style="margin-top:8px">回复: ${esc(r.reply)}</div>`);
  } catch (e) { toast("测试失败: " + e.message); }
};
window.delModel = async (id) => {
  try { await api("/api/v1/admin/models/" + id, { method: "DELETE" }); toast("已删除"); loadAdmin(); }
  catch (e) { toast("失败: " + e.message); }
};

/* ---------------------------------------------------------- API 密钥
   密钥只有一处签发入口（就在本页签），用户列表里只显示数量并可跳转过来。
   生命周期：签发 -> 吊销 -> 删除。 */

async function adminTokens(body) {
  const isAdmin = state.role === "admin";
  const [tok, usr] = await Promise.all([
    api("/api/v1/admin/tokens"),
    isAdmin ? api("/api/v1/admin/users") : Promise.resolve({ users: [] }),
  ]);
  const byUser = {};
  for (const t of tok.tokens) {
    (byUser[t.user_id] = byUser[t.user_id] || []).push(t);
  }
  const cur = state.tokenUserFilter;
  const shown = cur ? tok.tokens.filter(t => t.user_id === cur) : tok.tokens;
  const me = state.user || {};
  const uname = id => (usr.users.find(u => u.id === id) || {}).username
    || (id === me.id ? me.username : ("#" + id));
  const userPicker = isAdmin
    ? `<label>用户<select id="tk-user">${usr.users.map(u =>
        `<option value="${u.id}"${cur === u.id ? " selected" : ""}>${esc(u.display_name || u.username)} (${esc(u.username)})</option>`).join("")}</select></label>`
    : `<label>账号<input class="inline" value="${esc(me.username || "")}" disabled></label>`;

  body.innerHTML = `
    <div class="form-row">
      ${userPicker}
      <label>备注<input id="tk-label" class="inline" placeholder="如：WorkBuddy 接入 / 自动化脚本" value="${esc(state.tokenLabel || "")}"></label>
      <label>有效期(天)<input id="tk-ttl" class="inline" type="number" value="90" min="1"></label>
      <button class="primary" onclick="issueToken()">签发密钥</button>
      ${cur ? '<button onclick="clearTokenFilter()">显示全部用户</button>' : ""}
      <button onclick="openApiGuide()">📖 API 调用手册</button>
    </div>
    <div class="err" style="margin-bottom:8px">
      <b>列表里显示的是「密钥前缀」（前 12 位，用来人工辨认），不是密钥本身。</b><br>
      拿前缀去调接口一定会返回 <b>401</b>。要给客户端用，请点「查看密钥」拿到
      <b>完整密钥</b>（约 35 位）再复制——平台会加密保存，可以随时重新查看，<b>不需要换密钥</b>。
    </div>
    <div class="muted" style="margin-bottom:8px">
      用户与其密钥是 1:N 关系——同一用户可签发多枚（如不同机器各一枚）。
      <b>「重新签发」会吊销旧密钥</b>，所有正在用旧密钥的智能体会立刻 401；
      优先用「查看密钥」把完整密钥补给客户端。
    </div>
    <table><thead><tr><th>ID</th><th>用户</th><th>密钥前缀（非密钥）</th><th>备注</th><th>状态</th><th>创建</th><th>最后使用</th><th>过期</th><th>操作</th></tr></thead><tbody>
    ${shown.length ? shown.map(t => `<tr><td>${t.id}</td><td>${esc(uname(t.user_id))}</td>
      <td><code title="这只是前缀，不是密钥；用它调接口会 401">${esc(t.prefix)}</code></td>
      <td>${esc(t.label || "")}</td><td><span class="badge ${t.revoked ? "failed" : "done"}">${t.revoked ? "已吊销" : "有效"}</span></td>
      <td class="muted">${fmtTime(t.created_at)}</td><td class="muted">${fmtTime(t.last_used_at)}</td>
      <td class="muted">${fmtTime(t.expires_at)}</td>
      <td>${t.revoked
          ? `<button class="sm" onclick="purgeToken(${t.id})">删除</button>`
          : `<button class="sm" onclick="viewToken(${t.id})">查看密钥</button>
             <button class="sm" onclick="reissueToken(${t.id}, ${t.user_id})">重新签发</button>
             <button class="sm" onclick="revokeToken(${t.id})">吊销</button>`}</td></tr>`).join("")
      : '<tr><td colspan="9" class="muted">没有密钥</td></tr>'}
    </tbody></table>
    <p class="muted">吊销后密钥立即失效但记录保留；再点「删除」才会从库中彻底移除（已吊销的才可删）。</p>`;
}
window.viewToken = async (id) => {
  try {
    const r = await api("/api/v1/admin/tokens/" + id + "/plaintext");
    showTokenModal(r.token, "密钥 #" + id + (r.revoked ? "（已吊销，不可用）" : ""), false);
  } catch (e) { toast("取不到明文: " + e.message); }
};

/* ---------------------------------------------------------- API 调用手册 */
window.openApiGuide = async () => {
  toast("生成手册中…");
  let g;
  try { g = await api("/api/v1/admin/api-guide"); }
  catch (e) { toast("生成失败: " + e.message); return; }
  state.apiGuide = g;
  openModal(`<h3>API 接入说明（给 AI 用）</h3>
    <p class="muted">这是一份写给 AI 智能体的接入说明：怎么用密钥连上平台、怎么查知识、
    怎么下载文档，每一步都有可直接照抄的调用示例。下载后整份发给 AI 即可。</p>
    ${g.scope ? `<div class="src-hint" style="margin:10px 0">你的可见目录：<b>${esc(g.scope)}</b><br>
        超出这个范围的文档接口会返回空结果——这是权限边界，不是故障。</div>` : ""}
    <div class="form-row" style="margin:12px 0">
      <label style="min-width:300px">把你的密钥填在这里（可选，下载时自动替换占位符）
        <input id="guide-token" type="password" placeholder="kb_xxxx，留空则保留占位符"></label>
    </div>
    <div class="form-row" style="margin:0 0 10px">
      <button class="primary" onclick="downloadGuide()">⬇ 下载完整手册 (.md)</button>
      <button onclick="downloadSkillPackage()">⬇ 下载 WorkBuddy 技能包 (.zip)</button>
      <button onclick="copySkillSnippet()">复制 WorkBuddy 片段</button>
      <span class="muted">${esc(g.note)}</span>
    </div>
    <details>
      <summary class="muted" style="cursor:pointer">预览（不下载也能看）</summary>
      <textarea id="guide-text" style="width:100%;height:420px;margin-top:8px;background:#1c232c;color:#e6edf3;border:1px solid #2a333d;border-radius:6px;padding:10px;font:12px/1.5 Consolas,monospace">${esc(g.guide)}</textarea>
    </details>`);
  const ti = $("#guide-token");
  const applyToken = () => {
    const el = document.getElementById("guide-text");
    if (!el) return;
    if (!el.dataset.orig) el.dataset.orig = el.value;
    const key = (ti ? ti.value : "").trim();
    const orig = el.dataset.orig;
    if (!key) { el.value = orig; return; }
    // 手册正文用 $ZB_KB_TOKEN 环境变量写法，替换不直观 →
    // 改成在开头写明"本机密钥"，并把片段里的占位符换掉
    const head = `> **本机 API 密钥**（已填入，直接用这个）：\n> \`${key}\`\n`
              + `>\n> 下面的示例用 \`$ZB_KB_TOKEN\` 环境变量，把上面这串填进去即可。\n\n`;
    el.value = head + orig.split("<你的API密钥>").join(key);
  };
  if (ti) ti.addEventListener("input", applyToken);
};
window.copySkillSnippet = async () => {
  const g = state.apiGuide;
  if (!g) { toast("手册还没生成", 1500); return; }
  const key = (document.getElementById("guide-token") || {}).value || "";
  const text = (g.skill_snippet || "").split("<你的API密钥>").join(key.trim() || "<你的API密钥>");
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制 WorkBuddy 片段");
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); toast("已复制 WorkBuddy 片段"); }
    catch (e2) { toast("复制失败，请手动从下方预览里选"); }
    ta.remove();
  }
};
window.downloadGuide = () => {
  const el = document.getElementById("guide-text");
  if (!el) { toast("生成中，请稍候", 1500); return; }
  const blob = new Blob(["﻿" + el.value], { type: "text/markdown;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "企业知识库-API接入说明.md";
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  toast("已下载 企业知识库-API接入说明.md");
};
window.downloadSkillPackage = () => {
  const a = document.createElement("a");
  a.href = "/api/v1/admin/skill-package";
  a.download = "workbuddy-zs-kb.zip";
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast("已下载 workbuddy-zs-kb.zip（导入 WorkBuddy 即可用）");
};
window.issueToken = async () => {
  const sel = $("#tk-user");
  const uid = sel ? Number(sel.value) : 0;   // 普通用户没有选择框 → 签给自己
  const who = sel ? sel.selectedOptions[0].textContent : ((state.user || {}).username || "我");
  const label = $("#tk-label").value.trim();
  const ttl = Number($("#tk-ttl").value) || 90;
  try {
    const r = await api(uid ? `/api/v1/admin/tokens?user_id=${uid}` : "/api/v1/admin/tokens",
      { method: "POST", body: { label: label || null, ttl_days: ttl } });
    showTokenModal(r.token, who, true);
    state.tokenLabel = "";
    loadAdmin();
  } catch (e) { toast("签发失败: " + e.message); }
};
window.reissueToken = async (id, userId) => {
  if (!confirm("重新签发会吊销旧密钥并生成新密钥。\n\n"
             + "所有正在用旧密钥的智能体会立刻 401，直到你把新密钥补给它们。\n\n"
             + "如果只是密钥弄丢了，应该用「查看密钥」而不是重新签发。继续？")) return;
  try {
    await api("/api/v1/admin/tokens/" + id, { method: "DELETE" });
    const r = await api("/api/v1/admin/tokens?user_id=" + userId, { method: "POST",
      body: { label: "重新签发", ttl_days: 90 } });
    showTokenModal(r.token, "用户 #" + userId, false);
    loadAdmin();
  } catch (e) { toast("失败: " + e.message); }
};
window.showTokenModal = function (token, who, isNew) {
  const warn = isNew === false
    ? "<b>这是密钥的完整明文</b>（平台加密保存，随时可以再来查看）。请复制<b>完整这一串</b>给客户端。"
    : "<b>请立刻复制</b>——这是完整密钥（约 35 位）。平台会加密保存，之后也能用「查看密钥」再取回来。";
  openModal(`<h3>${isNew === false ? "查看密钥" : "密钥已签发"}</h3>
    <div class="err" style="margin-bottom:10px">
      ${warn}
      <br><b>不要只给前缀</b>（前 12 位）——那不是密钥，客户端会拿到 401。
    </div>
    <div style="background:#1c232c;padding:12px;border-radius:6px;font-family:Consolas,monospace;
         word-break:break-all;user-select:all" id="new-token">${esc(token)}</div>
    <p class="muted" style="margin-top:10px">
      交给客户端后，让 AI 配成环境变量 <code>ZB_KB_TOKEN</code>；
      请求头写 <code>Authorization: Bearer &lt;这串密钥&gt;</code>。
      ${who ? "<br>签发给：" + esc(who) : ""}
    </p>
    <div class="modal-foot" style="display:flex;gap:8px;justify-content:flex-end">
      <button class="primary" onclick="copyTokenPlain()">复制密钥</button>
      <button onclick="closeModal()">我已复制</button>
    </div>`);
};
window.copyTokenPlain = async () => {
  const el = document.getElementById("new-token");
  if (!el) { toast("找不到密钥内容", 1500); return; }
  await copyText(el.textContent);
};
window.copyText = async (txt) => {
  try {
    await navigator.clipboard.writeText(txt);
    toast("已复制到剪贴板");
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = txt; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); toast("已复制到剪贴板"); }
    catch (e2) { toast("复制失败，请手动选中上面的文本"); }
    document.body.removeChild(ta);
  }
};
window.reparseAllScanned = async () => {
  if (!confirm("把所有扫描件排进队列做 OCR？\n\n"
             + "OCR 约 136 秒/页，单个文件最多 20 页 / 10 分钟；\n"
             + "在后台队列里跑，期间平台照常可用。")) return;
  try {
    const r = await api("/api/v1/documents/reparse-batch", { method: "POST" });
    toast(r.notice || `已排队 ${r.queued} 个`, 6000);
    loadStats();
    loadDocs();
  } catch (e) { toast("排队失败: " + e.message, 5000); }
};
window.reparseDoc = async (id) => {
  if (!confirm("重新解析这个文档？\n\n扫描件需要做 OCR，约 136 秒/页；\n"
             + "重跑会清掉它已有的解析结果并重新生成。")) return;
  try {
    const r = await api(`/api/v1/documents/${id}/reparse`, { method: "POST" });
    toast(r.notice || "已重新排队");
    loadStats();
    loadDocs();
  } catch (e) { toast("重跑失败: " + e.message, 5000); }
};
window.filterTokensOf = (uid) => {
  state.tokenUserFilter = uid;
  setAdminTab("tokens");
  loadAdmin();
};
window.clearTokenFilter = () => { state.tokenUserFilter = null; loadAdmin(); };
window.revokeToken = async (id) => {
  try { await api("/api/v1/admin/tokens/" + id, { method: "DELETE" }); toast("已吊销"); loadAdmin(); }
  catch (e) { toast("失败: " + e.message); }
};
window.purgeToken = async (id) => {
  if (!confirm("彻底删除这条密钥记录？删除后不可恢复。")) return;
  try { await api("/api/v1/admin/tokens/" + id + "/purge", { method: "DELETE" }); toast("已删除"); loadAdmin(); }
  catch (e) { toast("失败: " + e.message); }
};

async function adminAgents(body) {
  const r = await api("/api/v1/admin/agents");
  const isAdmin = state.role === "admin";
  body.innerHTML = r.agents.length
    ? `<table><thead><tr>${isAdmin ? "<th>所属账号</th>" : ""}<th>ID</th><th>名称</th><th>类型</th><th>客户端</th><th>注册时间</th><th>最后活跃</th></tr></thead><tbody>
       ${r.agents.map(a => `<tr>${isAdmin ? `<td class="muted">${esc(a.username || "")}</td>` : ""}<td>${a.id}</td><td>${esc(a.name)}</td><td>${esc(a.kind || "")}</td>
         <td class="muted">${esc(a.client_id || "")}</td><td class="muted">${fmtTime(a.created_at)}</td>
         <td class="muted">${fmtTime(a.last_seen_at)}</td></tr>`).join("")}</tbody></table>`
    : '<p class="muted">还没有智能体接入。把 API 密钥交给 WorkBuddy 后，它注册时会出现在这里。</p>';
}

async function adminAudit(body) {
  const r = await api("/api/v1/admin/audit?limit=200");
  body.innerHTML = `<table><thead><tr><th>时间</th><th>用户</th><th>来源</th><th>动作</th><th>问题</th><th>模型</th><th>涉密</th></tr></thead><tbody>
    ${r.audit.map(a => `<tr><td class="muted">${fmtTime(a.created_at)}</td><td>${esc(a.username || "-")}</td>
      <td class="muted">${esc(a.token_prefix || "")}</td><td>${esc(a.action)}</td>
      <td>${esc((a.question || "").slice(0, 60))}</td><td class="muted">${esc(a.model_used || "")}</td>
      <td>${a.restricted ? '<span class="badge failed">涉密</span>' : ""}</td></tr>`).join("")}
    </tbody></table>`;
}

/* 检索轨迹（Task 0.3）：把"不准"变成看得见的。轨迹只存 chunk id / 分数 /
   命中通道，问题只存哈希前 16 位——界面上也只显示这些，别把原文加回来。 */
async function adminTraces(body) {
  const r = await api("/api/v1/admin/traces?limit=50");
  const routes = (t) => {
    const s = t.route_stats || {};
    return `<span class="muted">词${s.keyword ?? "-"}</span>
      <span class="muted">矢${s.vector ?? "-"}</span>
      <span class="muted">图${s.graph ?? "-"}</span>
      <span class="muted">wiki${s.wiki ?? "-"}</span>`;
  };
  body.innerHTML = `
    <table><thead><tr><th>时间</th><th>request_id</th><th>问题</th><th>各路召回</th><th>反馈</th><th></th></tr></thead><tbody>
    ${(r.traces || []).map(t => `<tr>
      <td class="muted">${fmtTime(t.created_at)}</td>
      <td class="muted">${esc(t.request_id)}</td>
      <td class="muted" title="红线：界面只显示问题哈希">${esc(t.question_hash || "")}</td>
      <td>${routes(t)}</td>
      <td>${t.feedback_rating
        ? `<span class="badge ${t.feedback_rating === "up" ? "done" : "failed"}">${
            t.feedback_rating === "up" ? "👍 有用" :
            t.feedback_rating === "down" ? "👎 没用" : "引用不对"}</span>
            ${t.feedback_comment ? `<div class="muted" style="font-size:12px">${esc(t.feedback_comment)}</div>` : ""}`
        : '<span class="muted">-</span>'}</td>
      <td><button class="sm" onclick="showTraceDetail('${esc(t.request_id)}')">详情</button></td></tr>`).join("")}
    </tbody></table>
    <p class="muted">轨迹记录每次问答的四路召回与融合名次（只含 chunk id 与分数，不含原文）。「没用 / 引用不对」集中的 request_id 就是检索优化的第一优先级。</p>`;
}

/* 轨迹详情：这次到底召回了什么、每路命中了哪些块、融合后排到第几 */
async function showTraceDetail(requestId) {
  let d;
  try {
    d = (await api(`/api/v1/admin/traces/${requestId}`)).trace;
  } catch (e) { toast("加载轨迹失败: " + e.message); return; }
  const ro = d.routes || {};
  openModal(`<h3 style="margin:0 0 10px">检索轨迹 ${esc(requestId)}</h3>
    <p class="muted" style="font-size:12px">问题哈希 ${esc(d.question_hash || "-")} ·
      ${d.acl_filtered ? '<span class="badge failed">有候选被 ACL 过滤</span>'
                       : '<span class="badge done">无 ACL 过滤</span>'}</p>
    <div class="muted" style="font-size:12px">
      keyword: ${(ro.keyword || []).join(", ") || "无"}<br>
      vector: ${(ro.vector || []).join(", ") || "无"}<br>
      graph: ${(ro.graph || []).join(", ") || "无"}<br>
      wiki: ${(ro.wiki || []).join(", ") || "无"}
    </div>
    <table><thead><tr><th>名次</th><th>chunk_id</th><th>分数</th><th>命中通道</th></tr></thead><tbody>
    ${(d.fused || []).map((c, i) => `<tr>
      <td class="muted">${i + 1}</td><td>${c.chunk_id}</td>
      <td class="muted">${Number(c.score).toFixed(5)}</td>
      <td>${(c.routes || []).map(x => `<span class="tag">${esc(x)}</span>`).join("")}</td></tr>`).join("")}
    </tbody></table>`);
}

/* ---------------------------------------------------------- 工具 */
function fmtTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString("zh-CN") : "-";
}
function esc(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function openModal(html) {
  $("#modal-body").innerHTML = html;
  $("#modal").classList.remove("hidden");
}
function closeModal() { $("#modal").classList.add("hidden"); }
$("#modal-close").addEventListener("click", closeModal);
$("#modal").addEventListener("click", e => { if (e.target.id === "modal") closeModal(); });

/* ---------------------------------------------------------- 启动 */
(async function boot() {
  try {
    const me = await api("/api/v1/me");
    showMain(me.user);
  } catch (e) { showLogin(); }
})();
