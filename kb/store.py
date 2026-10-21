"""存储层：SQLite 存文件指纹、文本块、抽取结果、知识图谱节点与边、向量。

向量用 fp16 存 BLOB（全量 80 万块 ≈ 1.6GB），检索时载入 numpy 做余弦相似度。
"""
import os, json, sqlite3, struct
import numpy as np

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    fingerprint TEXT,
    mtime       REAL,
    size        INTEGER,
    ext         TEXT,
    meta        TEXT,
    status      TEXT,
    ingested_at REAL
);

CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path  TEXT NOT NULL,
    file_id    INTEGER,
    chunk_idx  INTEGER,
    text       TEXT,
    locator    TEXT,
    tokens     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_path);
CREATE INDEX IF NOT EXISTS idx_chunks_fid  ON chunks(file_id);

CREATE TABLE IF NOT EXISTS vectors (
    chunk_id INTEGER PRIMARY KEY,
    dim      INTEGER,
    vec      BLOB
);

CREATE TABLE IF NOT EXISTS knowledge (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path  TEXT,
    chunk_id   INTEGER,
    summary    TEXT,
    key_points TEXT,
    relations  TEXT,
    raw        TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_file ON knowledge(file_path);

CREATE TABLE IF NOT EXISTS nodes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    norm      TEXT NOT NULL,
    type      TEXT NOT NULL,
    props     TEXT,
    mentions  INTEGER DEFAULT 0,
    merged_to INTEGER,
    UNIQUE(norm, type)
);
CREATE INDEX IF NOT EXISTS idx_nodes_norm ON nodes(norm);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);

CREATE TABLE IF NOT EXISTS edges (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    src       INTEGER NOT NULL,
    dst       INTEGER NOT NULL,
    rel       TEXT NOT NULL,
    file_path TEXT,
    chunk_id  INTEGER,
    weight    REAL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_edges_rel ON edges(rel);
"""


def norm_name(s):
    """实体名归一：去空白、全角转半角、去括号注释、统一大小写。"""
    if not s:
        return ""
    s = s.strip()
    out = []
    for ch in s:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:      # 全角 -> 半角
            ch = chr(o - 0xFEE0)
        elif o == 0x3000:
            ch = " "
        out.append(ch)
    s = "".join(out)
    # 去掉括号内容
    for a, b in (("（", "）"), ("(", ")"), ("【", "】"), ("《", "》"), ("「", "」")):
        while a in s and b in s:
            i, j = s.index(a), s.index(b)
            if j > i:
                s = s[:i] + s[j + 1:]
            else:
                break
    return " ".join(s.split()).lower()


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.executescript(SCHEMA)
        self.db.commit()

    @classmethod
    def from_conn(cls, conn):
        """包装一个已有的连接。

        平台层（kbplatform）自己管连接与 schema，但 kb.graph 期望 Store 的
        形状（upsert_node / add_edge / commit），用这个入口避免重复 SQL。
        """
        obj = cls.__new__(cls)
        obj.db = conn
        return obj

    # ---------------- files ----------------
    def seen(self, path):
        r = self.db.execute("SELECT fingerprint, status FROM files WHERE path=?", (path,)).fetchone()
        return r

    def mark_file(self, path, fp, meta, status="ok"):
        ext = os.path.splitext(path)[1].lower()
        self.db.execute(
            """INSERT INTO files(path,fingerprint,mtime,size,ext,meta,status,ingested_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 fingerprint=excluded.fingerprint, mtime=excluded.mtime, size=excluded.size,
                 meta=excluded.meta, status=excluded.status, ingested_at=excluded.ingested_at""",
            (path, fp, os.path.getmtime(path), os.path.getsize(path), ext,
             json.dumps(meta, ensure_ascii=False), status, __import__("time").time()))

    # ---------------- chunks ----------------
    def replace_chunks(self, file_path, chunks):
        """重置某文件的全部派生数据。

        必须先清 knowledge / edges，否则重复摄取会累积：
        旧知识重复入 wiki、旧边重复计权、实体 mentions 虚高。
        """
        self.db.execute("DELETE FROM vectors WHERE chunk_id IN (SELECT id FROM chunks WHERE file_path=?)",
                        (file_path,))
        self.db.execute("DELETE FROM chunks WHERE file_path=?", (file_path,))
        self.db.execute("DELETE FROM knowledge WHERE file_path=?", (file_path,))
        self.db.execute("DELETE FROM edges WHERE file_path=?", (file_path,))
        ids = []
        for c in chunks:
            cur = self.db.execute(
                "INSERT INTO chunks(file_path,chunk_idx,text,locator,tokens) VALUES(?,?,?,?,?)",
                (file_path, c["idx"], c["text"], c.get("locator", ""), c.get("tokens", 0)))
            ids.append(cur.lastrowid)
        self.db.commit()
        return ids

    def add_vectors(self, pairs):
        self.db.executemany("INSERT OR REPLACE INTO vectors(chunk_id,dim,vec) VALUES(?,?,?)",
                            [(cid, len(v), np.asarray(v, dtype=np.float16).tobytes())
                             for cid, v in pairs])
        self.db.commit()

    # ---------------- knowledge ----------------
    def add_knowledge(self, file_path, chunk_id, obj, raw):
        self.db.execute(
            "INSERT INTO knowledge(file_path,chunk_id,summary,key_points,relations,raw,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (file_path, chunk_id, obj.get("summary", ""),
             json.dumps(obj.get("key_points", []), ensure_ascii=False),
             json.dumps(obj.get("relations", []), ensure_ascii=False),
             raw, __import__("time").time()))

    # ---------------- graph ----------------
    def upsert_node(self, name, ntype, props=None):
        n = norm_name(name)
        if not n:
            return None
        self.db.execute(
            """INSERT INTO nodes(name,norm,type,props,mentions) VALUES(?,?,?,?,1)
               ON CONFLICT(norm,type) DO UPDATE SET mentions=mentions+1""",
            (name, n, ntype, json.dumps(props or {}, ensure_ascii=False)))
        r = self.db.execute("SELECT id FROM nodes WHERE norm=? AND type=?", (n, ntype)).fetchone()
        return r[0] if r else None

    def add_edge(self, src, dst, rel, file_path=None, chunk_id=None, weight=1.0):
        if src is None or dst is None or src == dst:
            return
        self.db.execute(
            "INSERT INTO edges(src,dst,rel,file_path,chunk_id,weight) VALUES(?,?,?,?,?,?)",
            (src, dst, rel, file_path, chunk_id, weight))

    def node_by_norm(self, name, ntype):
        r = self.db.execute("SELECT id FROM nodes WHERE norm=? AND type=?",
                            (norm_name(name), ntype)).fetchone()
        return r[0] if r else None

    # ---------------- vector search ----------------
    def load_matrix(self):
        rows = self.db.execute("SELECT chunk_id, dim, vec FROM vectors").fetchall()
        if not rows:
            return np.zeros((0, 0), dtype=np.float32), []
        dim = rows[0][1]
        ids = [r[0] for r in rows]
        mat = np.zeros((len(rows), dim), dtype=np.float32)
        for i, (_, _, b) in enumerate(rows):
            mat[i] = np.frombuffer(b, dtype=np.float16).astype(np.float32)
        n = np.linalg.norm(mat, axis=1, keepdims=True)
        n[n == 0] = 1
        return mat / n, ids

    def chunk_map(self):
        return {r[0]: (r[1], r[2], r[3], r[4]) for r in
                self.db.execute("SELECT id,file_path,chunk_idx,text,locator FROM chunks")}

    def commit(self):
        self.db.commit()
