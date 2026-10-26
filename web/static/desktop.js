/* 功能桌面：图标点击与 hash 跳转 + 顶部全局搜索框。 */
window.KB = window.KB || {};
KB.desktop = {
  mount() {
    const grid = document.querySelector("#desktop-grid");
    if (grid && !grid.dataset.bound) {
      grid.dataset.bound = "1";
      grid.addEventListener("click", (e) => {
        const card = e.target.closest("[data-open]");
        if (!card) return;
        const map = {
          files: "#/files", ask: "#/ask", graph: "#/graph",
          board: "#/board", trash: "#/trash", admin: "#/admin",
        };
        location.hash = map[card.dataset.open] || "#/desktop";
      });
    }
    const input = document.getElementById("desk-q");
    const box = document.getElementById("desk-results");
    if (!input || !box || input.dataset.bound) return;
    input.dataset.bound = "1";

    let timer = 0;
    let seq = 0;
    const esc = (s) => String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

    const close = () => { box.classList.add("hidden"); box.innerHTML = ""; };

    // 点开一条命中：文件名命中直接定位；正文命中先问它在哪
    const openHit = async (hit) => {
      try {
        if (hit.kind === "file") {
          location.hash = "#/files";
          setTimeout(() => {
            window.KB && KB.files && KB.files.openAt(hit.source_id, hit.rel, true);
          }, 150);
          return;
        }
        const r = await api(`/api/v1/documents/${hit.document_id}/file`);
        if (!r || !r.in_fs) { location.hash = "#/board"; return; }
        location.hash = "#/files";
        setTimeout(() => {
          window.KB && KB.files && KB.files.openAt(r.source_id, r.rel, true);
        }, 150);
      } catch (e) { toast("打不开：" + e.message); }
    };

    const render = (items, note) => {
      if (!items.length) {
        box.innerHTML = `<div class="desk-empty">${note || "没有找到匹配的内容"}</div>`;
        box.classList.remove("hidden");
        return;
      }
      box.innerHTML = items.map((h, i) => `
        <button class="desk-hit" data-i="${i}">
          <b>${h.kind === "file" ? `<span class="tag">文件名</span>` : `<span class="tag">正文</span>`}${esc(h.title)}</b>
          <span>${esc(h.sub)}</span>
        </button>`).join("");
      box.classList.remove("hidden");
      box.querySelectorAll(".desk-hit").forEach((b) => {
        b.onclick = () => { close(); openHit(items[Number(b.dataset.i)]); };
      });
    };

    const run = async () => {
      const q = input.value.trim();
      if (!q) { close(); return; }
      const my = ++seq;
      // 文件名搜索：快，先出结果
      let fileHits = [];
      try {
        const r = await api("/api/v1/fs/search?q=" + encodeURIComponent(q) + "&limit=12");
        if (my !== seq) return;
        fileHits = (r.results || []).map((x) => ({
          kind: "file", source_id: x.source_id, rel: x.rel,
          title: x.name, sub: (x.source_name || "") + " · " + (x.rel || ""),
        }));
      } catch (e) { /* 文件名搜索失败不影响正文搜索 */ }
      if (my !== seq) return;
      if (fileHits.length) render(fileHits);
      else { box.innerHTML = '<div class="desk-empty">搜索中…</div>'; box.classList.remove("hidden"); }

      // 正文搜索：慢，随后补充
      try {
        const r = await api("/api/v1/search", {
          method: "POST", body: { query: q, top_k: 8 },
        });
        if (my !== seq) return;
        const docHits = (r.results || []).map((x) => ({
          kind: "doc", document_id: x.document_id,
          title: x.file, sub: (x.snippet || "").replace(/\s+/g, " ").slice(0, 90),
        }));
        const all = fileHits.concat(docHits).slice(0, 14);
        if (!all.length) render([], "没有找到匹配的内容");
        else render(all);
      } catch (e) {
        if (my !== seq) return;
        if (!fileHits.length) render([], "没有找到匹配的内容");
      }
    };

    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(run, 260);
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { close(); input.blur(); }
      if (e.key === "Enter") {
        e.preventDefault();
        const q = input.value.trim();
        if (!q) return;
        clearTimeout(timer);
        close();
        // 回车 = 去文件页按这个名字找
        location.hash = "#/files";
        setTimeout(() => {
          const inp = document.getElementById("fm-search");
          if (inp) {
            inp.value = q;
            inp.dispatchEvent(new Event("input", { bubbles: true }));
            inp.dispatchEvent(new Event("keydown", { bubbles: true }));
          }
        }, 220);
      }
    });
    document.addEventListener("click", (e) => {
      if (!e.target.closest(".desk-search")) close();
    });
  },
};
