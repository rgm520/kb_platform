"""扫描范围：浏览整个共享，但只解析配置里监控的那部分。

背景（2026-10-25 事故）：自动接入存储源时用的是**共享根**，2.0 扫描器于是把
整个共享（信息化管理，约 12 万个可解析文件）都建了 catalog 行并排进队列，
把解析队列和算力吃满，还让问答引用到授权范围之外的内容。

现在：浏览范围 = 整个存储源；索引/解析范围 = config 的 [monitor].roots。
"""
from __future__ import annotations

import pytest

from kbplatform.config import Config
from kbplatform.scanner import index_scope_for
from kbplatform.storage import Source


def _cfg(roots):
    return Config(monitor_roots=list(roots))


def test_scope_is_the_monitored_subtree():
    src = Source(id=1, name="信息化管理", kind="smb", root=r"\\192.0.2.19\信息化管理")
    want, scope = index_scope_for(_cfg([r"\\192.0.2.19\信息化管理\AI知识库"]), src)
    assert want is True
    assert scope == "AI知识库", "只该解析监控的那个子目录"


def test_source_outside_monitor_roots_is_not_indexed():
    src = Source(id=2, name="综合系统", kind="smb", root=r"\\192.0.2.19\综合系统")
    want, scope = index_scope_for(_cfg([r"\\192.0.2.19\信息化管理\AI知识库"]), src)
    assert want is False, "不在监控范围的共享只浏览、不解析"
    assert scope == ""


def test_whole_source_as_monitor_root():
    src = Source(id=1, name="信息化管理", kind="smb", root=r"\\192.0.2.19\信息化管理")
    want, scope = index_scope_for(_cfg([r"\\192.0.2.19\信息化管理"]), src)
    assert want is True and scope == ""


def test_no_monitor_roots_means_index_everything():
    """没配监控根（纯 2.0 部署）→ 整个源都索引。"""
    src = Source(id=1, name="本地库", kind="local", root=r"D:\data")
    want, scope = index_scope_for(_cfg([]), src)
    assert want is True and scope == ""


def test_nested_monitor_root_uses_shortest_relative_path():
    src = Source(id=1, name="共享", kind="smb", root=r"\\nas\共享")
    want, scope = index_scope_for(
        _cfg([r"\\nas\共享\A\B", r"\\nas\共享\C"]), src)
    assert want is True
    assert scope in ("A\\B", "C")
    assert scope == "C", "取相对根最短的那个（覆盖范围最广）"
