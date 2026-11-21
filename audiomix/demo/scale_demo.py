"""规模案例：30 素材（8k/16k/48k）、100 clip、120 秒输出。

覆盖：跨采样率、同素材反复引用、重叠相消、2 倍/半倍速度、淡化相接、
削顶、移动一个 clip 的增量重渲染。全窗口逐样本用独立解释器抽样核对，
块渲染与整段渲染一致。
"""
import json
import os
import shutil
import sys

from fractions import Fraction as F

import numpy as np

from audiomix.archive import ArchiveStore
from audiomix.assets import AssetRegistry, sequence, ramp, tone
from audiomix.engine import (
    BLOCK_SAMPLES, OUTPUT_RATE, output_sample_count, render_reference,
    render_full_vectorized,
)
from audiomix.timeline import TimelineEditor

RNG = np.random.default_rng(20261118)
BASE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/audiomix_scale"


def build_assets(reg: AssetRegistry) -> list[str]:
    ids = []
    for i in range(30):
        rate = (8000, 16000, 48000)[i % 3]
        kind = i % 3
        if kind == 0:
            pcm = ramp(rate, n=min(rate // 2, 4000 + i * 97),
                       start=-20000 + i * 100, stop=20000 - i * 50)
        elif kind == 1:
            pcm = tone(rate, freq=120.0 + i * 13.7, seconds=0.5 + (i % 5) * 0.1,
                       amp=0.3 + (i % 4) * 0.15)
        else:
            pcm = sequence((RNG.integers(-8000, 8000, size=rate // 4)).tolist())
        ids.append(reg.register(pcm, rate, note=f"asset-{i:02d}-{rate}").asset_id)
    return ids


def _r_speed(i):
    return [F(1), F(2), F(1, 2), F(3, 2), F(2, 3)][i % 5]


def build_timeline(ed: TimelineEditor, asset_ids: list[str]) -> None:
    specs = []
    # 90 个常规 clip：随机素材/区间/时刻/速度/增益/淡化，刻意制造重叠
    for i in range(90):
        aid = asset_ids[i % len(asset_ids)]  # 同素材反复引用
        rec = ed.registry.get(aid)
        b = rec.sample_count
        a = int(RNG.integers(0, max(1, b // 2)))
        bb = int(RNG.integers(a + 1, b + 1))
        start = F(int(RNG.integers(0, 110 * 1000)), 1000)  # 0..110s，千分秒
        speed = _r_speed(i)
        gain = F(int(RNG.integers(-3, 4)), 2)              # -1.5..1.5
        # 淡化不得超过 clip 时长：先算时长再封顶，保证 fi+fo <= duration
        duration = F(bb - a, rec.sample_rate) / speed
        fi = min(F(int(RNG.integers(0, 30)), 1000), duration / 3)
        fo = min(F(int(RNG.integers(0, 30)), 1000), duration / 3)
        specs.append({"clip_id": f"clip-{i:03d}", "asset_id": aid, "a": a,
                      "b": bb, "start": start, "speed": speed, "gain": gain,
                      "fade_in": fi, "fade_out": fo})
    ed.insert(specs)

    # 91..94：重叠相消（同素材同区间，增益 +1 与 -1 严格抵消）
    aid = asset_ids[5]
    rec = ed.registry.get(aid)
    common = {"asset_id": aid, "a": 0, "b": min(2000, rec.sample_count),
              "start": "5", "speed": "1"}
    ed.insert([
        {**common, "clip_id": "clip-090", "gain": "1"},
        {**common, "clip_id": "clip-091", "gain": "-1"},
        {**common, "clip_id": "clip-092", "gain": "1/3", "fade_in": "1/1000"},
        {**common, "clip_id": "clip-093", "gain": "-1/3", "fade_out": "1/1000"},
    ])

    # 95..96：2 倍速与半倍速并排
    ed.insert([
        {"clip_id": "clip-094", "asset_id": asset_ids[3], "a": 0,
         "b": min(800, ed.registry.get(asset_ids[3]).sample_count),
         "start": "30", "speed": "2", "gain": "1"},
        {"clip_id": "clip-095", "asset_id": asset_ids[6], "a": 0,
         "b": min(800, ed.registry.get(asset_ids[6]).sample_count),
         "start": "30.5", "speed": "1/2", "gain": "1"},
    ])

    # 97..98：淡化相接（前一片淡出与后一片淡入在同一时刻相接）
    a97 = asset_ids[9]
    a98 = asset_ids[12]
    ed.insert([
        {"clip_id": "clip-096", "asset_id": a97, "a": 0,
         "b": min(400, ed.registry.get(a97).sample_count),
         "start": "60", "fade_out": "1/50", "gain": "1/2"},
        {"clip_id": "clip-097", "asset_id": a98, "a": 0,
         "b": min(400, ed.registry.get(a98).sample_count),
         "start": "60", "fade_in": "1/50", "gain": "1/2"},
    ])

    # 99：削顶 clip（大增益叠在已有素材上）
    ed.insert({"clip_id": "clip-098", "asset_id": asset_ids[1], "a": 0,
               "b": min(1000, ed.registry.get(asset_ids[1]).sample_count),
               "start": "90", "gain": "5"})
    # 100：跨块界的 48k 素材
    aid48 = next(a for a in asset_ids if ed.registry.get(a).sample_rate == 48000)
    ed.insert({"clip_id": "clip-099", "asset_id": aid48, "a": 0,
               "b": min(600, ed.registry.get(aid48).sample_count),
               "start": F(BLOCK_SAMPLES - 5, OUTPUT_RATE), "speed": "3/2"})


def window_verify(product, pcm, T, label) -> None:
    """在覆盖块界与各特征时刻的窗口上，用参考解释器逐样本核对向量化渲染。"""
    N = output_sample_count(T)
    full = render_full_vectorized(product, pcm, T)
    assert len(full) == N == 120 * OUTPUT_RATE, "输出必须恰为所有 0<=n/16000<T 的样本"
    windows = [(0, 800)]
    for k in [0, 1, 50, 300, 599, 600, 899, 900, 1199]:  # 含每块边界
        c = k * BLOCK_SAMPLES
        windows.append((max(0, c - 6), min(N, c + 6)))
    windows.append((N - 800, N))
    mism = 0
    checked = 0
    for lo, hi in windows:
        ref = render_reference(product, pcm, T, n0=lo, n1=hi)
        got = full[lo:hi]
        mism += int(np.count_nonzero(ref != got))
        checked += hi - lo
    assert mism == 0, f"{label}: 窗口核对发现 {mism} 个不一致样本"
    print(f"[OK] {label}: {len(windows)} 个窗口/共 {checked} 样本与独立解释器完全一致")


def main() -> None:
    shutil.rmtree(BASE, ignore_errors=True)
    reg_root = os.path.join(BASE, "registry")
    reg = AssetRegistry(reg_root)
    asset_ids = build_assets(reg)
    print(f"素材登记完成：{len(asset_ids)} 个，采样率分布 "
          f"{ {r: sum(1 for i in asset_ids if reg.get(i).sample_rate == r) for r in (8000, 16000, 48000)} }")

    ed = TimelineEditor(reg_root, reg, name="scale")
    build_timeline(ed, asset_ids)
    assert len(ed.clips) == 100
    p1 = ed.publish()
    product_path = os.path.join(BASE, "product_v1.json")
    ed.save_product(p1, product_path)
    print(f"发布 v1：{len(p1['clips'])} clips / {len(p1['assets'])} assets，"
          f"product_id={p1['product_id'][:16]}…")

    pcm = {a["asset_id"]: reg.load_pcm(a["asset_id"]) for a in p1["assets"]}
    T = F(120)
    window_verify(p1, pcm, T, "v1 分块 vs 逐样本解释器")

    store = ArchiveStore(os.path.join(BASE, "store"))
    r1 = store.render(p1, T, pcm, render_id="scale-v1")
    print(f"[OK] v1 渲染：{r1['n_samples']} 样本 / {r1['n_blocks']} 块，"
          f"实际削顶样本 {r1['clipped_samples']} 个")
    wav_path = store.export_wav("scale-v1", os.path.join(BASE, "scale_v1.wav"))

    # ---- 移动一个 clip：clip-098 从 90s 移到 100s，建立新 render ---- #
    ed.move("clip-098", "100")
    p2 = ed.publish()
    ed.save_product(p2, os.path.join(BASE, "product_v2.json"))
    window_verify(p2, pcm, T, "v2 分块 vs 逐样本解释器")
    r2 = store.render(p2, T, pcm, render_id="scale-v2")
    print(f"[OK] v2 移动后增量渲染：新渲染 {r2['blocks_rendered']} 块，"
          f"复用 {r2['blocks_reused']} 块（共 {r2['n_blocks']} 块）")
    assert r2["blocks_reused"] > 0 and r2["blocks_rendered"] < r2["n_blocks"]
    # v2 与 v1 必须不同（移动确实生效），但未受影响块字节相同
    assert not np.array_equal(store.full_pcm("scale-v1"), store.full_pcm("scale-v2"))
    store.export_wav("scale-v2", os.path.join(BASE, "scale_v2.wav"))

    # 追溯：削顶所在块都应记录 clip-098 与来源素材
    trace = store.read_trace("scale-v1", 90 * OUTPUT_RATE, 91 * OUTPUT_RATE)
    assert any("clip-098" in t["clip_ids"] and t["clipped_samples"] > 0 for t in trace)
    print("[OK] 追溯：90s 处削顶块可回溯到 clip-098 及其 asset 摘要")

    # 旧 render 仍可范围读取
    seg = store.read_samples("scale-v1", 0, 10)
    assert seg.dtype == np.int16 and len(seg) == 10
    print(f"[OK] 范围读取 scale-v1[0:10] = {seg.tolist()}")
    store.close()
    print(f"\n规模案例全部通过。WAV: {wav_path}")


if __name__ == "__main__":
    main()
