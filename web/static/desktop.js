/* 功能桌面：只负责图标点击与 hash 跳转，不直接调 API。 */
window.KB = window.KB || {};
KB.desktop = {
  mount() {
    const grid = document.querySelector("#desktop-grid");
    if (!grid || grid.dataset.bound) return;
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
  },
};
