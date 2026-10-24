"""前端 JS 语法自检（真解析，不是括号配平这种猜）。

背景（2026-10-24）：app.js 里一个箭头函数少写了右括号 → 整个脚本解析失败 →
页面全白、登录都不响应，而服务端测试全绿、看不出任何问题。
这个测试在**提交前**就把前端语法错误拦下来。

用 esprima（纯 Python 的 JS 解析器）真解析一遍；没装 esprima 就跳过。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "web" / "static"

esprima = pytest.importorskip("esprima", reason="需要 esprima 做前端语法检查")


def _normalize(src: str) -> str:
    """esprima 4.0 不支持 `?.` / `??`，解析前换成等价的旧写法（只为过语法检查）。"""
    src = re.sub(r"\?\.", ".", src)          # a?.b  ->  a.b
    src = re.sub(r"\?\?", "||", src)          # a ?? b ->  a || b
    return src


@pytest.mark.parametrize("f", sorted(WEB.glob("*.js")), ids=lambda p: p.name)
def test_frontend_js_parses(f: Path):
    src = f.read_text(encoding="utf-8")
    try:
        esprima.parseScript(_normalize(src), tolerant=False)
    except Exception as e:  # esprima 抛的是自己的异常类型
        line = getattr(e, "lineNumber", None)
        col = getattr(e, "column", None)
        raise AssertionError(
            f"{f.name} 语法错误（行 {line} 列 {col}）：{e}") from e
