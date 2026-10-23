/* 文件管理器与回收站：浏览、秒搜、预览、上传、删除、恢复。 */
window.KB = window.KB || {};
(function () {
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  const state = {
    sources: [], sourceId: null, sourceName: "", rel: "", entries: [],
    canWrite: false, selected: new Set(), lastIndex: -1,
    search: [], searchIndex: -1, searchTimer: 0, ctxEntry: null,
    uploads: 0,
  };

  const IMAGE = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico"]);
  const TEXT = new Set(["txt", "md", "csv", "log", "json", "xml", "html", "htm", "ini", "yaml", "yml"]);
  const VIDEO = new Set(["mp4", "webm", "ogg", "mov", "m4a", "mp3", "wav"]);
  const OFFICE = new Set(["docx", "xlsx", "pptx", "doc", "xls", "ppt", "ofd", "pdf"]);

  const extOf = (name) => (String(name).split(".").pop() || "").toLowerCase();
  const parentRel = (rel) => (rel && rel.includes("\\") ? rel.slice(0, rel.lastIndexOf("\\")) : "");
  const passes = (rel) => rel.split("\\").map(encodeURIComponent).join("/");
  const previewUrl = (sourceId, rel) =>
    `/api/v1/fs/preview?source_id=${encodeURIComponent(sourceId)}&rel=${passes(rel)}`;
  const downloadUrl = (sourceId, rel) =>
    `/api/v1/fs/download?source_id=${encodeURIComponent(sourceId)}&rel=${passes(rel)}`;
  const iconOf = (e) => e.is_dir ? "▸" : (extOf(e.name) === "pdf" ? "▤" : "▧");

  function fmtSize(n) {
    n = Number(n || 0);
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
    return (n / 1024 / 1024 / 1024).toFixed(1) + " GB";
  }
  function badge(status) {
    if (!status) return '<span class="fm-badge">—</span>';
    const map = { done: "可搜索", pending: "解析中", failed: "解析失败", skipped: "已跳过" };
    return `<span class="fm-badge ${esc(status)}">${esc(map[status] || status)}</span>`;
  }

  /* ------------------------------------------------------------ 左侧源 */
  function renderSources() {
    const box = $("#fm-sources");
    if (!box) return;
    if (!state.sources.length) {
      box.innerHTML = '<div class="fm-side-empty">没有可访问的存储源<br>请联系管理员授权</div>';
      return;
    }
    box.innerHTML = state.sources.map((s) => `
      <div class="fm-side-item ${s.id === state.sourceId ? "active" : ""}" data-sid="${s.id}">
        <span>▣</span><span class="txt">${esc(s.name)}</span>
        <span class="kind">${esc(s.kind)}</span>
      </div>`).join("") +
      (state.sourceId ? `<div class="fm-side-head" style="margin-top:14px">当前路径</div>
        <div class="fm-side-tree">${crumbsTree()}</div>` : "");
    $$(".fm-side-item", box).forEach((el) => el.addEventListener("click", () => {
      open(Number(el.dataset.sid), "");
    }));
    $$(".fm-side-tree .crumb", box).forEach((el) => el.addEventListener("click", () => {
      open(state.sourceId, el.dataset.rel || "");
    }));
  }

  function crumbsTree() {
    const parts = (state.rel || "").split("\\").filter(Boolean);
    const arr = [{ name: "/", rel: "" }];
    let acc = "";
    for (const p of parts) { acc = acc ? acc + "\\" + p : p; arr.push({ name: p, rel: acc }); }
    return arr.map((c) => `<div class="crumb ${c.rel === state.rel ? "last" : ""}"
      data-rel="${esc(c.rel)}" style="margin-left:${c.rel.split("\\").filter(Boolean).length * 12}px">${esc(c.name)}</div>`).join("");
  }

  async function loadSources() {
    const r = await api("/api/v1/fs/sources");
    state.sources = r.sources || [];
    if (!state.sources.length) {
      state.sourceId = null;
      renderSources();
      renderEmpty("没有可访问的存储源");
      return;
    }
    renderSources();
  }

  async function load() {
    if (!$("#p-files")) return;
    try {
      await loadSources();
      if (!state.sources.length) return;
      const alive = state.sources.some((s) => s.id === state.sourceId);
      if (!alive) state.sourceId = state.sources[0].id;
      await open(state.sourceId, state.rel || "");
    } catch (e) {
      renderEmpty("加载失败：" + e.message);
    }
  }

  async function open(sourceId, rel) {
    sourceId = Number(sourceId);
    rel = rel || "";
    try {
      const r = await api(`/api/v1/fs/list?source_id=${encodeURIComponent(sourceId)}&rel=${passes(rel)}`);
      state.sourceId = sourceId;
      state.sourceName = r.source.name;
      state.rel = r.rel || "";
      state.entries = r.entries || [];
      state.canWrite = !!r.can_write;
      state.selected.clear();
      state.lastIndex = -1;
      renderSources();
      renderCrumbs(r.breadcrumb || []);
      renderList();
      $("#fm-upload").disabled = !state.canWrite;
      $("#fm-upload").title = state.canWrite ? "" : "当前目录没有写权限";
      hideSearch();
      hideCtx();
    } catch (e) {
      renderEmpty("无法打开目录：" + e.message);
    }
  }

  function renderCrumbs(crumbs) {
    const box = $("#fm-crumbs");
    if (!box) return;
    box.innerHTML = crumbs.map((c, i) => {
      const sep = i ? '<span class="crumb-sep">/</span>' : "";
      return `${sep}<button class="crumb ${i === crumbs.length - 1 ? "last" : ""}"
        data-rel="${esc(c.rel)}">${esc(c.name)}</button>`;
    }).join("");
    $$(".crumb", box).forEach((el) => el.addEventListener("click", () => {
      open(state.sourceId, el.dataset.rel || "");
    }));
  }

  function renderEmpty(msg) {
    const box = $("#fm-list");
    if (box) box.innerHTML = `<tr><td colspan="5"><div class="fm-empty">${esc(msg)}</div></td></tr>`;
  }

  function renderList() {
    const box = $("#fm-list");
    if (!box) return;
    if (!state.entries.length) {
      renderEmpty("这个目录是空的");
      return;
    }
    box.innerHTML = state.entries.map((e, i) => `
      <tr class="fm-row" data-i="${i}" data-rel="${esc(e.rel)}" data-dir="${e.is_dir ? 1 : 0}">
        <td class="fm-check"><input type="checkbox" data-rel="${esc(e.rel)}"></td>
        <td><div class="fm-name"><span class="fi">${iconOf(e)}</span>
          <span class="txt">${esc(e.name)}</span></div></td>
        <td class="muted">${e.is_dir ? "-" : fmtSize(e.size)}</td>
        <td class="muted">${fmtTime(e.mtime)}</td>
        <td>${e.is_dir ? "" : badge(e.parse_status)}</td>
      </tr>`).join("");
    $$(".fm-row", box).forEach((tr) => {
      tr.addEventListener("click", (ev) => onRowClick(ev, Number(tr.dataset.i)));
      tr.addEventListener("dblclick", () => activate(Number(tr.dataset.i)));
      tr.addEventListener("contextmenu", (ev) => {
        ev.preventDefault();
        const i = Number(tr.dataset.i);
        if (!state.selected.has(state.entries[i].rel)) selectOnly(i);
        showCtx(ev.clientX, ev.clientY, state.entries[i]);
      });
    });
    $$("input[type=checkbox]", box).forEach((cb) => {
      cb.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const rel = cb.dataset.rel;
        if (cb.checked) state.selected.add(rel); else state.selected.delete(rel);
        reflectSelection();
      });
    });
    const all = $("#fm-check-all");
    if (all) all.checked = false;
    reflectSelection();
  }

  function onRowClick(ev, i) {
    const e = state.entries[i];
    if (!e) return;
    if (ev.shiftKey && state.lastIndex >= 0) {
      const [a, b] = [state.lastIndex, i].sort((x, y) => x - y);
      if (!(ev.ctrlKey || ev.metaKey)) state.selected.clear();
      for (let k = a; k <= b; k++) state.selected.add(state.entries[k].rel);
    } else if (ev.ctrlKey || ev.metaKey) {
      if (state.selected.has(e.rel)) state.selected.delete(e.rel); else state.selected.add(e.rel);
    } else {
      selectOnly(i);
    }
    state.lastIndex = i;
    reflectSelection();
  }

  function selectOnly(i) {
    state.selected.clear();
    if (state.entries[i]) state.selected.add(state.entries[i].rel);
    state.lastIndex = i;
    reflectSelection();
  }

  function reflectSelection() {
    $$(".fm-row").forEach((tr) => tr.classList.toggle("selected", state.selected.has(tr.dataset.rel)));
    $$(".fm-row input[type=checkbox]").forEach((cb) => { cb.checked = state.selected.has(cb.dataset.rel); });
    const all = $("#fm-check-all");
    if (all) all.checked = state.entries.length > 0 && state.selected.size === state.entries.length;
  }

  function activate(i) {
    const e = state.entries[i];
    if (!e) return;
    if (e.is_dir) open(state.sourceId, e.rel);
    else preview(e);
  }

  /* ------------------------------------------------------------ 搜索 */
  function bindSearch() {
    const input = $("#fm-search");
    if (!input || input.dataset.bound) return;
    input.dataset.bound = "1";
    input.addEventListener("input", () => {
      clearTimeout(state.searchTimer);
      const q = input.value.trim();
      if (!q) { hideSearch(); return; }
      state.searchTimer = setTimeout(() => runSearch(q), 150);
    });
    input.addEventListener("keydown", (ev) => {
      if (!state.search.length) return;
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        state.searchIndex = Math.min(state.search.length - 1, state.searchIndex + 1);
        paintSearchActive();
      } else if (ev.key === "ArrowUp") {
        ev.preventDefault();
        state.searchIndex = Math.max(0, state.searchIndex - 1);
        paintSearchActive();
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        if (state.searchIndex >= 0) activateSearch(state.searchIndex);
      } else if (ev.key === "Escape") {
        hideSearch();
      }
    });
  }

  async function runSearch(q) {
    try {
      const r = await api(`/api/v1/fs/search?q=${encodeURIComponent(q)}&limit=30`);
      state.search = r.results || [];
      state.searchIndex = state.search.length ? 0 : -1;
      renderSearch();
    } catch (e) {
      state.search = [];
      renderSearch("搜索失败：" + e.message);
    }
  }

  function renderSearch(err) {
    const box = $("#fm-search-results");
    if (!box) return;
    box.classList.remove("hidden");
    if (!state.search.length) {
      box.innerHTML = `<div class="fm-search-empty">${esc(err || "没有匹配的文件名")}</div>`;
      return;
    }
    box.innerHTML = state.search.map((r, i) => `
      <div class="fm-search-item ${i === state.searchIndex ? "active" : ""}" data-i="${i}">
        <div>${esc(r.name)}</div>
        <div class="p">${esc(r.source_name || "")}${r.rel ? " / " + esc(r.rel) : ""}</div>
      </div>`).join("");
    $$(".fm-search-item", box).forEach((el) => {
      el.addEventListener("mousedown", (ev) => { ev.preventDefault(); activateSearch(Number(el.dataset.i)); });
    });
  }

  function paintSearchActive() {
    $$(".fm-search-item").forEach((el) => el.classList.toggle("active", Number(el.dataset.i) === state.searchIndex));
  }

  function hideSearch() {
    const box = $("#fm-search-results");
    if (box) box.classList.add("hidden");
    state.search = [];
    state.searchIndex = -1;
  }

  async function activateSearch(i) {
    const r = state.search[i];
    if (!r) return;
    hideSearch();
    const input = $("#fm-search");
    if (input) input.value = "";
    await open(r.source_id, r.is_dir ? r.rel : parentRel(r.rel));
    if (!r.is_dir) {
      const hit = state.entries.find((e) => e.rel === r.rel);
      preview(hit || { rel: r.rel, name: r.name, is_dir: false, size: r.size, mtime: r.mtime });
    }
  }

  /* ------------------------------------------------------------ 预览 */
  function preview(entry, sourceId) {
    const sid = sourceId || state.sourceId;
    const box = $("#fm-preview");
    if (!box) return;
    const ext = extOf(entry.name);
    box.classList.remove("hidden");
    $(".fm-shell").classList.add("has-preview");
    box.innerHTML = `
      <div class="fm-preview-head">
        <span class="name" title="${esc(entry.rel)}">${esc(entry.name)}</span>
        <button class="sm" id="fm-prev-download">下载</button>
        <button class="sm" id="fm-prev-close">关闭</button>
      </div>
      <div class="fm-preview-body" id="fm-prev-body"><div class="preview-note">加载中…</div></div>
      <div class="fm-preview-foot muted">${esc(entry.is_dir ? "" : fmtSize(entry.size))}</div>`;
    $("#fm-prev-close").onclick = closePreview;
    $("#fm-prev-download").onclick = () => download(entry, sid);
    const body = $("#fm-prev-body");
    const url = previewUrl(sid, entry.rel);
    if (TEXT.has(ext)) {
      body.innerHTML = '<pre class="preview-note">加载中…</pre>';
      fetch(url).then(async (resp) => {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        const txt = await resp.text();
        body.innerHTML = `<pre>${esc(txt)}</pre>`;
      }).catch((err) => { body.innerHTML = `<div class="preview-note">预览失败：${esc(err.message)}</div>`; });
    } else if (IMAGE.has(ext)) {
      body.innerHTML = `<img alt="${esc(entry.name)}" src="${url}">`;
    } else if (VIDEO.has(ext)) {
      body.innerHTML = `<video controls src="${url}"></video>`;
    } else if (OFFICE.has(ext)) {
      body.innerHTML = `<iframe src="${url}" title="${esc(entry.name)}"></iframe>`;
    } else {
      body.innerHTML = `<div class="preview-note">该类型暂不支持在线预览<br><button class="primary" id="fm-prev-dl2">下载文件</button></div>`;
      const b = $("#fm-prev-dl2");
      if (b) b.onclick = () => download(entry, sid);
    }
  }

  function closePreview() {
    const box = $("#fm-preview");
    if (box) { box.classList.add("hidden"); box.innerHTML = ""; }
    const shell = $(".fm-shell");
    if (shell) shell.classList.remove("has-preview");
  }

  function download(entry, sourceId) {
    const a = document.createElement("a");
    a.href = downloadUrl(sourceId || state.sourceId, entry.rel);
    a.download = entry.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  /* ------------------------------------------------------------ 右键菜单 */
  function showCtx(x, y, entry) {
    const box = $("#fm-ctx");
    if (!box) return;
    state.ctxEntry = entry;
    box.innerHTML = `
      <button data-act="preview">预览</button>
      <button data-act="download">下载</button>
      <button data-act="copy">复制路径</button>
      ${state.canWrite ? '<button data-act="delete" class="danger">删除</button>' : ""}`;
    box.classList.remove("hidden");
    box.style.left = Math.min(x, window.innerWidth - 190) + "px";
    box.style.top = Math.min(y, window.innerHeight - 190) + "px";
    $$("button", box).forEach((b) => b.onclick = () => {
      const act = b.dataset.act;
      hideCtx();
      if (act === "preview") preview(entry);
      if (act === "download") download(entry, state.sourceId);
      if (act === "copy") copyPath(entry);
      if (act === "delete") deleteRels([entry.rel]);
    });
  }
  function hideCtx() {
    const box = $("#fm-ctx");
    if (box) box.classList.add("hidden");
  }
  function copyPath(entry) {
    const text = `${state.sourceName}/${entry.rel}`;
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast("路径已复制")).catch(() => toast(text));
    else toast(text);
  }

  /* ------------------------------------------------------------ 删除 */
  async function deleteRels(rels) {
    if (!rels.length) return;
    const names = rels.map((r) => r.split("\\").pop()).join("、");
    if (!confirm(`确定删除 ${rels.length} 项？\n${names}\n\n删除后可在回收站恢复。`)) return;
    try {
      const r = await api("/api/v1/fs/delete", { method: "POST", body: {
        source_id: state.sourceId, rels: rels } });
      const n = (r.deleted || []).length;
      toast(n ? `已移入回收站 ${n} 项` : "没有删除任何文件");
      if ((r.skipped || []).length) toast("部分跳过：" + r.skipped.map((s) => s.reason).join("；"), 5000);
      state.selected.clear();
      await open(state.sourceId, state.rel);
    } catch (e) { toast("删除失败: " + e.message, 5000); }
  }

  function deleteSelected() {
    if (!state.canWrite) { toast("当前目录没有写权限"); return; }
    deleteRels(Array.from(state.selected));
  }

  /* ------------------------------------------------------------ 上传 */
  function bindUpload() {
    const btn = $("#fm-upload");
    const input = $("#fm-file");
    if (!btn || !input || btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => input.click());
    input.addEventListener("change", async () => {
      const files = Array.from(input.files || []);
      input.value = "";
      if (files.length) await upload(files);
    });
  }

  async function upload(files) {
    const panel = $("#fm-upload-panel");
    if (!panel) return;
    panel.classList.remove("hidden");
    for (const file of files) {
      const row = document.createElement("div");
      row.className = "up-item";
      row.innerHTML = `<div class="n"></div><div class="up-bar"><div class="up-fill"></div></div><div class="up-state">准备中</div>`;
      $(".n", row).textContent = file.name;
      panel.appendChild(row);
      const fill = $(".up-fill", row), st = $(".up-state", row);
      try {
        await uploadOne(file, (done, total, speed) => {
          const pct = total ? Math.min(100, done * 100 / total) : 0;
          fill.style.width = pct.toFixed(1) + "%";
          st.textContent = `${pct.toFixed(0)}% · ${fmtSize(speed)}/s`;
        });
        fill.style.width = "100%";
        st.textContent = "完成";
        st.className = "up-state ok";
      } catch (e) {
        st.textContent = "失败：" + e.message;
        st.className = "up-state err";
      }
    }
    await open(state.sourceId, state.rel);
    setTimeout(() => {
      $$(".up-state", panel).forEach((s) => { if (s.classList.contains("ok")) s.closest(".up-item").remove(); });
      if (!$$(".up-item", panel).length) panel.classList.add("hidden");
    }, 4000);
  }

  async function uploadOne(file, onProgress) {
    let init;
    try {
      init = await api("/api/v1/fs/upload/init", { method: "POST", body: {
        source_id: state.sourceId, rel: state.rel, name: file.name, size: file.size } });
    } catch (e) {
      if (!/已存在/.test(e.message)) throw e;
      if (!confirm(`“${file.name}”已存在，是否覆盖？`)) throw new Error("已取消");
      init = await api("/api/v1/fs/upload/init", { method: "POST", body: {
        source_id: state.sourceId, rel: state.rel, name: file.name, size: file.size,
        overwrite: true } });
    }
    const chunk = Math.max(1024, Number(init.chunk_size || 5 * 1024 * 1024));
    const started = Date.now();
    for (let offset = 0; offset < file.size; offset += chunk) {
      const blob = file.slice(offset, Math.min(file.size, offset + chunk));
      const r = await fetch(`/api/v1/fs/upload/${encodeURIComponent(init.upload_id)}?offset=${offset}`, {
        method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: blob,
      });
      if (!r.ok) {
        const txt = await r.text();
        let detail = txt;
        try { detail = JSON.parse(txt).detail || txt; } catch (e) { }
        throw new Error(detail || ("HTTP " + r.status));
      }
      const done = offset + blob.size;
      onProgress(done, file.size, done / Math.max(0.2, (Date.now() - started) / 1000));
    }
    const done = await api(`/api/v1/fs/upload/${encodeURIComponent(init.upload_id)}/complete`, { method: "POST" });
    return done;
  }

  /* ------------------------------------------------------------ 回收站 */
  async function trashLoad() {
    const box = $("#trash-list");
    if (!box) return;
    box.innerHTML = '<p class="muted">加载中…</p>';
    try {
      const r = await api("/api/v1/fs/trash");
      const items = r.items || [];
      if (!items.length) {
        box.innerHTML = '<div class="fm-empty">回收站是空的</div>';
        return;
      }
      const admin = !!(window.KB.me && window.KB.me.role === "admin");
      box.innerHTML = `<table><thead><tr><th>名称</th><th>原路径</th><th>删除时间</th>
        <th>大小</th><th>剩余天数</th><th>操作</th></tr></thead><tbody>
        ${items.map((t) => {
          const days = Math.max(0, Math.ceil(((t.expires_at || 0) - Date.now() / 1000) / 86400));
          return `<tr><td>${esc(t.name)}</td><td class="muted">${esc(t.source_name)} / ${esc(t.original_rel)}</td>
            <td class="muted">${fmtTime(t.deleted_at)}</td><td class="muted">${t.is_dir ? "-" : fmtSize(t.size)}</td>
            <td>${days} 天</td><td>
              <button class="sm" onclick="KB.trash.restore(${t.id})">恢复</button>
              ${admin ? `<button class="sm" onclick="KB.trash.purge(${t.id})">彻底删除</button>` : ""}
            </td></tr>`;
        }).join("")}</tbody></table>`;
    } catch (e) {
      box.innerHTML = `<p class="err">加载失败：${esc(e.message)}</p>`;
    }
  }

  async function trashRestore(id) {
    try {
      await api(`/api/v1/fs/trash/${id}/restore`, { method: "POST" });
      toast("已恢复");
      await trashLoad();
    } catch (e) { toast("恢复失败: " + e.message, 5000); }
  }
  async function trashPurge(id) {
    if (!confirm("彻底删除后无法恢复，确定？")) return;
    try {
      await api(`/api/v1/fs/trash/${id}`, { method: "DELETE" });
      toast("已彻底删除");
      await trashLoad();
    } catch (e) { toast("删除失败: " + e.message, 5000); }
  }

  /* ------------------------------------------------------------ 键盘与全局事件 */
  function bindKeyboard() {
    document.addEventListener("keydown", (ev) => {
      const page = $("#p-files");
      if (!page || !page.classList.contains("active")) return;
      const tag = (ev.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;
      if (ev.key === "Escape") {
        if (!$("#fm-preview").classList.contains("hidden")) closePreview();
        else if (!$("#fm-ctx").classList.contains("hidden")) hideCtx();
        return;
      }
      if (ev.key === "Delete" || ev.key === "Backspace") {
        if (state.selected.size) { ev.preventDefault(); deleteSelected(); }
        return;
      }
      if (ev.key === "Enter" && state.selected.size === 1) {
        ev.preventDefault();
        const el = Array.from(state.selected)[0];
        const e = state.entries.find((x) => x.rel === el);
        if (e) e.is_dir ? open(state.sourceId, e.rel) : preview(e);
      }
    });
    document.addEventListener("click", (ev) => {
      if (!$("#fm-ctx").contains(ev.target) && !ev.target.closest(".fm-row")) hideCtx();
      if (!$("#fm-search-results").contains(ev.target) && ev.target.id !== "fm-search") hideSearch();
    });
    window.addEventListener("resize", hideCtx);
  }

  function bindHeader() {
    const all = $("#fm-check-all");
    if (!all || all.dataset.bound) return;
    all.dataset.bound = "1";
    all.addEventListener("change", () => {
      state.selected.clear();
      if (all.checked) state.entries.forEach((e) => state.selected.add(e.rel));
      reflectSelection();
    });
  }

  /* ------------------------------------------------------------ 启动/导出 */
  function boot() {
    bindSearch();
    bindUpload();
    bindKeyboard();
    bindHeader();
    const tr = $("#trash-refresh");
    if (tr && !tr.dataset.bound) {
      tr.dataset.bound = "1";
      tr.addEventListener("click", trashLoad);
    }
  }

  KB.files = {
    load, open, refresh: () => open(state.sourceId, state.rel),
    preview, closePreview, upload, deleteSelected,
    getState: () => state,
  };
  KB.trash = { load: trashLoad, restore: trashRestore, purge: trashPurge };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
