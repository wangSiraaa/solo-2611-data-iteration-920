"""有理数秒/参数表示与序列化。

时间轴上的所有时刻、速度、增益、淡化时长都用 ``fractions.Fraction``
精确表示，避免浮点四舍五入在区间边界上“多一个样本/少一个样本”。
对外 JSON 用 ``[分子, 分母]``（分母恒正，整数简写为 ``[n, 1]``）。
"""
from __future__ import annotations

from fractions import Fraction
from typing import Any, Union

__all__ = ["R", "as_r", "dump_r", "load_r", "floor_div", "ceil_div"]

R = Fraction


def as_r(value: Union[int, str, float, Fraction, tuple[int, int], list[int]]) -> Fraction:
    """把 int/str(如 '1/3'、'0.25')/float/Fraction/[n,d] 统一转成 Fraction。"""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value, 1)
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            raise ValueError(f"有理数需为 [分子, 分母]，收到 {value!r}")
        return Fraction(int(value[0]), int(value[1]))
    if isinstance(value, str):
        s = value.strip()
        if "/" in s:
            n, d = s.split("/", 1)
            return Fraction(int(n), int(d))
        return Fraction(s)  # 支持 '0.25'、'3' 等十进制串
    if isinstance(value, float):
        return Fraction(value)
    raise TypeError(f"无法解释为有理数: {value!r}")


def dump_r(f: Fraction) -> list[int]:
    f = Fraction(f)
    return [f.numerator, f.denominator]


def load_r(obj: Any) -> Fraction:
    return as_r(obj)


def floor_div(a: Fraction, b: Fraction) -> int:
    """floor(a / b)，b > 0，精确整数运算。"""
    q, r = divmod(a, b)
    return int(q)


def ceil_div(a: Fraction, b: Fraction) -> int:
    """ceil(a / b)，b > 0，精确整数运算。"""
    q, r = divmod(a, b)
    return int(q) + (1 if r > 0 else 0)
