"""NAS 文档解析层：把各类办公文档转成纯文本块。

设计要点：
- 统一 UTF-8 内部表示，源文件编码由 chardet 兜底
- 每条记录保留 (path, page/para, text)，便于回溯引用
- PDF 区分「文本型」与「扫描件」；扫描件标记 need_ocr，交由 vision/OCR 管线
"""
import os, io, sys, json, hashlib, zipfile, warnings
from dataclasses import dataclass, field, asdict

warnings.filterwarnings("ignore")

TEXT_EXT = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".sql", ".ini", ".log", ".js", ".java", ".py", ".vue", ".dwg"}
DOCX_EXT = {".docx"}
XLSX_EXT = {".xlsx", ".xlsm"}
PPTX_EXT = {".pptx"}
PDF_EXT = {".pdf"}
LEGACY_EXT = {".doc", ".ppt", ".xls"}


@dataclass
class Block:
    """一个文本块，携带定位信息。"""
    path: str
    kind: str          # paragraph / table / slide / page
    text: str
    idx: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def locator(self):
        return self.meta.get("loc", "")


def file_fingerprint(path, size=None, mtime=None):
    st = os.stat(path)
    h = hashlib.sha1()
    h.update(path.encode("utf-8", "replace"))
    h.update(str(st.st_size).encode())
    h.update(str(int(st.st_mtime)).encode())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------- PDF
def parse_pdf(path):
    import pymupdf
    doc = pymupdf.open(path)
    blocks, npages, nimg = [], doc.page_count, 0
    for pno, page in enumerate(doc):
        blocks.append(Block(path, "page", page.get_text().strip(), pno, {"loc": f"p{pno+1}"}))
        nimg += len(page.get_images(full=True))
    total = sum(len(b.text) for b in blocks)
    cpg = total / max(npages, 1)
    doc.close()
    if cpg < 50:
        return blocks, {"pages": npages, "chars": total, "cpg": cpg, "images": nimg,
                        "type": "scanned", "need_ocr": True}
    return blocks, {"pages": npages, "chars": total, "cpg": cpg, "images": nimg,
                    "type": "text", "need_ocr": False}


# ---------------------------------------------------------------- DOCX
def parse_docx(path):
    import docx
    d = docx.Document(path)
    blocks = []
    for i, p in enumerate(d.paragraphs):
        t = p.text.strip()
        if t:
            blocks.append(Block(path, "paragraph", t, i, {"loc": f"p{i+1}", "style": p.style.name}))
    for ti, tb in enumerate(d.tables):
        rows = []
        for row in tb.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            blocks.append(Block(path, "table", "\n".join(rows), ti, {"loc": f"table{ti+1}"}))
    nimg = 0
    try:
        with zipfile.ZipFile(path) as z:
            nimg = len([n for n in z.namelist() if n.startswith("word/media/")])
    except Exception:
        pass
    chars = sum(len(b.text) for b in blocks)
    return blocks, {"chars": chars, "paragraphs": len(d.paragraphs), "tables": len(d.tables),
                    "images": nimg, "type": "text", "need_ocr": chars < 50 and nimg > 0}


# ---------------------------------------------------------------- XLSX
def parse_xlsx(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    blocks = []
    for ws in wb.worksheets:
        rows = []
        for r, row in enumerate(ws.iter_rows(values_only=True)):
            cells = ["" if v is None else str(v).strip() for v in row]
            if any(cells):
                rows.append(" | ".join(cells))
            if r > 5000:
                break
        if rows:
            blocks.append(Block(path, "table", "\n".join(rows), len(blocks),
                                {"loc": f"sheet:{ws.title}", "sheet": ws.title, "rows": len(rows)}))
    wb.close()
    chars = sum(len(b.text) for b in blocks)
    return blocks, {"chars": chars, "type": "text", "need_ocr": False}


# ---------------------------------------------------------------- PPTX
def parse_pptx(path):
    from pptx import Presentation
    pr = Presentation(path)
    blocks = []
    for si, slide in enumerate(pr.slides):
        parts = []
        for sh in slide.shapes:
            if sh.has_text_frame and sh.text_frame.text.strip():
                parts.append(sh.text_frame.text.strip())
        if parts:
            blocks.append(Block(path, "slide", "\n".join(parts), si, {"loc": f"slide{si+1}"}))
    chars = sum(len(b.text) for b in blocks)
    return blocks, {"chars": chars, "slides": len(pr.slides), "type": "text", "need_ocr": False}


# ---------------------------------------------------------------- 纯文本
def parse_text(path):
    raw = b""
    try:
        raw = open(path, "rb").read()
    except Exception:
        pass
    txt = None
    for enc in ("utf-8", "utf-8-sig", "gb18030", "big5", "latin-1"):
        try:
            txt = raw.decode(enc)
            break
        except Exception:
            continue
    if txt is None:
        txt = raw.decode("utf-8", "replace")
    txt = txt.replace("\r\n", "\n")
    paras = [x.strip() for x in txt.split("\n") if x.strip()]
    blocks = [Block(path, "paragraph", p, i, {"loc": f"L{i+1}"}) for i, p in enumerate(paras)]
    return blocks, {"chars": len(txt), "type": "text", "need_ocr": False}


# ---------------------------------------------------------------- OFD
def parse_ofd(path):
    """OFD 是国标版式文档，本质是 ZIP + XML。粗粒度抽取文本节点。"""
    blocks = []
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.endswith(".xml")]
            import re
            for n in names:
                try:
                    x = z.read(n).decode("utf-8", "replace")
                except Exception:
                    continue
                texts = re.findall(r"<TextCode[^>]*>(.*?)</TextCode>", x, re.S)
                if texts:
                    blocks.append(Block(path, "paragraph", "\n".join(t.strip() for t in texts),
                                        len(blocks), {"loc": n}))
    except Exception as e:
        return [], {"chars": 0, "type": "error", "error": str(e)[:100], "need_ocr": True}
    chars = sum(len(b.text) for b in blocks)
    return blocks, {"chars": chars, "type": "text", "need_ocr": chars < 50}


PARSERS = {}
for _e in (PDF_EXT | DOCX_EXT | XLSX_EXT | PPTX_EXT | TEXT_EXT):
    PARSERS[_e] = None
PARSERS[".pdf"] = parse_pdf
PARSERS[".docx"] = parse_docx
PARSERS[".xlsx"] = parse_xlsx
PARSERS[".xlsm"] = parse_xlsx
PARSERS[".pptx"] = parse_pptx
PARSERS[".ofd"] = parse_ofd
PARSERS[".txt"] = parse_text
PARSERS[".md"] = parse_text


def parse_file(path):
    ext = os.path.splitext(path)[1].lower()
    fn = PARSERS.get(ext)
    if fn is None:
        if ext in LEGACY_EXT:
            return [], {"type": "unsupported_legacy", "need_ocr": False}
        return [], {"type": "unsupported", "need_ocr": False}
    try:
        return fn(path)
    except Exception as e:
        return [], {"type": "error", "error": f"{type(e).__name__}: {e}"[:150], "need_ocr": False}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for p in sys.argv[1:]:
        b, m = parse_file(p)
        print(f"\n=== {p}")
        print("  meta:", json.dumps(m, ensure_ascii=False))
        print(f"  blocks={len(b)} 总字符={sum(len(x.text) for x in b)}")
        for x in b[:5]:
            print(f"  [{x.kind}/{x.locator}] {x.text[:100]!r}")
