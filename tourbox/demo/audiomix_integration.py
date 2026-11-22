"""跨仓集成：直接消费 audiomix ArchiveStore 的不可变 render 封 tourbox 母带。

只通过 audiomix 的公开读取接口取已渲染 PCM 与 render 元数据，不碰编辑器日志；
随后在 tourbox 侧完成两段 render 拼带、封版与离线核对。

用法：python3 -m demo.audiomix_integration [工作目录]
"""
from __future__ import annotations

import os
import shutil
import sys
from fractions import Fraction as F

from audiomix.archive import ArchiveStore
from audiomix.assets import AssetRegistry, sequence
from audiomix.timeline import TimelineEditor

from tourbox.adapters import render_meta_from_archive, segments_from_concat
from tourbox.master import BuildRequest, build_bundle
from tourbox.verify import verify_bundle


def main(work: str) -> None:
    shutil.rmtree(work, ignore_errors=True)
    reg = AssetRegistry(os.path.join(work, "registry"))
    a1 = reg.register(sequence([100, 200, 300, 400, 500, 600, 700, 800]),
                      16000, note="讲解 A").asset_id
    a2 = reg.register(sequence([-32768, -32000, 999, 1000, 1001, 32767]),
                      16000, note="音效 B").asset_id

    ed = TimelineEditor(os.path.join(work, "editor"), reg, name="t")
    ed.insert([
        {"clip_id": "clip-narr", "asset_id": a1, "a": 0, "b": 8, "start": "0"},
        {"clip_id": "clip-sfx", "asset_id": a2, "a": 0, "b": 6,
         "start": "1/2"},
    ])
    product = ed.publish()
    ed.save_product(product, os.path.join(work, "product.json"))
    pcm = {a["asset_id"]: reg.load_pcm(a["asset_id"]) for a in product["assets"]}

    store = ArchiveStore(os.path.join(work, "archive"))
    store.render(product, F(1), pcm, render_id="render-1")
    # 同产品再渲染一条独立 render_id，模拟另一授权方的同源讲解
    store.render(product, F(1), pcm, render_id="render-2")
    n = store.render_meta("render-1")["n_samples"]
    assert n >= 14

    # 只经公开接口取 render 正文与元数据（适配器是唯一 numpy 边界）
    meta1 = render_meta_from_archive(store, "render-1")
    meta2 = render_meta_from_archive(store, "render-2")
    store.close()

    # 两段旧 render 按明确顺序拼带，分别挂不同授权，接缝不刻复位
    segs = segments_from_concat([
        {"render_id": "render-1", "lo": 0, "hi": 8, "clip_id": "clip-narr",
         "occurrence": 0, "license_id": "LIC-MUSEUM", "render_meta": meta1},
        {"render_id": "render-2", "lo": 8, "hi": 14, "clip_id": "clip-sfx",
         "occurrence": 0, "license_id": "LIC-THIRDPARTY", "render_meta": meta2},
    ])
    bundle = os.path.join(work, "tourbox-bundle")
    manifest = build_bundle(bundle, BuildRequest(
        master_id="integrated-master", sample_rate=16000, segments=segs,
        approved_reset_positions=[0, 8, 12],
        burned_reset_positions=[0]))

    report = verify_bundle(bundle)
    s = report.summary()
    assert s["ok"] and s["seams_checked"] == 1
    seam = report.seam_checks[0]
    assert seam["record_at_seam"] == "delta"  # 跨原块差分，无绝对值
    # 两份授权即使 render 正文相同也必须是两条独立来源
    ids = {r["provenance_id"] for r in manifest["provenance_runs"]}
    assert len(ids) == 2
    print({"master_id": manifest["master_id"],
           "n_samples": manifest["n_samples"],
           "seam_wide_delta": seam["wide_delta"],
           "provenance_runs": s["provenance_runs_checked"],
           "candidate_replays": s["verified_candidate_points"],
           "offline_ok": True})
    print("跨仓集成通过：audiomix render -> tourbox 差分母带 -> 离线核对")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/tourbox_audiomix_integration")
