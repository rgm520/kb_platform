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


# ---------------------------------------------------------------- continue/break
# esprima 只做语法解析，**不查**"continue 不在循环里"这种语义错误：
#   nodes.forEach(() => { if (x) continue; })   ← 解析通过，运行直接 SyntaxError
#   → 整个模块不加载，页面看着正常但功能全失灵（2026-10-26 图谱模块就是这样哑掉的）。
# 所以这里拿 AST 自己走一遍：continue/break 必须处在**同一个函数内**的循环里。
LOOPS = {"ForStatement", "WhileStatement", "DoWhileStatement",
         "ForInStatement", "ForOfStatement", "LabeledStatement"}
FUNCS = {"FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression"}


def _find_illegal_jumps(node, in_loop: int, problems: list, path: str = ""):
    if isinstance(node, list):
        for i, child in enumerate(node):
            _find_illegal_jumps(child, in_loop, problems, path)
        return
    if not isinstance(node, dict):
        return
    t = node.get("type")
    if t in ("ContinueStatement", "BreakStatement"):
        if in_loop == 0:
            problems.append((path, t, node.get("line")))
        return
    if t in FUNCS:
        # 函数边界：里面的循环不算外层的
        for key, val in node.items():
            if key in ("type", "loc", "range"):
                continue
            _find_illegal_jumps(val, 0, problems, path + "/" + t)
        return
    if t in LOOPS:
        for key, val in node.items():
            if key in ("type", "loc", "range"):
                continue
            _find_illegal_jumps(val, in_loop + 1, problems, path + "/" + t)
        return
    for key, val in node.items():
        if key in ("type", "loc", "range"):
            continue
        _find_illegal_jumps(val, in_loop, problems, path)


@pytest.mark.parametrize("f", sorted(WEB.glob("*.js")), ids=lambda p: p.name)
def test_frontend_no_continue_outside_loop(f: Path):
    src = f.read_text(encoding="utf-8")
    ast = esprima.parseScript(_normalize(src), tolerant=False)
    problems: list = []
    _find_illegal_jumps(ast, 0, problems)
    assert not problems, (
        f"{f.name} 里有 continue/break 不在同函数的循环内（运行时会直接 SyntaxError，"
        f"整个模块不加载）：{problems}")
