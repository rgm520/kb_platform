/* 企业知识平台 前端（无构建步骤，原生 ES Module 风格单页应用） */
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

const state = {
  user: null, role: null, page: "board",
  docs: { page: 1, size: 50, total: 0 },
  filters: { status: "", share: "" },
  cy: null, models: [], adminTab: "users",
  tokenUserFilter: null, tokenLabel: "", tokenCounts: {},
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
function showLogin() {
  $("#login").classList.remove("hidden");
  $("#main").classList.add("hidden");
  state.user = null;
}
function showMain(user) {
  state.user = user; state.role = user.role;
  $("#login").classList.add("hidden");
  $("#main").classList.remove("hidden");
  $("#who").textContent = (user.display_name || user.username) + (user.role === "admin" ? "（管理员）" : "");
  $$(".admin-only").forEach(e => e.classList.toggle("hidden", user.role !== "admin"));
  loadModels();
  route();
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
  const h = (location.hash || "#/board").replace("#/", "");
  const p = ["board", "graph", "ask", "admin"].includes(h) ? h : "board";
  state.page = p;
  $$("#nav a").forEach(a => a.classList.toggle("active", a.dataset.p === p));
  $$(".page").forEach(s => s.classList.toggle("active", s.id === "p-" + p));
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
    const cards = [
      { l: "监控范围内文档", n: s.total, c: "" },
      { l: "已完成解析 (Tier 1)", n: s.tier1_done, c: "ok" },
      { l: "已进入知识图谱 (Tier 2)", n: s.tier2_done, c: "ok" },
      { l: "待处理", n: bs.pending || 0, c: "warn" },
      { l: "处理失败", n: bs.failed || 0, c: "err" },
      { l: "已跳过(源文件删除)", n: bs.skipped || 0, c: "" },
      { l: "跳过的非文档文件", n: s.non_document_files || 0, c: "" },
    ];
    $("#stat-cards").innerHTML = cards.map(x =>
      `<div class="card-stat ${x.c}"><div class="n">${x.n}</div><div class="l">${x.l}</div></div>`).join("");
  } catch (e) { toast("统计加载失败: " + e.message); }
}
async function loadDocs() {
  const q = new URLSearchParams({
    page: state.docs.page, page_size: state.docs.size,
  });
  if (state.filters.status) q.set("status", state.filters.status);
  if (state.filters.share) q.set("share", state.filters.share);
  try {
    const r = await api("/api/v1/documents?" + q.toString());
    state.docs.total = r.total;
    const tb = $("#doc-table tbody");
    tb.innerHTML = "";
    if (!r.items.length) {
      tb.innerHTML = '<tr><td colspan="9" class="muted">没有符合条件的文档</td></tr>';
    }
    for (const d of r.items) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${esc(d.name)}<div class="muted" style="font-size:11px">${esc(d.rel_path || "")}</div></td>
        <td>${esc(d.share)}</td>
        <td><code>${esc(d.ext)}</code></td>
        <td><span class="badge ${esc(d.status)}">${statusText(d.status)}</span>
            ${d.status_detail ? `<div class="muted" style="font-size:11px">${esc(d.status_detail)}</div>` : ""}</td>
        <td><div class="two-tier"><span class="dot ${d.tier1_done ? "on" : ""}"></span>解析
            <span class="dot t2 ${d.tier2_done ? "on" : ""}"></span>提炼</div></td>
        <td>${d.chunk_count || 0}</td>
        <td>${d.kp_count || 0}</td>
        <td class="muted">${d.updated_at ? new Date(d.updated_at * 1000).toLocaleString("zh-CN") : "-"}</td>
        <td><button class="sm" onclick="showDoc(${d.id})">详情</button></td>`;
      tb.appendChild(tr);
    }
    $("#doc-total").textContent = "共 " + r.total + " 个文档";
    $("#pg-info").textContent = `第 ${r.page} / ${Math.max(1, Math.ceil(r.total / r.page_size))} 页`;
  } catch (e) { toast("文档列表加载失败: " + e.message); }
}
function statusText(s) {
  return ({ done: "已解析", pending: "待处理", failed: "失败", skipped: "已跳过" })[s] || s || "待处理";
}
$("#btn-refresh").addEventListener("click", () => { loadStats(); loadDocs(); });
$("#f-status").addEventListener("change", e => { state.filters.status = e.target.value; state.docs.page = 1; loadDocs(); });
$("#f-share").addEventListener("change", e => { state.filters.share = e.target.value.trim(); state.docs.page = 1; loadDocs(); });
$("#pg-prev").addEventListener("click", () => { if (state.docs.page > 1) { state.docs.page--; loadDocs(); } });
$("#pg-next").addEventListener("click", () => { state.docs.page++; loadDocs(); });

async function showDoc(id) {
  try {
    const d = await api("/api/v1/documents/" + id);
    const kps = (d.knowledge || []).flatMap(k => (k.key_points || []).map(x =>
      `<li><b>${esc(x.title)}</b> <code>${esc(x.category || "")}</code><div class="muted">${esc(x.detail || "")}</div></li>`)).join("");
    openModal(`<h3>${esc(d.name)}</h3>
      <div class="kv">路径: ${esc(d.path)}</div>
      <div class="kv">状态: ${statusText(d.status)} ${d.status_detail ? "— " + esc(d.status_detail) : ""}</div>
      <div class="kv">文本块 ${d.chunk_count || 0} · 知识点 ${d.kp_count || 0} · Tier1 ${d.tier1_done ? "✓" : "—"} · Tier2 ${d.tier2_done ? "✓" : "—"}</div>
      <hr style="border-color:#2a333d">
      <h4>提炼出的知识点</h4><ul>${kps || "<li class='muted'>尚未提炼</li>"}</ul>`);
  } catch (e) { toast("加载文档详情失败: " + e.message); }
}

/* ---------------------------------------------------------- 图谱 */
let cyReady = false;
function initGraph() {
  if (state.cy) { state.cy.resize(); return; }
  state.cy = cytoscape({
    container: $("#cy"),
    elements: [],
    layout: { name: "cose", animate: false, padding: 30, nodeRepulsion: 8000, idealEdgeLength: 90 },
    style: [
      { selector: "node", style: {
          "background-color": "#4a9eff", "label": "data(label)", "color": "#e6edf3",
          "font-size": 11, "text-valign": "bottom", "text-margin-y": 5,
          "width": "data(size)", "height": "data(size)", "border-width": 1, "border-color": "#2a333d" } },
      { selector: "node[type='knowledge_point']", style: { "background-color": "#3fb950" } },
      { selector: "node[type='category']", style: { "background-color": "#a371f7" } },
      { selector: "node[type='document']", style: { "background-color": "#d29922", "shape": "round-rectangle" } },
      { selector: "node[?restricted]", style: { "border-color": "#f85149", "border-width": 2 } },
      { selector: "node:selected", style: { "border-color": "#fff", "border-width": 3 } },
      { selector: "edge", style: { "width": 1, "line-color": "#3a4652", "curve-style": "straight",
          "target-arrow-shape": "triangle", "target-arrow-color": "#3a4652",
          "label": "data(rel)", "font-size": 9, "color": "#8b98a5", "text-rotation": "autorotate" } },
    ],
  });
  state.cy.on("tap", "node", (e) => showNode(e.target.id()));
  cyReady = true;
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
$("#g-q").addEventListener("keydown", e => { if (e.key === "Enter") searchNodes(); });

async function expandNode(id, replace) {
  try {
    const r = await api("/api/v1/graph/neighbors?node=" + id + "&hops=1");
    const cy = state.cy;
    if (replace) cy.elements().remove();
    const existing = new Set(cy.nodes().map(n => n.id()));
    const els = [];
    for (const n of r.nodes) {
      if (!existing.has(String(n.id))) {
        els.push({ data: { id: String(n.id), label: n.name, type: n.type, size: 14 + Math.min(20, n.mentions || 0) } });
      }
    }
    for (const e of r.edges) {
      els.push({ data: { id: e.src + "->" + e.dst + "|" + e.rel, source: String(e.src), target: String(e.dst), rel: e.rel } });
    }
    cy.add(els);
    cy.layout({ name: "cose", animate: true, animationDuration: 400, padding: 30,
                nodeRepulsion: 8000, idealEdgeLength: 90 }).run();
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
  try {
    const r = await api("/api/v1/query", { method: "POST", body: { question: q, model: $("#a-model").value, top_k: 8 } });
    $("#a-answer").textContent = r.answer || "(无输出)";
    if (r.restricted) {
      const n = $("#a-notice");
      n.textContent = r.restricted_notice || "本次回答命中涉密内容，已锁定本地模型且未回传片段原文";
      n.classList.remove("hidden");
    }
    const box = $("#a-cites"); box.innerHTML = "";
    (r.citations || []).forEach((c, i) => {
      const d = document.createElement("div");
      d.className = "cite" + (c.restricted ? " restricted" : "");
      d.innerHTML = `<div><span class="f">[片段${i + 1}] ${esc(c.file)}</span><span class="loc">位置 ${esc(c.locator)}</span></div>
        <div class="tags">${(c.matched_by || []).map(x => `<span class="tag">${esc(x)}</span>`).join("")}
        ${c.restricted ? '<span class="tag">涉密·原文不回传</span>' : ""}</div>
        ${c.snippet ? `<div class="sn">${esc(c.snippet)}</div>` : ""}`;
      box.appendChild(d);
    });
  } catch (e) { toast("提问失败: " + e.message); }
  finally { btn.disabled = false; btn.textContent = "提问"; }
}

/* ---------------------------------------------------------- 管理台 */
$$(".tabs button").forEach(b => b.addEventListener("click", () => {
  $$(".tabs button").forEach(x => x.classList.toggle("active", x === b));
  state.adminTab = b.dataset.t;
  loadAdmin();
}));

async function loadAdmin() {
  const body = $("#admin-body");
  body.innerHTML = '<p class="muted">加载中…</p>';
  try {
    if (state.adminTab === "users") return adminUsers(body);
    if (state.adminTab === "restricted") return adminRestricted(body);
    if (state.adminTab === "models") return adminModels(body);
    if (state.adminTab === "tokens") return adminTokens(body);
    if (state.adminTab === "agents") return adminAgents(body);
    if (state.adminTab === "audit") return adminAudit(body);
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
  body.innerHTML = `
    <div class="form-row">
      <label>用户名<input id="nu-name" class="inline"></label>
      <label>显示名<input id="nu-disp" class="inline"></label>
      <label>初始密码<input id="nu-pass" class="inline" type="password"></label>
      <label>角色<select id="nu-role" class="inline"><option value="user">普通用户</option><option value="admin">管理员</option></select></label>
      <button class="primary" onclick="createUser()">新建用户</button>
    </div>
    <div class="form-row">
      <label>共享<input id="ns-share" class="inline" placeholder="如 信息化管理"></label>
      <label>子目录<input id="ns-sub" class="inline" placeholder="留空=整共享"></label>
      <button onclick="addScope()">为所选用户授权目录</button>
    </div>
    <table><thead><tr><th>ID</th><th>用户名</th><th>显示名</th><th>角色</th><th>可见范围</th><th>状态</th><th>操作</th></tr></thead><tbody>
    ${r.users.map(u => `<tr>
      <td>${u.id}</td><td>${esc(u.username)}</td><td>${esc(u.display_name || "")}</td>
      <td>${esc(u.role)}</td>
      <td>${(u.scopes || []).map(s => `<span class="scope-chip">${esc(s)}</span>`).join("") ||
            (u.role === "admin" ? '<span class="muted">（全部）</span>'
                                 : '<span class="muted">（未授权）</span>')}</td>
      <td>${u.active ? '<span class="muted">启用</span>' : '<span class="err">已停用</span>'}</td>
      <td><button class="sm" onclick="grantUser(${u.id})">授权</button>
         <button class="sm" onclick="filterTokensOf(${u.id})">密钥 ${tokenCount(u.id)}</button>
         <button class="sm" onclick="toggleActive(${u.id},${u.active ? 0 : 1})">${u.active ? "停用" : "启用"}</button></td></tr>`).join("")}
    </tbody></table>`;
}
window.createUser = async () => {
  try {
    await api("/api/v1/admin/users", { method: "POST", body: {
      username: $("#nu-name").value.trim(), password: $("#nu-pass").value,
      role: $("#nu-role").value, display_name: $("#nu-disp").value.trim(), scopes: [] } });
    toast("用户已创建"); loadAdmin();
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
  state.adminTab = "tokens";
  $$(".tabs button").forEach(x => x.classList.toggle("active", x.dataset.t === "tokens"));
  await loadAdmin();
  const sel = $("#tk-user");
  if (sel) sel.value = String(uid);
  toast("已切到 API 密钥页，请确认备注与有效期后签发");
};
window.tokenCount = (uid) => (state.tokenCounts && state.tokenCounts[uid]) || 0;

async function adminRestricted(body) {
  const r = await api("/api/v1/admin/restricted");
  body.innerHTML = `
    <div class="form-row">
      <label>共享<input id="dr-share" class="inline"></label>
      <label>子目录<input id="dr-sub" class="inline" placeholder="留空=整共享"></label>
      <label>说明<input id="dr-label" class="inline"></label>
      <button class="primary" onclick="addRestricted()">标记为涉密</button>
    </div>
    <table><thead><tr><th>共享</th><th>子目录</th><th>说明</th><th>状态</th></tr></thead><tbody>
    ${r.dirs.map(d => `<tr><td>${esc(d.share)}</td><td>${esc(d.subpath || "(整共享)")}</td><td>${esc(d.label || "")}</td>
      <td><span class="badge ${d.restricted ? "failed" : "skipped"}">${d.restricted ? "涉密" : "普通"}</span></td></tr>`).join("")}
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
   密钥只有一处签发入口（就在本��签），用户列表里只显示数量并可跳转过来。
   生命周期：签发 -> 吊销 -> 删除。 */

async function adminTokens(body) {
  const [tok, usr] = await Promise.all([
    api("/api/v1/admin/tokens"),
    api("/api/v1/admin/users"),
  ]);
  const byUser = {};
  for (const t of tok.tokens) {
    (byUser[t.user_id] = byUser[t.user_id] || []).push(t);
  }
  const cur = state.tokenUserFilter;
  const shown = cur ? tok.tokens.filter(t => t.user_id === cur) : tok.tokens;
  const uname = id => (usr.users.find(u => u.id === id) || {}).username || ("#" + id);

  body.innerHTML = `
    <div class="form-row">
      <label>用户<select id="tk-user">${usr.users.map(u =>
        `<option value="${u.id}"${cur === u.id ? " selected" : ""}>${esc(u.display_name || u.username)} (${esc(u.username)})</option>`).join("")}</select></label>
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
    <div class="form-row" style="margin:12px 0">
      <label style="min-width:300px">把你的密钥填在这里（可选，下载时自动替换占位符）
        <input id="guide-token" type="password" placeholder="kb_xxxx，留空则保留占位符"></label>
    </div>
    <div class="form-row" style="margin:0 0 10px">
      <button class="primary" onclick="downloadGuide()">⬇ 下载完整手册 (.md)</button>
      <span class="muted">${esc(g.note)}</span>
    </div>
    <details>
      <summary class="muted" style="cursor:pointer">预览（不下载也能看）</summary>
      <textarea id="guide-text" style="width:100%;height:420px;margin-top:8px;background:#1c232c;color:#e6edf3;border:1px solid #2a333d;border-radius:6px;padding:10px;font:12px/1.5 Consolas,monospace">${esc(g.guide)}</textarea>
    </details>`);
  const ti = $("#guide-token");
  if (ti) ti.addEventListener("input", () => {
    const el = document.getElementById("guide-text");
    if (!el) return;
    if (!el.dataset.orig) el.dataset.orig = el.value;
    el.value = ti.value.trim()
      ? el.dataset.orig.split("<你的API密钥>").join(ti.value.trim())
      : el.dataset.orig;
  });
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
window.issueToken = async () => {
  const uid = Number($("#tk-user").value);
  const label = $("#tk-label").value.trim();
  const ttl = Number($("#tk-ttl").value) || 90;
  try {
    const r = await api(`/api/v1/admin/tokens?user_id=${uid}`, { method: "POST",
      body: { label: label || null, ttl_days: ttl } });
    showTokenModal(r.token, $("#tk-user").selectedOptions[0].textContent, true);
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
window.filterTokensOf = (uid) => {
  state.tokenUserFilter = uid;
  state.adminTab = "tokens";
  $$(".tabs button").forEach(x => x.classList.toggle("active", x.dataset.t === "tokens"));
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
  const r = await api("/api/v1/agents");
  body.innerHTML = r.agents.length
    ? `<table><thead><tr><th>ID</th><th>名称</th><th>类型</th><th>客户端</th><th>注册时间</th><th>最后活跃</th></tr></thead><tbody>
       ${r.agents.map(a => `<tr><td>${a.id}</td><td>${esc(a.name)}</td><td>${esc(a.kind || "")}</td>
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
