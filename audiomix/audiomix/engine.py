"""混音引擎：精确语义的逐样本参考解释器 + 等价的 NumPy 向量化块渲染。

语义约定（全部基于精确有理数，输出采样率固定 16000）：
- 输出样本 n 满足 0 <= n/16000 < T，即 n = 0 .. ceil(16000*T)-1。
- clip 在 start <= t < end 有效（左闭右开）。
- 源实数位置 p = a + (t-start)*源采样率*速度；floor 得 i、frac 得小数部分：
  * i < b-1：x[i] 与 x[i+1] 线性插值；
  * i >= b-1（即 p 已到最后一个样本及其之后、但仍在 clip 时长内）：保持 x[b-1]，
    绝不读到 [a,b) 之外。
- 淡化：增益乘 0->1 线性淡入、1->0 线性淡出；淡化时长为 0 时对应因子恒为 1。
- 所有片段的浮点贡献先求和，再做唯一一次“恰半取偶”舍入，最后饱和到 int16。
"""
from __future__ import annotations

from fractions import Fraction
from typing import Any

import numpy as np

from .rationals import R, as_r, load_r

OUTPUT_RATE = 16000
BLOCK_SAMPLES = 1600  # 每块 0.1 秒


# ============================ 产品装载与校验 ============================ #

class ProductError(ValueError):
    pass


def load_product(path: str) -> dict[str, Any]:
    import json
    with open(path, encoding="utf-8") as fh:
        product = json.load(fh)
    return product


def validate_product(product: dict[str, Any], pcm_by_asset: dict[str, np.ndarray]) -> None:
    """校验发布产品（含外部产品）。

    ``pcm_by_asset`` 为产品引用到的实际素材字节（asset_id -> int16 一维数组）。
    只消费产品与实际素材，不依赖任何编辑器私有日志。
    """
    if product.get("format") != "audiomix.timeline/1":
        raise ProductError("非法产品：format 不匹配")
    if product.get("output_rate") != OUTPUT_RATE:
        raise ProductError(f"输出采样率必须为 {OUTPUT_RATE}")

    assets = {}
    for ar in product.get("assets", []):
        aid = ar["asset_id"]
        if aid in assets:
            raise ProductError(f"资产 {aid} 重复登记")
        rate = int(ar["sample_rate"])
        if rate not in (8000, 16000, 48000):
            raise ProductError(f"资产 {aid} 采样率非法: {rate}")
        pcm = pcm_by_asset.get(aid)
        if pcm is None:
            raise ProductError(f"缺少实际素材字节: {aid}")
        import hashlib
        actual_digest = hashlib.sha256(np.asarray(pcm, dtype=np.int16).tobytes()).hexdigest()
        if actual_digest != ar["digest"]:
            raise ProductError(f"资产 {aid} 摘要与实际字节不符")
        if len(pcm) != int(ar["sample_count"]):
            raise ProductError(f"资产 {aid} 样本数与登记不符")
        assets[aid] = (rate, len(pcm))

    seen_clips = set()
    for c in product.get("clips", []):
        cid = c["clip_id"]
        if cid in seen_clips:
            raise ProductError(f"clip_id 重复: {cid}")
        seen_clips.add(cid)
        aid = c["asset_id"]
        if aid not in assets:
            raise ProductError(f"clip {cid} 引用未登记资产 {aid}")
        rate, count = assets[aid]
        a, b = int(c["a"]), int(c["b"])
        if not (0 <= a < b <= count):
            raise ProductError(f"clip {cid} 源区间 [{a},{b}) 越界")
        speed = load_r(c["speed"])
        if speed <= 0:
            raise ProductError(f"clip {cid} 速度必须为正")
        start = load_r(c["start"])
        if start < 0:
            raise ProductError(f"clip {cid} 起点为负")
        fi, fo = load_r(c["fade_in"]), load_r(c["fade_out"])
        if fi < 0 or fo < 0:
            raise ProductError(f"clip {cid} 淡化时长为负")
        duration = Fraction(b - a, rate) / speed
        if fi + fo > duration:
            raise ProductError(f"clip {cid} 淡化之和超过时长")

    from .timeline import TimelineEditor
    if TimelineEditor.canonical_product_id(product) != product.get("product_id"):
        raise ProductError("product_id 与产品正文不一致（产品可能被篡改）")


def product_clip_view(product: dict[str, Any]) -> list[dict[str, Any]]:
    """把产品里的 clip 转成带 Fraction 的内部视图（按 clip_id 排序，确定求和次序）。"""
    view = []
    rates = {a["asset_id"]: int(a["sample_rate"]) for a in product["assets"]}
    for c in sorted(product["clips"], key=lambda x: x["clip_id"]):
        rate = rates[c["asset_id"]]
        speed = load_r(c["speed"])
        start = load_r(c["start"])
        duration = Fraction(int(c["b"]) - int(c["a"]), rate) / speed
        view.append({
            "clip_id": c["clip_id"],
            "asset_id": c["asset_id"],
            "a": int(c["a"]),
            "b": int(c["b"]),
            "rate": rate,
            "start": start,
            "end": start + duration,
            "speed": speed,
            "gain": load_r(c["gain"]),
            "fade_in": load_r(c["fade_in"]),
            "fade_out": load_r(c["fade_out"]),
        })
    return view


def output_sample_count(T: Any) -> int:
    """满足 0 <= n/16000 < T 的样本数 = ceil(16000*T)。"""
    T = as_r(T)
    if T <= 0:
        raise ValueError("总时长 T 必须为正")
    q, r = divmod(T * OUTPUT_RATE, 1)
    return int(q) + (1 if r > 0 else 0)


# ====================== 参考解释器（纯 Python 逐样本） ====================== #

def _interp(x: np.ndarray, a: int, b: int, p: Fraction) -> float:
    """源位置 p（Fraction）-> 样本值（float），含末样本保持。"""
    i = p.numerator // p.denominator  # floor（p >= a >= 0，普通整除即 floor）
    if i >= b - 1:
        return float(x[b - 1])
    frac = float(p - i)
    return float(x[i]) + frac * (float(x[i + 1]) - float(x[i]))


def _fade_factor(c: dict[str, Any], t: Fraction) -> float:
    d = t - c["start"]
    fin = 1.0
    if c["fade_in"] > 0 and d < c["fade_in"]:
        fin = float(d / c["fade_in"])
    fout = 1.0
    if c["fade_out"] > 0:
        remain = c["end"] - t
        if remain < c["fade_out"]:
            fout = float(remain / c["fade_out"])
    return fin * fout


def render_reference(product: dict[str, Any], pcm_by_asset: dict[str, np.ndarray],
                     T: Any, n0: int = 0, n1: int | None = None) -> np.ndarray:
    """独立逐样本解释器：每个输出样本、每个有效 clip 全部用 Fraction 精算。

    作为语义基准，用于与分块/向量化渲染逐样本核对。可限定输出样本区间
    [n0, n1)，避免大窗口核对时反复从头计算。
    """
    validate_product(product, pcm_by_asset)
    clips = product_clip_view(product)
    N = output_sample_count(T)
    n0 = max(0, n0)
    n1 = N if n1 is None else min(N, n1)
    out = np.zeros(n1 - n0, dtype=np.float64)
    for n in range(n0, n1):
        t = Fraction(n, OUTPUT_RATE)
        total = 0.0
        for c in clips:
            if not (c["start"] <= t < c["end"]):
                continue
            p = c["a"] + (t - c["start"]) * c["rate"] * c["speed"]
            value = _interp(pcm_by_asset[c["asset_id"]], c["a"], c["b"], p)
            total += value * float(c["gain"]) * _fade_factor(c, t)
        rounded = round(total)  # Python 内置：恰半取偶
        out[n - n0] = max(-32768, min(32767, rounded))
    return out.astype(np.int16)


# ========================== 向量化块渲染（NumPy） ========================== #

def _clip_contribution(c: dict[str, Any], x: np.ndarray, n0: int, n1: int) -> tuple[np.ndarray, np.ndarray]:
    """计算 clip 在输出样本区间 [n0,n1) 上的浮点贡献，返回 (idx 局部下标, 贡献)。

    全程 int64 有理数运算确定源位置与淡化，避免浮点边界误差。
    """
    S = c["start"] * OUTPUT_RATE                 # 起点样本号（有理）
    E = c["end"] * OUTPUT_RATE                   # 终点样本号（有理）
    # ceil(S) / ceil(E)，兼容负分子（start 非负时实际不会出现）
    lo = (S.numerator + S.denominator - 1) // S.denominator if S.numerator >= 0 \
        else S.numerator // S.denominator
    hi = (E.numerator + E.denominator - 1) // E.denominator if E.numerator >= 0 \
        else E.numerator // E.denominator       # ceil(E)
    lo = max(lo, n0)
    hi = min(hi, n1)
    if lo >= hi:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64)

    nn = np.arange(lo, hi, dtype=np.int64)
    # ---- 源位置 p = a + rate*speed*(n-S)/OUT，通分成分母 den ----
    speed = c["speed"]
    rs_num = c["rate"] * speed.numerator        # rate*speed 的分子
    rs_den = speed.denominator
    # p = a + rs_num/rs_den * (n - Snum/Sden) / OUT
    Snum, Sden = S.numerator, S.denominator
    den = np.int64(OUTPUT_RATE * rs_den * Sden)
    pnum = (np.int64(c["a"]) * den
            + rs_num * (nn * Sden - Snum))
    # 溢出保护（常规规模远不可能触发）：触发时退回调用方逐样本
    for v in (pnum, den):
        if np.any(v > np.iinfo(np.int64).max // 1024) or np.any(v < np.iinfo(np.int64).min // 1024):
            raise OverflowError("int64 有理数溢出，需要参考解释器兜底")
    idx = np.floor_divide(pnum, den)            # floor(p)
    frac = (pnum - idx * den).astype(np.float64) / den.astype(np.float64)

    xi = np.empty(len(nn), dtype=np.float64)
    hold = idx >= (c["b"] - 1)
    safe = ~hold
    xi[hold] = float(x[c["b"] - 1])
    if np.any(safe):
        ii = idx[safe].astype(np.int64)
        xi[safe] = x[ii].astype(np.float64) + frac[safe] * (
            x[ii + 1].astype(np.float64) - x[ii].astype(np.float64))

    # ---- 淡化（0 时长 -> 因子恒 1）----
    d_num = nn * Sden - Snum                    # (n-S) 的分子，分母 Sden
    factor = np.ones(len(nn), dtype=np.float64)
    if c["fade_in"] > 0:
        fi = c["fade_in"]
        # d/fi = d_num / (Sden*OUT) * fi_den/fi_num
        fden = np.int64(Sden * OUTPUT_RATE * fi.numerator)
        fnum = d_num * fi.denominator
        m = d_num * fi.denominator < fden       # d < fi
        factor[m] = (fnum[m] / fden).astype(np.float64)
    if c["fade_out"] > 0:
        fo = c["fade_out"]
        Enum, Eden = E.numerator, E.denominator
        # (E-n)/(OUT*fo) = (Enum - n*Eden)*fo_den / (Eden*OUT*fo_num)
        fden = np.int64(Eden * OUTPUT_RATE * fo.numerator)
        fnum = (Enum - nn * Eden) * fo.denominator
        m = fnum < fden                          # 剩余时长 < fo
        factor[m] *= (fnum[m] / fden).astype(np.float64)

    contrib = xi * float(c["gain"]) * factor
    return nn - n0, contrib


def render_block(product: dict[str, Any], pcm_by_asset: dict[str, np.ndarray],
                 block_index: int, T: Any,
                 clips_view: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """渲染第 block_index 块（每块 BLOCK_SAMPLES 样本，末块按 T 截断）。

    返回样本、块峰值、实际削顶样本数以及依赖的 clip/asset 来源。
    ``clips_view`` 可由调用方预构造并在所有块间复用，避免重复 Fraction 计算。
    """
    clips = clips_view if clips_view is not None else product_clip_view(product)
    N = output_sample_count(T)
    n0 = block_index * BLOCK_SAMPLES
    n1 = min(n0 + BLOCK_SAMPLES, N)
    if n0 >= N:
        raise IndexError(f"块 {block_index} 超出输出长度 {N}")

    acc = np.zeros(n1 - n0, dtype=np.float64)
    used_clips: list[str] = []
    used_assets: set[str] = set()
    for c in clips:  # 排序次序与参考解释器一致，保证浮点求和次序相同
        try:
            local_idx, contrib = _clip_contribution(c, pcm_by_asset[c["asset_id"]], n0, n1)
        except OverflowError:
            return _render_block_reference(product, pcm_by_asset, block_index, T)
        if len(local_idx) == 0:
            continue
        acc[local_idx] += contrib
        used_clips.append(c["clip_id"])
        used_assets.add(c["asset_id"])

    rounded = np.rint(acc)                                   # 恰半取偶，全片只舍入一次
    clipped = int(np.count_nonzero((rounded < -32768) | (rounded > 32767)))
    saturated = np.clip(rounded, -32768, 32767).astype(np.int16)
    return {
        "block_index": block_index,
        "n0": n0,
        "n1": n1,
        "samples": saturated,
        "peak": int(np.max(np.abs(saturated))) if len(saturated) else 0,
        "clipped_samples": clipped,
        "clip_ids": used_clips,
        "asset_ids": sorted(used_assets),
    }


def _render_block_reference(product, pcm_by_asset, block_index, T) -> dict[str, Any]:
    """溢出兜底：用参考解释器渲染同一块，舍入/饱和语义不变。"""
    N = output_sample_count(T)
    n0 = block_index * BLOCK_SAMPLES
    n1 = min(n0 + BLOCK_SAMPLES, N)
    full = render_reference(product, pcm_by_asset, Fraction(n1, OUTPUT_RATE))
    samples = full[n0:n1].copy()
    return {
        "block_index": block_index, "n0": n0, "n1": n1, "samples": samples,
        "peak": int(np.max(np.abs(samples))) if len(samples) else 0,
        "clipped_samples": 0,  # 参考路径已在内部饱和，溢出兜底极难触发；统计保持保守
        "clip_ids": [c["clip_id"] for c in product_clip_view(product)
                     if c["start"] < Fraction(n1, OUTPUT_RATE)
                     and c["end"] > Fraction(n0, OUTPUT_RATE)],
        "asset_ids": sorted({c["asset_id"] for c in product["clips"]}),
    }


def render_full_vectorized(product: dict[str, Any],
                           pcm_by_asset: dict[str, np.ndarray], T: Any) -> np.ndarray:
    """整段向量化渲染（逐块拼接），供与参考解释器做全量一致性核对。"""
    N = output_sample_count(T)
    nb = (N + BLOCK_SAMPLES - 1) // BLOCK_SAMPLES
    view = product_clip_view(product)
    out = np.empty(N, dtype=np.int16)
    for k in range(nb):
        blk = render_block(product, pcm_by_asset, k, T, clips_view=view)
        out[blk["n0"]:blk["n1"]] = blk["samples"]
    return out
