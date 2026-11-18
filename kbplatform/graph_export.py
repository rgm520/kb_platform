"""把知识图谱导出成 **Obsidian 直接能用的东西**。

为什么是这个方向（2026-10-26，用户意见）：
    自己手调力导向布局 —— 斥力参数、fit 的 maxZoom、布局收敛时序……
    全是在重新发明轮子，还老是不稳。Obsidian 的图谱视图、双链、搜索、
    Canvas、插件生态（Graph Analysis / Dataview / Breadcrumbs）都是现成的，
    而且是用户已经在用的工具。

    Obsidian 插件**不能**嵌进我们的网页（它们依赖 Obsidian 桌面端的
    `obsidian` 模块与本地库 API），所以可行的做法是：
    **把我们的图谱导出成 Obsidian 的库（.md + [[双链]]）和画布（.canvas）**，
    用户下载后用 Obsidian 打开，就得到原汁原味的图谱体验。

两种导出：
    * vault —— 一个 zip：每个实体/文档一篇 markdown，实体之间用 [[双链]]，
      Obsidian 的图谱视图会自动把双链长成图（还能按 frontmatter 的 tags 上色）；
    * canvas —— 一个 .canvas 文件（Obsidian 1.4+ 的无限画布），
      节点指向库里的笔记，Obsidian 原生渲染，可手工再编辑。

导出内容按**调用者的可见范围**过滤（和图谱页看到的一致）。
"""
from __future__ import annotations

import io
import json
import re
import time
import zipfile

# 节点类型 → Obsidian 标签（图谱视图按标签上色）
TYPE_TAGS = {
    "entity": "实体",
    "knowledge_point": "知识点",
    "document": "文档",
    "category": "分类",
}

_VAULT_MAX = 2000      # 库里最多多少篇笔记（再多 Obsidian 打开也慢）
_CANVAS_MAX = 300      # 画布最多多少节点
_EDGE_MAX = 4000


def _safe_name(name: str) -> str:
    """把实体名变成合法的文件名（Windows 禁用字符 + 结尾的点/空格）。"""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(name or "").strip())
    s = s.rstrip(". ")
    return s[:120] or "未命名"


def _unique(mapping: dict, name: str) -> str:
    """同名实体（不同 id）要不同的文件名，否则互相覆盖。"""
    base = _safe_name(name)
    key = base.lower()
    n = mapping.get(key, 0)
    mapping[key] = n + 1
    return base if n == 0 else f"{base} ({n + 1})"


def _md_escape(text: str) -> str:
    return str(text or "").replace("|", "\\|")


def collect_graph(conn, pred: str, params: list, limit: int) -> tuple[list[dict], list[dict]]:
    """按可见范围取节点与关系（与图谱页同一套过滤）。"""
    limit = max(10, min(int(limit or 200), _VAULT_MAX))
    rows = conn.execute(
        f"""SELECT n.id, n.name, n.type, n.mentions FROM nodes n
            WHERE n.merged_to IS NULL AND EXISTS (
              SELECT 1 FROM edges e JOIN chunks ch ON ch.id=e.chunk_id
              JOIN catalog c ON c.path=ch.file_path
              WHERE (e.src=n.id OR e.dst=n.id) AND {pred})
            ORDER BY n.mentions DESC LIMIT ?""",
        params + [limit]).fetchall()
    nodes = [{"id": r[0], "name": r[1], "type": r[2] or "entity",
              "mentions": r[3] or 0} for r in rows]
    ids = [n["id"] for n in nodes]
    edges: list[dict] = []
    if ids:
        marks = ",".join("?" * len(ids))
        erows = conn.execute(
            f"""SELECT e.src, e.dst, e.rel, c.name FROM edges e
                JOIN chunks ch ON ch.id=e.chunk_id
                JOIN catalog c ON c.path=ch.file_path
                WHERE e.src IN ({marks}) AND e.dst IN ({marks}) AND {pred}
                GROUP BY e.src, e.dst, e.rel LIMIT ?""",
            ids + ids + params + [_EDGE_MAX]).fetchall()
        edges = [{"src": r[0], "dst": r[1], "rel": r[2] or "关联",
                  "doc": r[3]} for r in erows]
    return nodes, edges


def _notes(nodes: list[dict], edges: list[dict]) -> dict:
    """把节点/关系摊成"一篇笔记的内容"。"""
    by_id = {n["id"]: n for n in nodes}
    # 关系：src → [(dst, rel)]，以及文档出处
    rels: dict[str, list[dict]] = {}
    docs: dict[str, set] = {}
    for e in edges:
        rels.setdefault(str(e["src"]), []).append(e)
        if e.get("doc"):
            docs.setdefault(str(e["src"]), set()).add(e["doc"])
    out = {}
    for n in nodes:
        nid = str(n["id"])
        tag = TYPE_TAGS.get(n.get("type") or "entity", "实体")
        lines = [
            "---",
            f"name: {_md_escape(n['name'])}",
            f"type: {_md_escape(n.get('type') or 'entity')}",
            f"tags: [{tag}]",
            f"mentions: {n.get('mentions') or 0}",
            "source: 企业知识平台",
            "---",
            "",
            f"# {n['name']}",
            "",
            f"> 由企业知识平台知识图谱导出 · 类型 {n.get('type') or 'entity'} · 被提及 {n.get('mentions') or 0} 次",
            "",
        ]
        rl = rels.get(nid) or []
        if rl:
            lines += ["## 关联", ""]
            for e in rl:
                tgt = by_id.get(str(e["dst"]))
                if not tgt:
                    continue
                lines.append(f"- [[{tgt['name']}]] （{e['rel']}）")
            lines.append("")
        if docs.get(nid):
            lines += ["## 出处", ""]
            for d in sorted(docs[nid]):
                lines.append(f"- {d}")
            lines.append("")
        out[nid] = "\n".join(lines)
    return out


README = """# 企业知识平台 · 知识图谱导出

这是从企业知识平台导出的 Obsidian 库。

## 怎么用

1. 下载并解压这个 zip（例如 `知识图谱.zip`）。
2. 装 Obsidian：https://obsidian.md （免费，个人/商业用途都可用）。
3. Obsidian → **打开文件夹作为仓库** → 选解压出来的这个目录。
4. 左侧点 **图谱视图**（Graph View）→ 双链会自动长成图谱。

## 目录里有什么

- `实体/` —— 实体笔记（人名、部门、系统、制度…），之间用 `[[双链]]` 相连
- `文档/` —— 文档笔记，列出了它涉及的实体
- `README.md` —— 就是这个文件

每篇笔记的 frontmatter 里有 `type` / `tags` / `mentions`：
图谱视图里可以按标签过滤和上色（实体=蓝、知识点=绿、文档=黄、分类=紫）。

## 想更好用，装这两个插件

- **Graph Analysis** —— 在图谱上加中心度、社区发现、最短路径（Obsidian 设置里搜名字）
- **Dataview** —— 把 frontmatter 当数据库查（例如"列出所有被提及 10 次以上的实体"）

## 说明

导出内容按**导出者当时的可见目录**过滤 —— 普通用户只会拿到自己授权目录里的图谱。
"""


def build_vault(conn, pred: str, params: list, limit: int = 500) -> bytes:
    """导出成一个 Obsidian 库（zip 字节流）。"""
    nodes, edges = collect_graph(conn, pred, params, limit)
    bodies = _notes(nodes, edges)
    by_id = {str(n["id"]): n for n in nodes}
    used: dict = {}
    # 文件名：实体一篇、文档一篇
    path_of: dict[str, str] = {}
    for n in nodes:
        folder = "文档" if (n.get("type") == "document") else "实体"
        fname = _unique(used, n["name"]) + ".md"
        path_of[str(n["id"])] = f"{folder}/{fname}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", README)
        z.writestr(".obsidian/app.json", json.dumps(
            {"alwaysUpdateLinks": True, "newLinkFormat": "shortest",
             "useMarkdownLinks": False, "showUnsupportedFiles": True},
            ensure_ascii=False, indent=2))
        z.writestr(".obsidian/graph.json", json.dumps(
            {"collapse-filter": True, "search": "", "showTags": True,
             "showAttachments": False, "hideUnresolved": False,
             "showOrphans": True, "collapse-color-groups": False,
             "colorGroups": [
                 {"query": "path:实体", "color": {"a": 1, "rgb": 6638910}},
                 {"query": "path:文档", "color": {"a": 1, "rgb": 15844367}},
             ]},
            ensure_ascii=False, indent=2))
        for nid, body in bodies.items():
            z.writestr(path_of[nid], body)
        # 双链：把 [[名称]] 换成 [[名称]]（Obsidian 自己按文件名解析，
        # 但同名实体会指向不同文件 —— 这里统一改成带序号的文件名）
        for nid, body in list(bodies.items()):
            fixed = body
            for other_id, other in by_id.items():
                if other_id == nid:
                    continue
                name = other["name"]
                if f"[[{name}]]" in fixed:
                    fixed = fixed.replace(f"[[{name}]]", f"[[{path_of[other_id][:-3]}]]")
            if fixed != body:
                z.writestr(path_of[nid], fixed)
    return buf.getvalue()


def build_canvas(conn, pred: str, params: list, limit: int = 200) -> dict:
    """导出成 Obsidian 画布（.canvas JSON）。"""
    nodes, edges = collect_graph(conn, pred, params, min(limit, _CANVAS_MAX))
    bodies = _notes(nodes, edges)
    used: dict = {}
    path_of: dict[str, str] = {}
    for n in nodes:
        folder = "文档" if (n.get("type") == "document") else "实体"
        path_of[str(n["id"])] = f"{folder}/{_unique(used, n['name'])}.md"
    by_id = {str(n["id"]): n for n in nodes}
    cnodes = []
    pos = {}
    # 圆周排布：Obsidian 里可以随便拖，初始位置只要不重叠就行
    R = 260 + len(nodes) * 4
    for i, n in enumerate(nodes):
        nid = str(n["id"])
        a = 2 * 3.14159265 * i / max(1, len(nodes))
        x = int(R * (1 + 0.55 * __import__("math").cos(a)))
        y = int(R * 0.62 * (1 + 0.55 * __import__("math").sin(a)))
        pos[nid] = (x, y)
        cnodes.append({
            "id": nid,
            "type": "file",
            "file": path_of[nid],
            "x": x, "y": y,
            "width": 320, "height": 200,
        })
    cedges = []
    for i, e in enumerate(edges):
        s, d = str(e["src"]), str(e["dst"])
        if s not in pos or d not in pos:
            continue
        cedges.append({
            "id": "e" + str(i),
            "fromNode": s, "fromSide": "right",
            "toNode": d, "toSide": "left",
            "fromEnd": "none", "toEnd": "arrow",
            "label": e["rel"],
        })
    return {"nodes": cnodes, "edges": cedges}


def canvas_zip_bytes(conn, pred: str, params: list, limit: int = 200) -> bytes:
    """画布 + 库一起打包（画布里的 file 节点要指向库里的笔记才有意义）。"""
    canvas = build_canvas(conn, pred, params, limit)
    vault = build_vault(conn, pred, params, limit)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("知识图谱.canvas", json.dumps(canvas, ensure_ascii=False))
        # 把库也塞进去（画布同级一个 notes/ 目录）
        with zipfile.ZipFile(io.BytesIO(vault)) as vz:
            for item in vz.infolist():
                z.writestr("notes/" + item.filename, vz.read(item.filename))
    return buf.getvalue()
