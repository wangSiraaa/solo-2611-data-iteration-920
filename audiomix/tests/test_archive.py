"""混音数据仓测试：内容寻址复用、增量重渲染、旧 render 可读、外部产品隔离。"""
import json
import os
import shutil

from fractions import Fraction as F

import numpy as np

from audiomix.archive import ArchiveStore, block_key_of
from audiomix.assets import AssetRegistry, sequence
from audiomix.engine import render_full_vectorized, render_reference, validate_product
from audiomix.timeline import TimelineEditor


def _setup(base):
    shutil.rmtree(base, ignore_errors=True)
    reg = AssetRegistry(base)
    a1 = reg.register(sequence([100, 200, 300, 400, 500, 600]), 16000, note="s1").asset_id
    a2 = reg.register(sequence([10, 20, 30, 40, 50, 60, 70, 80]), 8000, note="s2").asset_id
    ed = TimelineEditor(base, reg, name="t1")
    return base, reg, ed, a1, a2


def test_archive_lifecycle(base="/tmp/audiomix_archive"):
    base, reg, ed, a1, a2 = _setup(base)
    T = F(1, 10)  # 0.1 秒 = 1600 样本 = 1 块
    # c1 落在块 0；c2 从 0.2s 开始，落在块 2，两者天然分块
    ed.insert([
        {"clip_id": "c1", "asset_id": a1, "a": 0, "b": 6, "start": "0"},
        {"clip_id": "c2", "asset_id": a2, "a": 0, "b": 8, "start": "2/10",
         "gain": "3"},
    ])
    p1 = ed.publish()
    ed.save_product(p1, os.path.join(base, "product_v1.json"))
    store = ArchiveStore(os.path.join(base, "store"))
    pcm = {a["asset_id"]: reg.load_pcm(a["asset_id"]) for a in p1["assets"]}

    # ---- 首次渲染（3 块）：全部新渲染 ---- #
    T3 = F(3, 10)
    info1 = store.render(p1, T3, pcm, render_id="r1")
    assert info1["n_blocks"] == 3
    assert info1["blocks_rendered"] == 3 and info1["blocks_reused"] == 0
    # 与独立逐样本解释器一致
    ref = render_reference(p1, pcm, T3)
    fast = render_full_vectorized(p1, pcm, T3)
    assert np.array_equal(ref, fast)
    assert np.array_equal(store.full_pcm("r1"), ref)
    print("[OK] 首渲染与参考解释器逐样本一致")

    # ---- 同产品同 T 再渲染：整块复用 ---- #
    info1b = store.render(p1, T3, pcm, render_id="r1b")
    assert info1b["blocks_rendered"] == 0 and info1b["blocks_reused"] == 3
    print("[OK] 同产品重渲染：块全复用")

    # ---- 移动 c2：同一块（块 2）内从 0.20s 移到 0.25s ---- #
    ed.move("c2", "1/4")  # 0.25s，仍在块 2
    p2 = ed.publish()
    ed.save_product(p2, os.path.join(base, "product_v2.json"))
    info2 = store.render(p2, T3, pcm, render_id="r2")
    # 块0（c1 未动）与块1（空）依赖逐项相同 -> 复用；块2（c2 起点变）重渲染
    assert info2["blocks_reused"] == 2 and info2["blocks_rendered"] == 1, info2
    print(f"[OK] 移动 clip 增量渲染：新渲染 {info2['blocks_rendered']} 块，"
          f"复用 {info2['blocks_reused']} 块")

    # 块0 的 key 在两个产品下必须相同（依赖完全一致：c1 身份+参数+素材摘要）
    k1 = block_key_of(p1, 0, ["c1"], [a1])
    k2 = block_key_of(p2, 0, ["c1"], [a1])
    assert k1 == k2
    # 块2 依赖含 c2 且 start 改变，key 必须不同
    k_old = block_key_of(p1, 2, ["c2"], [a2])
    k_new = block_key_of(p2, 2, ["c2"], [a2])
    assert k_old != k_new
    print("[OK] 复用依据含全部重叠 clip：未动块 key 相同，移动块 key 改变")

    # ---- 参考解释器核对 r2 全量 ---- #
    ref2 = render_reference(p2, pcm, T3)
    assert np.array_equal(store.full_pcm("r2"), ref2)
    # 范围读取与追溯
    seg = store.read_samples("r2", 1598, 1605)
    assert np.array_equal(seg, ref2[1598:1605])
    trace = store.read_trace("r2", 0, 4800)
    deps = {t["block_index"]: set(t["clip_ids"]) for t in trace}
    assert deps[0] == {"c1"} and deps[1] == set() and deps[2] == {"c2"}
    print("[OK] 范围读取/块追溯：块0={c1} 块1={} 块2={c2}")

    # ---- 新产品不能误用旧块：改素材摘要（换正文）---- #
    reg2 = AssetRegistry(base)
    a1b = reg2.register(sequence([111, 222, 333, 444, 555, 666]), 16000,
                        note="s1-改").asset_id
    ed3 = TimelineEditor(base + "_alt", reg2, name="t1")
    shutil.rmtree(base + "_alt", ignore_errors=True)
    os.makedirs(base + "_alt", exist_ok=True)
    ed3 = TimelineEditor(base + "_alt", reg2, name="t1")
    ed3.insert({"clip_id": "c1", "asset_id": a1b, "a": 0, "b": 6, "start": "0"})
    p3 = ed3.publish()
    pcm3 = {a1b: reg2.load_pcm(a1b)}
    # clip_id/参数全同，但素材摘要不同 -> 块 key 必须不同 -> 不会复用
    k3 = block_key_of(p3, 0, ["c1"], [a1b])
    assert k3 != block_key_of(p1, 0, ["c1"], [a1])
    info3 = store.render(p3, T, pcm3, render_id="r3")
    assert info3["blocks_reused"] == 0 and info3["blocks_rendered"] == 1
    assert np.array_equal(store.full_pcm("r3"), render_reference(p3, pcm3, T))
    print("[OK] 素材摘要变化 -> key 变化 -> 新产品零误用旧块")

    # ---- 外部合法产品：篡改 digest 必须被拒 ---- #
    bad = json.loads(json.dumps(p1))
    bad["assets"][0]["digest"] = "0" * 64
    try:
        validate_product(bad, pcm)
        raise AssertionError("篡改摘要应拒绝")
    except Exception as e:
        assert "摘要" in str(e)
    bad2 = json.loads(json.dumps(p1))
    bad2["clips"][0]["gain"] = ["999", "1"]  # 改参数但不重算 product_id
    try:
        validate_product(bad2, pcm)
        raise AssertionError("篡改产品正文应拒绝")
    except Exception as e:
        assert "product_id" in str(e)
    print("[OK] 外部产品校验：摘要/正文篡改均拒绝")

    # ---- 编辑器移走后，旧 render 继续可读（只靠产品+块仓）---- #
    store.close()
    for fname in os.listdir(base):
        if fname.startswith("timeline_"):
            os.remove(os.path.join(base, fname))
    store2 = ArchiveStore(os.path.join(base, "store"))
    assert np.array_equal(store2.full_pcm("r1"), ref)
    meta = store2.render_meta("r1")
    assert meta["n_samples"] == 4800
    store2.verify_block_pcm_integrity()
    print("[OK] 编辑器私有日志移走后，r1/r2/r3 仍可读，块完整性校验通过")
    store2.close()


if __name__ == "__main__":
    test_archive_lifecycle()
    print("数据仓测试全部通过")
