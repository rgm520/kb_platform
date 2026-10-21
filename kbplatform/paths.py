"""UNC 路径规范化。

约束（来自 spec Global Constraints #1 #2）：
  1. 路径一律以 UNC 绝对路径为准，盘符形式只允许作为 to_unc() 的入参
  2. 规范化**不做 Unicode NFC/NFD 归一**——Windows 与 DSM 都是 NFC，
     归一会破坏原路径匹配
"""
from __future__ import annotations


def norm_unc(path: str) -> str:
    """统一分隔符、去掉重复分隔符、每段去首尾空格。不做 Unicode 归一。

    UNC 的两个前导分隔符必须保留，否则 ``\\\\server\\share`` 会退化成
    ``server\\share``，后续 split_share 取到的共享名会错位。
    """
    if not path:
        return ""
    p = path.replace("/", "\\")
    is_unc = p.startswith("\\\\")
    parts = [seg.strip() for seg in p.split("\\") if seg.strip() != ""]
    joined = "\\".join(parts)
    return ("\\\\" + joined) if is_unc else joined


def to_unc(path: str, drive_map: dict[str, str]) -> str:
    """盘符路径 -> UNC；已是 UNC 则原样规范化。

    drive_map 形如 {"W:": "\\\\192.0.2.19\\信息化管理"}，键忽略大小写。
    """
    if not path:
        return ""
    p = path.replace("/", "\\")
    if not p.startswith("\\\\"):
        for drive, unc in drive_map.items():
            d = drive.rstrip("\\")
            if p.upper().startswith(d.upper() + "\\"):
                p = unc.rstrip("\\") + p[len(d):]
                break
            if p.upper() == d.upper():
                p = unc
                break
    return norm_unc(p)


def split_share(unc: str, roots: list[str] | None = None) -> tuple[str, str]:
    """返回 (共享名, 共享内相对路径)。

    共享名取 UNC 的第 2 段（``\\\\server\\share\\...``），
    相对路径是共享名之后的全部内容。**与监控根无关**——
    监控根可能指向共享内的子目录（如 ``\\信息化管理\\AI知识库``），
    但共享名始终是 ``信息化管理``，相对路径始终是 ``AI知识库\\x.pdf``。
    这正是权限模型 (share, subpath) 需要的形式。

    roots 保留为可选参数仅为兼容旧调用；判断是否在监控范围内请用 is_under_root。
    """
    u = norm_unc(unc)
    segs = [s for s in u.split("\\") if s != ""]
    # UNC: segs[0]=服务器, segs[1]=共享名, segs[2:]=共享内相对路径
    if len(segs) >= 3:
        return segs[1], "\\".join(segs[2:])
    if len(segs) == 2:
        return segs[1], ""
    return "", u


def is_under_root(unc: str, roots: list[str]) -> bool:
    """路径是否位于任一监控根之下（按目录边界匹配）。"""
    u = norm_unc(unc).lower()
    for r in roots or []:
        rn = norm_unc(r).lower()
        if rn and (u == rn or u.startswith(rn + "\\")):
            return True
    return False


def path_key(unc: str) -> str:
    """比较用 key：小写 + 统一分隔符（Windows 语义，路径不区分大小写）。"""
    return norm_unc(unc).lower()
