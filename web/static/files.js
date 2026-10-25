/* 文件管理器与回收站：浏览、秒搜、预览、上传、删除、恢复。 */
window.KB = window.KB || {};
(function () {
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  const state = {
    sources: [], sourceId: null, sourceName: "", rel: "", entries: [],
    byRel: new Map(), viewOrder: [], collapsed: new Set(),
    canWrite: false, selected: new Set(), lastRel: "",
    search: [], searchIndex: -1, searchTimer: 0, ctxEntry: null,
    contentSearch: [], uploads: 0,
  };

  const IMAGE = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico"]);
  const TEXT = new Set(["txt", "md", "csv", "log", "json", "xml", "html", "htm", "ini", "yaml", "yml"]);
  const VIDEO = new Set(["mp4", "webm", "ogg", "mov", "m4a", "mp3", "wav"]);
  const OFFICE = new Set(["docx", "xlsx", "pptx", "doc", "xls", "ppt", "ofd", "pdf"]);

  const parentRel = (rel) => (rel && rel.includes("\\") ? rel.slice(0, rel.lastIndexOf("\\")) : "");
  const passes = (rel) => rel.split("\\").map(encodeURIComponent).join("/");
  const previewUrl = (sourceId, rel) =>
    `/api/v1/fs/preview?source_id=${encodeURIComponent(sourceId)}&rel=${passes(rel)}`;
  const downloadUrl = (sourceId, rel) =>
    `/api/v1/fs/download?source_id=${encodeURIComponent(sourceId)}&rel=${passes(rel)}`;

  /* ------------------------------------------------------------ 视图偏好
     图标/列表视图、排序、分组都按浏览器记住（同一台电脑保持习惯）。 */
  const VIEW_KEY = "kb.files.view";
  const view = { mode: "list", sortKey: "mtime", sortDir: "desc", groupBy: "none" };

  function loadView() {
    try {
      const raw = localStorage.getItem(VIEW_KEY);
      if (!raw) return;
      const v = JSON.parse(raw) || {};
      if (v.mode === "icons" || v.mode === "list") view.mode = v.mode;
      if (["name", "size", "ext", "mtime"].includes(v.sortKey)) view.sortKey = v.sortKey;
      if (v.sortDir === "asc" || v.sortDir === "desc") view.sortDir = v.sortDir;
      if (["none", "name", "ext", "size", "mtime"].includes(v.groupBy)) view.groupBy = v.groupBy;
    } catch (e) { /* 偏好坏了就用默认值 */ }
  }
  function saveView() {
    try { localStorage.setItem(VIEW_KEY, JSON.stringify(view)); } catch (e) { }
  }

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

  /* ------------------------------------------------------------ 排序与分组（纯逻辑）
     这段不依赖 DOM，可以被单独抽出来跑断言。 */
  /* ===== VIEW_LOGIC_START ===== */

  const TYPE_COLORS = {
    doc: "#2b5797", sheet: "#217346", slides: "#c43e1c", pdf: "#c50f1f",
    text: "#5b6770", image: "#0f9d8f", video: "#7c3aed", audio: "#d81b60",
    archive: "#b45309", code: "#0b7285", default: "#64748b",
  };
  const TYPE_NAMES = {
    doc: "文档", sheet: "表格", slides: "演示", pdf: "PDF", text: "文本",
    image: "图片", video: "视频", audio: "音频", archive: "压缩包", code: "代码",
    default: "文件",
  };

  function typeOf(name) {
    const x = extOf(name);
    if (["doc", "docx", "rtf", "odt"].includes(x)) return "doc";
    if (["xls", "xlsx", "csv", "ods"].includes(x)) return "sheet";
    if (["ppt", "pptx", "odp"].includes(x)) return "slides";
    if (x === "pdf") return "pdf";
    if (["txt", "md", "log", "ini", "yaml", "yml"].includes(x)) return "text";
    if (["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico", "tif", "tiff"].includes(x)) return "image";
    if (["mp4", "mov", "avi", "mkv", "wmv", "flv", "webm"].includes(x)) return "video";
    if (["mp3", "wav", "m4a", "flac", "aac"].includes(x)) return "audio";
    if (["zip", "rar", "7z", "tar", "gz", "bz2"].includes(x)) return "archive";
    if (["py", "js", "ts", "json", "xml", "html", "htm", "css", "java", "go", "sql", "sh"].includes(x)) return "code";
    return "default";
  }

  function typeLabel(e) {
    const x = extOf(e.name);
    if (!x) return "文件";
    return x.toUpperCase();
  }

  /** 文件图标：文件夹是黄色文件夹，其它按类型上色并标扩展名（Windows 风格）。 */
  function fileIcon(e) {
    if (e.is_dir) {
      return `<svg class="ico ico-dir" viewBox="0 0 32 32" aria-hidden="true">
        <path d="M3 7h9l3 3h14v18H3z" fill="#e0a92c" stroke="#b98613" stroke-width="1"/>
        <path d="M3 13h26v15H3z" fill="#f7c948" stroke="#b98613" stroke-width="1"/></svg>`;
    }
    const t = typeOf(e.name);
    const color = TYPE_COLORS[t] || TYPE_COLORS.default;
    const tag = extOf(e.name).toUpperCase().slice(0, 4) || "";
    return `<svg class="ico" viewBox="0 0 32 32" aria-hidden="true">
      <path d="M6 2h12l8 8v20H6z" fill="#eef2f6" stroke="#9fb0c0" stroke-width="1"/>
      <path d="M18 2l8 8h-8z" fill="#c9d6e2" stroke="#9fb0c0" stroke-width="1"/>
      <rect x="6" y="22" width="20" height="8" rx="1.5" fill="${color}"/>
      <text x="16" y="28" font-size="6.2" text-anchor="middle" fill="#ffffff"
        font-family="Segoe UI,Arial,sans-serif">${esc(tag)}</text></svg>`;
  }

  /** 首字母分组：英文取字母，中文按拼音首字母，数字/符号单独一段。 */
  function initialOf(name) {
    const s = String(name || "").trim();
    if (!s) return "#";
    const ch = s[0];
    if (/[a-zA-Z]/.test(ch)) return ch.toUpperCase();
    if (/[0-9]/.test(ch)) return "0-9";
    let lo = 0, hi = 25, ans = 25;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (collator.compare(ch, String.fromCharCode(65 + mid)) >= 0) { ans = mid; lo = mid + 1; }
      else hi = mid - 1;
    }
    return String.fromCharCode(65 + ans);
  }

  /** 排序：文件夹永远排在文件前面，其余按指定字段升降序。 */
  function sortEntries(entries, key, dir) {
    const sign = dir === "asc" ? 1 : -1;
    return (entries || []).slice().sort((a, b) => {
      if (!!a.is_dir !== !!b.is_dir) return a.is_dir ? -1 : 1;
      let r = 0;
      if (key === "size") r = (a.size || 0) - (b.size || 0);
      else if (key === "mtime") r = (a.mtime || 0) - (b.mtime || 0);
      else if (key === "ext") r = collator.compare(extOf(a.name), extOf(b.name));
      else r = collator.compare(a.name, b.name);
      if (r === 0) r = collator.compare(a.name, b.name);
      return r * sign;
    });
  }

  const SIZE_BUCKETS = [
    [1024, "小于 1 KB"],
    [10 * 1024, "1 KB – 10 KB"],
    [100 * 1024, "10 KB – 100 KB"],
    [1024 * 1024, "100 KB – 1 MB"],
    [10 * 1024 * 1024, "1 MB – 10 MB"],
    [100 * 1024 * 1024, "10 MB – 100 MB"],
    [Infinity, "大于 100 MB"],
  ];

  function groupKeyOf(e, groupBy) {
    if (groupBy === "name") return initialOf(e.name);
    if (groupBy === "ext") {
      const x = extOf(e.name);
      return x ? x.toUpperCase() : "无扩展名";
    }
    if (groupBy === "size") {
      const n = e.size || 0;
      for (const [lim, label] of SIZE_BUCKETS) if (n < lim) return label;
      return SIZE_BUCKETS[SIZE_BUCKETS.length - 1][1];
    }
    if (groupBy === "mtime") {
      if (!e.mtime) return "未知时间";
      const d = new Date(e.mtime * 1000);
      return `${d.getFullYear()} 年 ${d.getMonth() + 1} 月`;
    }
    return "";
  }

  /** 分组：文件夹单独一组在前，其余按分组键分段。 */
  function groupEntries(entries, groupBy, sortKey, sortDir) {
    const sorted = sortEntries(entries, sortKey, sortDir);
    if (!groupBy || groupBy === "none") return [{ label: "", items: sorted }];
    const dirs = sorted.filter((e) => e.is_dir);
    const files = sorted.filter((e) => !e.is_dir);
    const buckets = new Map();
    for (const e of files) {
      const k = groupKeyOf(e, groupBy);
      if (!buckets.has(k)) buckets.set(k, []);
      buckets.get(k).push(e);
    }
    let keys = [...buckets.keys()];
    if (groupBy === "size") {
      keys = SIZE_BUCKETS.map((b) => b[1]).filter((k) => buckets.has(k));
    } else if (groupBy === "mtime") {
      keys.sort((a, b) => {
        const ta = Math.max(...buckets.get(a).map((e) => e.mtime || 0));
        const tb = Math.max(...buckets.get(b).map((e) => e.mtime || 0));
        return tb - ta;
      });
    } else {
      keys.sort((a, b) => collator.compare(a, b));
    }
    const groups = [];
    if (dirs.length) groups.push({ label: "文件夹", items: dirs, kind: "dirs" });
    for (const k of keys) groups.push({ label: k, items: buckets.get(k) });
    return groups;
  }

  const collator = new Intl.Collator("zh-Hans-u-co-pinyin", { numeric: true, sensitivity: "base" });
  const extOf = (name) => (String(name).split(".").pop() || "").toLowerCase();
  /* ===== VIEW_LOGIC_END ===== */

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
      await ensureSources();
      if (!state.sources.length) return;
      // 有"待定位"的目标（看板/问答跳过来的），优先处理它
      if (pendingAt) {
        const t = pendingAt;
        pendingAt = null;
        await doOpenAt(t.sourceId, t.rel, t.previewIt);
        return;
      }
      // 已经打开过某个目录就别覆盖（避免把刚定位好的位置冲掉）
      if (state.sourceId && state.entries.length) return;
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
      state.byRel = new Map(state.entries.map((e) => [e.rel, e]));
      state.canWrite = !!r.can_write;
      state.selected.clear();
      state.lastRel = "";
      state.collapsed.clear();
      renderSources();
      renderCrumbs(r.breadcrumb || []);
      bindViewControls();
      setViewMode(view.mode);
      $("#fm-upload").disabled = !state.canWrite;
      $("#fm-upload").title = state.canWrite ? "" : "当前目录没有写权限";
      hideSearch();
      hideCtx();
    } catch (e) {
      renderEmpty("无法打开目录：" + e.message);
    }
  }

  /** 从看板/问答跳过来：打开某个文件所在的目录，可选直接预览。
   *  可能发生在文件页刚被路由打开、还没加载完的时候，所以用 pendingAt 交接。 */
  let pendingAt = null;
  let sourcesPromise = null;

  function ensureSources() {
    if (!sourcesPromise) sourcesPromise = loadSources();
    return sourcesPromise;
  }

  async function doOpenAt(sourceId, rel, previewIt) {
    const dir = parentRel(rel);
    await open(Number(sourceId), dir);
    const hit = state.entries.find((e) => e.rel === rel);
    if (hit) {
      selectOnly(hit);
      if (previewIt) preview(hit);
      else toast("已定位到文件所在目录");
    } else {
      toast("目录已打开，但列表里没找到该文件（可能已被移动）");
    }
  }

  async function openAt(sourceId, rel, previewIt) {
    rel = rel || "";
    pendingAt = { sourceId: Number(sourceId), rel, previewIt };
    try { await ensureSources(); } catch (e) { /* 交给调用方报错 */ }
    if (state.sources.length && pendingAt) {
      const t = pendingAt;
      pendingAt = null;
      await doOpenAt(t.sourceId, t.rel, t.previewIt);
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
    if (box) box.innerHTML = `<tr><td colspan="6"><div class="fm-empty">${esc(msg)}</div></td></tr>`;
  }

  /* ------------------------------------------------------------ 列表渲染（表格 / 图标） */
  function renderList() {
    const entries = state.entries || [];
    if (view.mode === "icons") return renderIcons(entries);
    const box = $("#fm-list");
    if (!box) return;
    if (!entries.length) { renderEmpty("这个目录是空的"); return; }
    const groups = groupEntries(entries, view.groupBy, view.sortKey, view.sortDir);
    state.viewOrder = [];
    const rows = [];
    for (const g of groups) {
      if (g.label) {
        const collapsed = state.collapsed.has(g.label);
        rows.push(`<tr class="fm-group" data-group="${esc(g.label)}"><td colspan="6">
          <span class="tw">${collapsed ? "▸" : "▾"}</span>
          <span class="gl">${esc(g.label)}</span>
          <span class="muted">${g.items.length} 项</span></td></tr>`);
        if (collapsed) continue;
      }
      for (const e of g.items) {
        state.viewOrder.push(e.rel);
        rows.push(rowHtml(e));
      }
    }
    box.innerHTML = rows.join("");
    bindRows(box);
    const all = $("#fm-check-all");
    if (all) all.checked = false;
    reflectSelection();
    paintSortIndicators();
  }

  function rowHtml(e) {
    return `<tr class="fm-row" data-rel="${esc(e.rel)}" data-dir="${e.is_dir ? 1 : 0}">
        <td class="fm-check"><input type="checkbox" data-rel="${esc(e.rel)}"></td>
        <td><div class="fm-name"><span class="fi">${fileIcon(e)}</span>
          <span class="txt">${esc(e.name)}</span></div></td>
        <td class="muted">${e.is_dir ? "" : fmtSize(e.size)}</td>
        <td class="muted">${e.is_dir ? "" : typeLabel(e)}</td>
        <td class="muted">${fmtTime(e.mtime)}</td>
        <td>${e.is_dir ? "" : badge(e.parse_status)}</td>
      </tr>`;
  }

  function renderIcons(entries) {
    const box = $("#fm-icons");
    if (!box) return;
    if (!entries.length) {
      box.innerHTML = `<div class="fm-empty">这个目录是空的</div>`;
      return;
    }
    const sorted = sortEntries(entries, view.sortKey, view.sortDir);
    state.viewOrder = sorted.map((e) => e.rel);
    box.innerHTML = sorted.map((e) => `
      <div class="fm-icon${state.selected.has(e.rel) ? " selected" : ""}" data-rel="${esc(e.rel)}"
           data-dir="${e.is_dir ? 1 : 0}" title="${esc(e.name)}">
        <div class="ic">${fileIcon(e)}</div>
        <div class="nm">${esc(e.name)}</div>
        <div class="mt muted">${e.is_dir ? "文件夹" : fmtSize(e.size)}</div>
      </div>`).join("");
    bindRows(box);
    const all = $("#fm-check-all");
    if (all) all.checked = false;
    reflectSelection();
    paintSortIndicators();
  }

  function bindRows(box) {
    $$(".fm-row, .fm-icon", box).forEach((el) => {
      const entry = state.byRel.get(el.dataset.rel);
      if (!entry) return;
      el.addEventListener("click", (ev) => onRowClick(ev, entry));
      el.addEventListener("dblclick", () => activate(entry));
      el.addEventListener("contextmenu", (ev) => {
        ev.preventDefault();
        if (!state.selected.has(entry.rel)) selectOnly(entry);
        showCtx(ev.clientX, ev.clientY, entry);
      });
    });
    $$(".fm-group", box).forEach((el) => {
      el.addEventListener("click", () => {
        const key = el.dataset.group;
        if (state.collapsed.has(key)) state.collapsed.delete(key);
        else state.collapsed.add(key);
        renderList();
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
  }

  function onRowClick(ev, e) {
    if (!e) return;
    if (ev.shiftKey && state.lastRel) {
      const order = state.viewOrder || [];
      const a = order.indexOf(state.lastRel);
      const b = order.indexOf(e.rel);
      if (a >= 0 && b >= 0) {
        const [lo, hi] = a < b ? [a, b] : [b, a];
        if (!(ev.ctrlKey || ev.metaKey)) state.selected.clear();
        for (let k = lo; k <= hi; k++) state.selected.add(order[k]);
      }
    } else if (ev.ctrlKey || ev.metaKey) {
      if (state.selected.has(e.rel)) state.selected.delete(e.rel); else state.selected.add(e.rel);
    } else {
      selectOnly(e);
    }
    state.lastRel = e.rel;
    reflectSelection();
  }

  function selectOnly(e) {
    state.selected.clear();
    if (e) state.selected.add(e.rel);
    state.lastRel = e ? e.rel : "";
    reflectSelection();
  }

  function reflectSelection() {
    $$(".fm-row, .fm-icon").forEach((el) =>
      el.classList.toggle("selected", state.selected.has(el.dataset.rel)));
    $$(".fm-row input[type=checkbox]").forEach((cb) => { cb.checked = state.selected.has(cb.dataset.rel); });
    const all = $("#fm-check-all");
    if (all) all.checked = state.entries.length > 0 && state.selected.size === state.entries.length;
  }

  function activate(e) {
    if (!e) return;
    if (e.is_dir) open(state.sourceId, e.rel);
    else preview(e);
  }

  /* ------------------------------------------------------------ 视图控件（排序/分组/视图切换） */
  function bindViewControls() {
    const sortSel = $("#fm-sort");
    if (sortSel && !sortSel.dataset.bound) {
      sortSel.dataset.bound = "1";
      sortSel.value = view.sortKey;
      sortSel.addEventListener("change", () => {
        view.sortKey = sortSel.value;
        saveView();
        renderList();
      });
    }
    const dirBtn = $("#fm-sort-dir");
    if (dirBtn && !dirBtn.dataset.bound) {
      dirBtn.dataset.bound = "1";
      dirBtn.addEventListener("click", () => {
        view.sortDir = view.sortDir === "asc" ? "desc" : "asc";
        saveView();
        renderList();
      });
    }
    const groupSel = $("#fm-group");
    if (groupSel && !groupSel.dataset.bound) {
      groupSel.dataset.bound = "1";
      groupSel.value = view.groupBy;
      groupSel.addEventListener("change", () => {
        view.groupBy = groupSel.value;
        state.collapsed.clear();
        saveView();
        renderList();
      });
    }
    const ic = $("#fm-view-icons"), ls = $("#fm-view-list");
    if (ic && !ic.dataset.bound) {
      ic.dataset.bound = "1";
      ic.addEventListener("click", () => setViewMode("icons"));
    }
    if (ls && !ls.dataset.bound) {
      ls.dataset.bound = "1";
      ls.addEventListener("click", () => setViewMode("list"));
    }
    // 表头点击排序（点一次升序、再点降序）
    $$(".fm-table th[data-sort]").forEach((th) => {
      if (th.dataset.bound) return;
      th.dataset.bound = "1";
      th.addEventListener("click", () => {
        const key = th.dataset.sort;
        if (view.sortKey === key) view.sortDir = view.sortDir === "asc" ? "desc" : "asc";
        else {
          view.sortKey = key;
          view.sortDir = key === "size" || key === "mtime" ? "desc" : "asc";
        }
        saveView();
        const sortSel = $("#fm-sort");
        if (sortSel) sortSel.value = view.sortKey;
        renderList();
      });
    });
  }

  function setViewMode(mode) {
    view.mode = mode;
    saveView();
    const table = $("#fm-table-wrap"), icons = $("#fm-icons");
    if (table) table.classList.toggle("hidden", mode !== "list");
    if (icons) icons.classList.toggle("hidden", mode !== "icons");
    const ic = $("#fm-view-icons"), ls = $("#fm-view-list");
    if (ic) ic.classList.toggle("active", mode === "icons");
    if (ls) ls.classList.toggle("active", mode === "list");
    const groupSel = $("#fm-group");
    if (groupSel) {
      groupSel.disabled = mode !== "list";
      groupSel.title = mode === "list" ? "" : "分组只在列表（详细信息）视图可用";
    }
    renderList();
  }

  function paintSortIndicators() {
    const dirBtn = $("#fm-sort-dir");
    if (dirBtn) dirBtn.textContent = view.sortDir === "asc" ? "↑ 升序" : "↓ 降序";
    $$(".fm-table th[data-sort]").forEach((th) => {
      const on = th.dataset.sort === view.sortKey;
      th.classList.toggle("sorted", on);
      const arrow = th.querySelector(".ar");
      if (arrow) arrow.textContent = on ? (view.sortDir === "asc" ? " ▲" : " ▼") : "";
    });
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
    // 「搜正文」开关：勾上时把输入框里已有的词立刻搜一遍；清空则收起
    const cb = $("#fm-content-search");
    if (cb && !cb.dataset.bound) {
      cb.dataset.bound = "1";
      cb.addEventListener("change", () => {
        const q = input.value.trim();
        if (q) { clearTimeout(state.searchTimer); runSearch(q); }
        else hideContentSearch();
      });
    }
  }

  async function runSearch(q) {
    const cb = $("#fm-content-search");
    if (!cb || !cb.checked) hideContentSearch();
    try {
      const r = await api(`/api/v1/fs/search?q=${encodeURIComponent(q)}&limit=30`);
      state.search = r.results || [];
      state.searchIndex = state.search.length ? 0 : -1;
      renderSearch();
    } catch (e) {
      state.search = [];
      renderSearch("搜索失败：" + e.message);
    }
    // 「搜正文」勾选时，同一个词再走一遍全文检索（与文件名秒搜并列展示）
    if (cb && cb.checked) await searchContent(q, 10).then((hits) => {
      state.contentSearch = hits;
      renderContentSearch();
    }).catch((e) => {
      state.contentSearch = [];
      renderContentSearch("正文搜索失败：" + e.message);
    });
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
    const box = $("#fm-search-results");
    $$(".fm-search-item", box).forEach((el) => el.classList.toggle("active", Number(el.dataset.i) === state.searchIndex));
  }

  function hideSearch() {
    const box = $("#fm-search-results");
    if (box) box.classList.add("hidden");
    state.search = [];
    state.searchIndex = -1;
    hideContentSearch();
  }

  /* ------------------------------------------------------------ 搜正文
     后端 POST /api/v1/search：关键词+向量双路，chunk 级片段；
     scope 过滤在后端做（grants 是唯一授权源），前端不做二次过滤。 */
  const ROUTE_NAMES = { keyword: "关键词", vector: "语义", graph: "图谱", wiki: "校正" };

  async function searchContent(query, topK) {
    const r = await api("/api/v1/search", { method: "POST", body: {
      query: query, top_k: topK || 10 } });
    return r.results || [];
  }

  function dirLabel(path) {
    // path 形如 \\nas\share\目录\文件.pdf → 取「源 / 目录」作定位提示
    const parts = String(path || "").split("\\").filter(Boolean);
    if (parts.length < 3) return "";
    return parts[parts.length - 3] + " / " + parts[parts.length - 2];
  }

  function renderContentSearch(err) {
    const box = $("#fm-content-results");
    if (!box) return;
    box.classList.remove("hidden");
    const hits = state.contentSearch || [];
    if (!hits.length) {
      box.innerHTML = `<div class="fm-search-empty">${esc(err || "正文没有匹配")}</div>`;
      return;
    }
    box.innerHTML = hits.map((h, i) => {
      const snippet = h.restricted ? "涉密·原文不回传" : (h.snippet || "");
      const tags = (h.matched_by || []).map((m) => ROUTE_NAMES[m] || m).join(" / ");
      return `<div class="fm-search-item" data-i="${i}">
        <div>${esc(h.file)}${tags ? `<span class="tag">${esc(tags)}</span>` : ""}</div>
        <div class="p">${esc(dirLabel(h.path))}</div>
        <div class="s">${esc(snippet)}</div>
      </div>`;
    }).join("");
    $$(".fm-search-item", box).forEach((el) => {
      el.addEventListener("mousedown", (ev) => {
        ev.preventDefault();
        activateContent(hits[Number(el.dataset.i)]);
      });
    });
  }

  function hideContentSearch() {
    const box = $("#fm-content-results");
    if (box) box.classList.add("hidden");
    state.contentSearch = [];
  }

  async function activateContent(hit) {
    if (!hit) return;
    hideSearch();
    const input = $("#fm-search");
    if (input) input.value = "";
    let info = null;
    try {
      info = await api(`/api/v1/documents/${hit.document_id}/file`);
    } catch (e) { toast("定位失败：" + e.message, 5000); return; }
    if (info && info.in_fs && info.source_id != null) {
      // 跳到该文件所在目录：复用看板「定位」同一套 openAt/pendingAt 交接
      await openAt(info.source_id, info.rel, false);
    } else {
      toast("该文件还未接入 2.0 文件页");
    }
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
        else if (!$("#fm-search-results").classList.contains("hidden")
                 || !$("#fm-content-results").classList.contains("hidden")) hideSearch();
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
      const onSearchUI = ev.target.closest
        && ev.target.closest("#fm-search, .fm-csearch, #fm-search-results, #fm-content-results");
      if (!onSearchUI) hideSearch();
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
    loadView();
    bindSearch();
    bindUpload();
    bindKeyboard();
    bindHeader();
    bindViewControls();
    const tr = $("#trash-refresh");
    if (tr && !tr.dataset.bound) {
      tr.dataset.bound = "1";
      tr.addEventListener("click", trashLoad);
    }
  }

  KB.files = {
    load, open, openAt, refresh: () => open(state.sourceId, state.rel),
    preview, closePreview, upload, deleteSelected,
    searchContent,
    getState: () => state,
  };
  KB.trash = { load: trashLoad, restore: trashRestore, purge: trashPurge };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
