/* 管理台：存储源接入、测试/扫描/编辑，以及用户目录授权。 */
window.KB = window.KB || {};
(function () {
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  let sources = [];
  let tree = { sid: null, rel: "", target: null, path: [] };

  const esc2 = (s) => String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const fmt = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-CN") : "从未";
  const bodyEl = () => $("#admin-body");
  const val = (id) => { const e = $("#" + id); return e ? e.value : ""; };
  const checked = (id) => { const e = $("#" + id); return e ? !!e.checked : false; };

  /* ------------------------------------------------------------ 列表 */
  async function render(body) {
    body = body || bodyEl();
    body.innerHTML = '<p class="muted">加载中…</p>';
    try {
      const r = await api("/api/v1/admin/sources");
      sources = r.sources || [];
    } catch (e) {
      body.innerHTML = `<p class="err">加载失败：${esc2(e.message)}</p>`;
      return;
    }
    body.innerHTML = `
      <div class="page-head">
        <div><h3 style="margin:0">存储接入</h3>
          <p class="muted">每个共享根独立账号密码，密码加密保存且不回显。</p></div>
        <button class="primary" onclick="KB.adminSources.newSource()">＋ 新增存储源</button>
      </div>
      <div id="src-form-wrap"></div>
      <div class="table-wrap"><table>
        <thead><tr><th>名称</th><th>类型</th><th>根路径</th><th>密码</th><th>可写</th>
          <th>扫描状态</th><th>最近扫描</th><th>操作</th></tr></thead>
        <tbody>${sources.map((s) => `<tr>
          <td>${esc2(s.name)}</td><td>${esc2(s.kind)}</td>
          <td class="muted" title="${esc2(s.root)}">${esc2(s.root)}</td>
          <td>${s.has_password ? '<span class="fm-badge done">已配置</span>' : '<span class="fm-badge">未配置</span>'}</td>
          <td>${s.writable ? "是" : "否"}</td>
          <td>${s.scan_state === "error"
            ? `<span class="err" title="${esc2(s.scan_error || "")}">错误</span>`
            : (s.scan_state === "scanning" ? "扫描中" : (s.enabled ? "空闲" : "已停用"))}</td>
          <td class="muted">${fmt(s.last_scan_at)}</td>
          <td>
            <button class="sm" onclick="KB.adminSources.test(${s.id})">测试</button>
            <button class="sm" onclick="KB.adminSources.scan(${s.id})">扫描</button>
            <button class="sm" onclick="KB.adminSources.edit(${s.id})">编辑</button>
            <button class="sm" onclick="KB.adminSources.remove(${s.id})">删除</button>
          </td></tr>`).join("") || '<tr><td colspan="8" class="muted">还没有接入存储源</td></tr>'}
        </tbody></table></div>`;
  }

  /* ------------------------------------------------------------ 表单 */
  function showForm(src) {
    const s = src || { name: "", kind: "local", root: "", username: "", domain: "",
      writable: true, personal_root: "", enabled: true, has_password: false };
    const wrap = $("#src-form-wrap");
    wrap.innerHTML = `
      <div class="src-form">
        <h3 style="margin:0 0 12px">${src ? "编辑存储源" : "新增存储源"}</h3>
        <div class="src-grid">
          <label>名称<input id="src-name" value="${esc2(s.name)}"></label>
          <label>类型<select id="src-kind">
            <option value="local" ${s.kind === "local" ? "selected" : ""}>本地目录</option>
            <option value="smb" ${s.kind === "smb" ? "selected" : ""}>SMB 共享</option>
          </select></label>
          <label>根路径<input id="src-root" value="${esc2(s.root)}" placeholder="D:\\共享 或 \\\\nas\\共享\\子目录"></label>
          <label>账号<input id="src-user" value="${esc2(s.username)}" placeholder="SMB 校验账号"></label>
          <label>密码<input id="src-pass" type="password" placeholder="${src && s.has_password ? "留空=不修改" : "共享密码"}"></label>
          <label>域<input id="src-domain" value="${esc2(s.domain)}" placeholder="可选"></label>
          <label>个人空间目录<input id="src-personal" value="${esc2(s.personal_root)}" placeholder="如 _personal，留空关闭"></label>
          <label class="chk" style="flex-direction:row;align-items:center;margin-top:22px">
            <input type="checkbox" id="src-writable" ${s.writable ? "checked" : ""}> 允许写入</label>
          <label class="chk" style="flex-direction:row;align-items:center;margin-top:22px">
            <input type="checkbox" id="src-enabled" ${s.enabled ? "checked" : ""}> 启用</label>
        </div>
        <div class="src-actions">
          <button class="primary" id="src-save">${src ? "保存修改" : "接入"}</button>
          <button id="src-test">测试连接</button>
          <button id="src-cancel">取消</button>
          <span id="src-result" class="muted"></span>
        </div>
        <div id="src-err" class="src-err"></div>
      </div>`;
    const sid = src ? src.id : 0;
    $("#src-save").onclick = () => save(sid);
    $("#src-test").onclick = () => testForm(sid);
    $("#src-cancel").onclick = () => { wrap.innerHTML = ""; };
    $("#src-kind").onchange = () => {
      if (val("src-kind") === "local") $("#src-root").placeholder = "D:\\共享目录";
      else $("#src-root").placeholder = "\\\\nas\\共享\\子目录";
    };
  }

  function collect() {
    const out = {
      name: val("src-name").trim(), kind: val("src-kind"), root: val("src-root").trim(),
      username: val("src-user").trim(), domain: val("src-domain").trim(),
      personal_root: val("src-personal").trim(),
      writable: checked("src-writable"), enabled: checked("src-enabled"),
    };
    const p = val("src-pass");
    if (p) out.password = p;
    return out;
  }

  async function save(id) {
    const err = $("#src-err");
    err.textContent = "";
    const btn = $("#src-save");
    btn.disabled = true;
    try {
      if (id) await api(`/api/v1/admin/sources/${id}`, { method: "PUT", body: collect() });
      else await api("/api/v1/admin/sources", { method: "POST", body: collect() });
      toast(id ? "存储源已更新" : "存储源已接入");
      await render(bodyEl());
    } catch (e) {
      err.textContent = e.message;
    } finally { btn.disabled = false; }
  }

  async function testForm(id) {
    const out = $("#src-result");
    out.textContent = "测试中…";
    try {
      const r = await api(`/api/v1/admin/sources/${id || 0}/test`, { method: "POST", body: collect() });
      out.textContent = r.ok ? `连接成功，发现 ${r.count} 项${(r.sample || []).length ? "：" + r.sample.join("、") : ""}`
        : `连接失败：${r.detail || "未知错误"}`;
      out.className = r.ok ? "muted" : "err";
    } catch (e) { out.textContent = "测试失败：" + e.message; out.className = "err"; }
  }

  async function test(id) {
    try {
      const r = await api(`/api/v1/admin/sources/${id}/test`, { method: "POST" });
      toast(r.ok ? `连接成功，发现 ${r.count} 项` : `连接失败：${r.detail}`, r.ok ? 3200 : 6000);
    } catch (e) { toast("测试失败: " + e.message); }
  }

  async function scan(id) {
    try {
      await api(`/api/v1/admin/sources/${id}/scan`, { method: "POST" });
      toast("已排队扫描，稍后刷新查看状态");
      setTimeout(() => render(bodyEl()), 1500);
    } catch (e) { toast("扫描失败: " + e.message); }
  }

  async function remove(id) {
    const s = sources.find((x) => x.id === id);
    if (!confirm(`确定删除存储源“${s ? s.name : id}”？\n有授权或回收站条目时需要强制删除。`)) return;
    try {
      await api(`/api/v1/admin/sources/${id}`, { method: "DELETE" });
      toast("已删除"); await render(bodyEl());
    } catch (e) {
      if (/force/i.test(e.message) || /授权/.test(e.message)) {
        if (confirm("该源仍有授权或回收站条目，强制删除会一并清除授权，确定？")) {
          try {
            await api(`/api/v1/admin/sources/${id}?force=1`, { method: "DELETE" });
            toast("已强制删除"); await render(bodyEl());
          } catch (e2) { toast("删除失败: " + e2.message); }
        }
      } else toast("删除失败: " + e.message);
    }
  }

  function edit(id) {
    const s = sources.find((x) => x.id === id);
    if (s) showForm(s);
  }

  /* ------------------------------------------------------------ 用户目录授权 */
  async function openGrants(userId) {
    let srcs = [], data;
    try {
      [srcs, data] = await Promise.all([
        api("/api/v1/admin/sources"),
        api(`/api/v1/admin/users/${userId}/grants`),
      ]);
    } catch (e) { toast("加载授权失败: " + e.message); return; }
    sources = srcs.sources || [];
    const list = data.grants || [];
    const options = (sel) => (srcs.sources || []).map((s) =>
      `<option value="${s.id}" ${Number(sel) === s.id ? "selected" : ""}>${esc2(s.name)} (${esc2(s.kind)})</option>`).join("");
    openModal(`<h3 style="margin:0 0 6px">目录授权</h3>
      <p class="muted">授权是可见边界；勾选可写后该目录内才能上传、删除。保存为当前快照。</p>
      <div id="grant-rows"></div>
      <div id="grant-tree" class="src-tree hidden"></div>
      <div class="src-actions">
        <button id="grant-add">＋ 添加目录</button>
        <button id="grant-save" class="primary">保存授权</button>
        <span id="grant-msg" class="muted"></span>
      </div>`);
    const rows = $("#grant-rows");
    const addRow = (g) => {
      const div = document.createElement("div");
      div.className = "grant-row";
      div.innerHTML = `<select class="g-src">${options(g ? g.source_id : (srcs.sources[0] || {}).id)}</select>
        <input type="text" class="g-sub" placeholder="留空=整个源" value="${esc2(g ? g.subpath : "")}">
        <label class="chk"><input type="checkbox" class="g-write" ${g && g.can_write ? "checked" : ""}>可写</label>
        <button class="sm g-del">移除</button>`;
      $(".g-del", div).onclick = () => div.remove();
      $(".g-src", div).onchange = () => {
        const inp = $(".g-sub", div);
        tree.target = inp;
        loadTree(Number($(".g-src", div).value), "", inp);
      };
      const inp = $(".g-sub", div);
      inp.addEventListener("focus", () => {
        tree.target = inp;
        loadTree(Number($(".g-src", div).value), "", inp);
      });
      rows.appendChild(div);
    };
    (list.length ? list : [null]).forEach(addRow);
    $("#grant-add").onclick = () => addRow(null);
    $("#grant-save").onclick = async () => {
      const grants = $$(".grant-row").map((r) => ({
        source_id: Number($(".g-src", r).value),
        subpath: $(".g-sub", r).value.trim(),
        can_write: $(".g-write", r).checked,
      })).filter((g) => g.source_id);
      try {
        await api(`/api/v1/admin/users/${userId}/grants`, { method: "PUT", body: { grants } });
        closeModal();
        toast("授权已保存");
        loadAdmin();
      } catch (e) { $("#grant-msg").textContent = "保存失败：" + e.message; }
    };
    if ((srcs.sources || []).length) {
      tree.target = $(".g-sub");
      loadTree(srcs.sources[0].id, "", tree.target);
    }
  }

  async function loadTree(sid, rel, target) {
    tree.sid = sid; tree.rel = rel || ""; tree.target = target;
    const box = $("#grant-tree");
    if (!box) return;
    box.classList.remove("hidden");
    try {
      const r = await api(`/api/v1/admin/sources/${sid}/tree?rel=${encodeURIComponent(rel || "")}`);
      box.innerHTML = `<div class="muted" style="padding:4px 8px">
          ${esc2((sources.find((s) => s.id === sid) || {}).name || "")} / ${esc2(rel || "/")}
        </div>
        <div class="dir" data-rel="${esc2(rel || "")}">✓ 选择当前目录</div>
        ${(r.dirs || []).map((d) => `<div class="dir" data-rel="${esc2(d.rel)}">▸ ${esc2(d.name)}</div>`).join("")}`;
      $$(".dir", box).forEach((el) => {
        el.onclick = () => {
          const chosen = el.dataset.rel || "";
          if (target && target.value !== undefined) target.value = chosen;
          if (el.textContent.indexOf("选择当前目录") >= 0) return;
          loadTree(sid, chosen, target);
        };
      });
    } catch (e) {
      box.innerHTML = `<div class="err">目录读取失败：${esc2(e.message)}</div>`;
    }
  }

  function newSource() { showForm(null); }

  KB.adminSources = { render, newSource, edit, test, scan, remove, openGrants };
})();
