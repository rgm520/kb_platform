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
    const r = await api("/api/v1/models");
    state.models = r.models;
    const sel = $("#a-model");
    const cur = sel.value;
    sel.innerHTML = r.models.map(m =>
      `<option value="${m.id}">${esc(m.name)}${m.is_local ? " [本地]" : ""}</option>`).join("");
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
window.grantUser = (id) => {
  openModal(`<h3>授权可见目录（用户 #${id}）</h3>
    <p class="muted">格式：共享 + 子目录（子目录留空表示整共享）。多个用换行分隔。</p>
    <textarea id="g-scope" style="width:100%;height:120px;background:#1c232c;color:#e6edf3;border:1px solid #2a333d;border-radius:6px;padding:8px"
      placeholder="信息化管理&#10;综合系统\\2.党建（党）"></textarea>
    <button class="primary" onclick="saveScopes(${id})">保存</button>`);
};
window.saveScopes = async (id) => {
  const lines = $("#g-scope").value.split("\n").map(s => s.trim()).filter(Boolean);
  try {
    await api(`/api/v1/admin/users/${id}/scopes`, { method: "PUT",
      body: { scopes: lines.map(l => { const i = l.indexOf("\\"); return i < 0 ? [l, ""] : [l.slice(0, i), l.slice(i + 1)]; }) } });
    closeModal(); toast("已保存可见范围"); loadAdmin();
  } catch (e) { toast("保存失败: " + e.message); }
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
  const r = await api("/api/v1/models");
  body.innerHTML = `
    <div class="form-row">
      <label>名称<input id="nm-name" class="inline"></label>
      <label>类型<select id="nm-prov" class="inline"><option value="ollama">Ollama(本地)</option><option value="openai_compatible">OpenAI 兼容</option></select></label>
      <label>Base URL<input id="nm-url" class="inline" value="https://"></label>
      <label>模型名<input id="nm-mid" class="inline"></label>
      <label>API Key<input id="nm-key" class="inline" type="password"></label>
      <label class="chk"><input type="checkbox" id="nm-local">本地</label>
      <button class="primary" onclick="addModel()">添加</button>
    </div>
    <table><thead><tr><th>ID</th><th>名称</th><th>类型</th><th>Base URL</th><th>模型</th><th>本地</th><th>默认</th><th></th></tr></thead><tbody>
    ${r.models.map(m => `<tr><td>${m.id}</td><td>${esc(m.name)}</td><td>${esc(m.provider)}</td>
      <td class="muted">${esc(m.base_url)}</td><td><code>${esc(m.model_id)}</code></td>
      <td>${m.is_local ? "是" : "否"}</td><td>${m.is_default ? "★" : ""}</td>
      <td><button class="sm" onclick="delModel(${m.id})">删除</button></td></tr>`).join("")}
    </tbody></table>`;
}
window.addModel = async () => {
  try {
    await api("/api/v1/admin/models", { method: "POST", body: {
      name: $("#nm-name").value.trim(), provider: $("#nm-prov").value,
      base_url: $("#nm-url").value.trim(), model_id: $("#nm-mid").value.trim(),
      api_key: $("#nm-key").value || null, is_local: $("#nm-local").checked } });
    toast("已添加"); loadAdmin(); loadModels();
  } catch (e) { toast("失败: " + e.message); }
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
    </div>
    <div class="muted" style="margin-bottom:8px">明文只显示一次，请立即交给用户。用户与其密钥是 1:N 关系——同一用户可签发多枚（如不同机器各一枚）。</div>
    <table><thead><tr><th>ID</th><th>用户</th><th>前缀</th><th>备注</th><th>状态</th><th>创建</th><th>最后使用</th><th>过期</th><th>操作</th></tr></thead><tbody>
    ${shown.length ? shown.map(t => `<tr><td>${t.id}</td><td>${esc(uname(t.user_id))}</td><td><code>${esc(t.prefix)}</code></td>
      <td>${esc(t.label || "")}</td><td><span class="badge ${t.revoked ? "failed" : "done"}">${t.revoked ? "已吊销" : "有效"}</span></td>
      <td class="muted">${fmtTime(t.created_at)}</td><td class="muted">${fmtTime(t.last_used_at)}</td>
      <td class="muted">${fmtTime(t.expires_at)}</td>
      <td>${t.revoked
          ? `<button class="sm" onclick="purgeToken(${t.id})">删除</button>`
          : `<button class="sm" onclick="revokeToken(${t.id})">吊销</button>`}</td></tr>`).join("")
      : '<tr><td colspan="9" class="muted">没有密钥</td></tr>'}
    </tbody></table>
    <p class="muted">吊销后密钥立即失效但记录保留；再点「删除」才会从库中彻底移除（已吊销的才可删）。</p>`;
}
window.issueToken = async () => {
  const uid = Number($("#tk-user").value);
  const label = $("#tk-label").value.trim();
  const ttl = Number($("#tk-ttl").value) || 90;
  try {
    const r = await api(`/api/v1/admin/tokens?user_id=${uid}`, { method: "POST",
      body: { label: label || null, ttl_days: ttl } });
    openModal(`<h3>密钥已签发</h3>
      <p class="muted">用户 ${esc($("#tk-user").selectedOptions[0].textContent)}。这是唯一一次显示明文，请立即交给用户。</p>
      <div style="background:#1c232c;padding:12px;border-radius:6px;font-family:monospace;word-break:break-all">${esc(r.token)}</div>
      <p class="muted" style="margin-top:10px">交给用户后，让他们配置到 WorkBuddy：
      环境变量 <code>ZB_KB_TOKEN</code>。</p>`);
    state.tokenLabel = "";
    loadAdmin();
  } catch (e) { toast("签发失败: " + e.message); }
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
