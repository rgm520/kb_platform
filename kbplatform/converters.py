"""老格式转换与 OCR。

spec 决策 D9：**本期安装 PaddleOCR + LibreOffice，但不批量跑存量**；
监控范围内新进来的扫描件与老格式会被自动处理。

降级原则：任何一步不可用都**返回 None 而不是抛异常**——流水线会把文件
标记为「待支持」并继续处理下一个文件（Review Focus #5）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

LEGACY_EXT = {".doc", ".ppt", ".xls"}
SCAN_EXT = {".pdf"}

_SOFFICE_CANDIDATES = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
]

_LEGACY_TARGET = {".doc": "docx", ".ppt": "pptx", ".xls": "xlsx"}


def soffice_path() -> str | None:
    for p in _SOFFICE_CANDIDATES:
        if os.path.exists(p):
            return p
    found = shutil.which("soffice")
    return found


def ocr_available() -> bool:
    """PaddleOCR 是否可用（import 成功且模型目录已就绪）。"""
    try:
        import paddleocr  # noqa: F401
    except Exception:
        return False
    model_dir = Path(os.path.expanduser("~/.paddleocr"))
    return model_dir.exists()


def available() -> dict[str, bool]:
    return {"soffice": soffice_path() is not None, "ocr": ocr_available()}


def convert_legacy(unc_path: str, out_dir: str) -> str | None:
    """.doc/.ppt/.xls -> 现代格式。成功返回新文件路径，失败返回 None。"""
    so = soffice_path()
    if not so:
        return None
    src = Path(unc_path)
    ext = src.suffix.lower()
    target = _LEGACY_TARGET.get(ext)
    if not target:
        return None
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [so, "--headless", "--norestore", "--convert-to", target,
             "--outdir", out_dir, str(src)],
            capture_output=True, timeout=180, check=False)
    except Exception:
        return None
    out = Path(out_dir) / (src.stem + "." + target)
    if out.exists() and out.stat().st_size > 0:
        return str(out)
    return None


def _make_ocr():
    """构造 PaddleOCR 实例。兼容 3.x 与 2.x 的构造参数差异。"""
    from paddleocr import PaddleOCR
    try:  # PaddleOCR 3.x
        return PaddleOCR(use_doc_orientation_classify=False,
                          use_doc_unwarping=False,
                          use_textline_orientation=False,
                          lang="ch")
    except TypeError:  # PaddleOCR 2.x
        return PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)


def _extract_lines(result) -> list[str]:
    """从 PaddleOCR 的结果里取出文本行。兼容 3.x 与 2.x。"""
    lines: list[str] = []
    if result is None:
        return lines
    # 3.x: result.json -> {'res': {'rec_texts': [...]}}
    j = getattr(result, "json", None)
    if j is None and isinstance(result, dict):
        j = result
    if isinstance(j, dict):
        res = j.get("res", j)
        texts = res.get("rec_texts")
        if texts:
            return [str(t) for t in texts if str(t).strip()]
    # 2.x: [[box, (text, score)], ...]
    try:
        for line in result:
            if line and len(line) > 1 and line[1]:
                txt = line[1][0]
                if isinstance(txt, (list, tuple)):
                    txt = "".join(str(x) for x in txt)
                lines.append(str(txt))
    except Exception:
        pass
    return lines


def ocr_pdf(unc_path: str, out_dir: str) -> str | None:
    """扫描件 PDF -> 纯文本 .txt。成功返回文本路径，失败返回 None。"""
    if not ocr_available():
        return None
    try:
        import pymupdf
    except Exception:
        return None
    try:
        import numpy as np
        from PIL import Image
    except Exception:
        return None

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    try:
        ocr = _make_ocr()
    except Exception:
        return None

    chunks: list[str] = []
    try:
        doc = pymupdf.open(unc_path)
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            arr = np.array(img)
            try:  # 3.x
                results = ocr.predict(arr)
            except AttributeError:  # 2.x
                results = ocr.ocr(arr, cls=True)
            for r in (results or []):
                chunks.extend(_extract_lines(r))
        doc.close()
    except Exception:
        return None

    chunks = [c.strip() for c in chunks if c and c.strip()]
    if not chunks:
        return None
    out = Path(out_dir) / (Path(unc_path).stem + ".ocr.txt")
    try:
        out.write_text("\n".join(chunks), encoding="utf-8")
    except Exception:
        return None
    return str(out)
