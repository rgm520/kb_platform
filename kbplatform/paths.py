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


def split_share(unc: str, roots: list[str]) -> tuple[str, str]:
    """返回 (共享名, 共享内相对路径)。

    roots 为已知监控根（UNC 列表），取**最长匹配**；未命中时退化为
    取 UNC 的第 3 段作共享名。
    """
    u = norm_unc(unc)
    best: tuple[str, str] | None = None
    for r in roots:
        rn = norm_unc(r)
        if rn and u.lower().startswith(rn.lower() + "\\"):
            if best is None or len(rn) > len(best[0]):
                best = (rn, u[len(rn) + 1:])
    if best:
        return best[0].split("\\")[-1], best[1]
    segs = [s for s in u.split("\\") if s != ""]
    # UNC 去掉空段后：segs[0]=服务器, segs[1]=共享名, segs[2:]=共享内相对路径
    if len(segs) >= 3:
        return segs[1], "\\".join(segs[2:])
    if len(segs) == 2:
        return segs[1], ""
    return "", u


def path_key(unc: str) -> str:
    """比较用 key：小写 + 统一分隔符（Windows 语义，路径不区分大小写）。"""
    return norm_unc(unc).lower()
