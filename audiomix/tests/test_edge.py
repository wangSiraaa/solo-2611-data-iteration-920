"""边界与语义补充测试。"""
import os
import shutil
from fractions import Fraction as F

import numpy as np

from audiomix.archive import ArchiveStore
from audiomix.assets import AssetRegistry, sequence, lowpass, tone
from audiomix.engine import (
    OUTPUT_RATE, output_sample_count, render_full_vectorized,
    render_reference,
)
from audiomix.timeline import TimelineEditor, ValidationError


def test_sample_count_half_open():
    # T 不是样本周期整数倍：n 满足 0 <= n/16000 < T -> ceil(16000*T)
    assert output_sample_count(F(1)) == 16000
    assert output_sample_count(F(1, 16000)) == 1
    assert output_sample_count(F(3, 2 * 16000)) == 2   # T=1.5 样本周期 -> 2 个
    assert output_sample_count(F(1, 3)) == 5334         # ceil(16000/3)
    assert output_sample_count(F(1, 3)) == -(-16000 // 3)
    print("[OK] 半开区间样本数：无浮点、不多不少一个样本")


def test_end_boundary_exclusive(tmp="/tmp/audiomix_boundary"):
    shutil.rmtree(tmp, ignore_errors=True)
    reg = AssetRegistry(tmp)
    aid = reg.register(sequence([7, 8, 9]), 16000).asset_id
    ed = TimelineEditor(tmp, reg, name="b")
    ed.insert({"clip_id": "c", "asset_id": aid, "a": 0, "b": 3, "start": "0"})
    p = ed.publish()
    pcm = {aid: reg.load_pcm(aid)}
    # T 恰好等于 clip 结束时刻 3/16000：最后一个有效样本是 n=2（t=2/16000 < end）
    out = render_reference(p, pcm, F(3, 16000))
    assert out.tolist() == [7, 8, 9]
    assert len(out) == 3
    # T 多一个微小量，仍不引入 clip 外样本（n=3 处 clip 已失效 -> 0）
    out2 = render_reference(p, pcm, F(3, 16000) + F(1, 160000))
    assert out2.tolist() == [7, 8, 9, 0]
    print("[OK] clip 终点左闭右开：边界样本不四舍五入、不读区间外")


def test_modify_rollback_and_reopen(tmp="/tmp/audiomix_rollback"):
    shutil.rmtree(tmp, ignore_errors=True)
    reg = AssetRegistry(tmp)
    aid = reg.register(sequence([1, 2, 3, 4]), 16000).asset_id
    ed = TimelineEditor(tmp, reg, name="r")
    ed.insert({"clip_id": "c", "asset_id": aid, "a": 0, "b": 2, "start": "0"})
    before = ed.clips["c"]
    try:
        ed.modify("c", {"b": 99})  # 越界
        raise AssertionError("应拒绝")
    except ValidationError:
        pass
    assert ed.clips["c"] == before, "非法 modify 必须回滚"
    # 持久化后重开
    ed2 = TimelineEditor(tmp, reg, name="r")
    assert ed2.clips["c"] == before
    print("[OK] 非法修改回滚 + 时间轴持久化重开")


def test_single_rounding_after_sum(tmp="/tmp/audiomix_rounding"):
    """两个各贡献 0.5 的 clip：先求和得 1.0 -> 1；若各自先舍入会得 0。"""
    shutil.rmtree(tmp, ignore_errors=True)
    reg = AssetRegistry(tmp)
    aid = reg.register(sequence([1, 3, 5]), 16000).asset_id
    ed = TimelineEditor(tmp, reg, name="s")
    ed.insert([
        {"clip_id": "x", "asset_id": aid, "a": 0, "b": 3, "start": "0",
         "gain": "1/2"},
        {"clip_id": "y", "asset_id": aid, "a": 0, "b": 3, "start": "0",
         "gain": "1/2"},
    ])
    p = ed.publish()
    pcm = {aid: reg.load_pcm(aid)}
    # 每样本和 = x（0.5+0.5），即 [1,3,5]；证明只在求和后舍入一次
    out = render_full_vectorized(p, pcm, F(3, 16000))
    assert out.tolist() == [1, 3, 5], out.tolist()
    # 再制造恰半取偶：单 clip 半增益奇数序列
    dir2 = tmp + "_2"
    shutil.rmtree(dir2, ignore_errors=True)
    os.makedirs(dir2, exist_ok=True)
    ed2 = TimelineEditor(dir2, reg, name="s2")
    ed2.insert({"clip_id": "z", "asset_id": aid, "a": 0, "b": 3,
                "start": "0", "gain": "1/2"})
    p2 = ed2.publish()
    out2 = render_reference(p2, {aid: reg.load_pcm(aid)}, F(3, 16000))
    # 0.5->0, 1.5->2, 2.5->2（恰半取偶，偶数优先）
    assert out2.tolist() == [0, 2, 2], out2.tolist()
    print("[OK] 全部浮点贡献相加后才单次恰半取偶（区别于逐片段先舍入）")


def test_archive_half_T_blocks(tmp="/tmp/audiomix_halft"):
    shutil.rmtree(tmp, ignore_errors=True)
    reg = AssetRegistry(tmp)
    aid = reg.register(sequence(list(range(100))), 16000).asset_id
    ed = TimelineEditor(tmp, reg, name="h")
    ed.insert({"clip_id": "c", "asset_id": aid, "a": 0, "b": 100, "start": "0"})
    p = ed.publish()
    pcm = {aid: reg.load_pcm(aid)}
    store = ArchiveStore(os.path.join(tmp, "store"))
    T = F(100, 16000) + F(1, 48000)  # 末块只有部分样本
    info = store.render(p, T, pcm, render_id="h1")
    ref = render_reference(p, pcm, T)
    assert info["n_samples"] == len(ref)
    assert np.array_equal(store.full_pcm("h1"), ref)
    store.close()
    print("[OK] 非整数 T 末块截断与参考解释器一致，样本数 =", len(ref))


def test_scipy_lowpass_asset():
    """SciPy 滤波素材：高频分量被抑制，输出仍为合法 int16 素材。"""
    rate = 16000
    src = tone(rate, freq=4000.0, seconds=0.05, amp=0.8)
    filtered = lowpass(src, cutoff_hz=500.0, sample_rate=rate)
    assert filtered.dtype == np.int16 and len(filtered) == len(src)
    assert np.max(np.abs(filtered)) < np.max(np.abs(src))
    # 滤波结果本身也能作为素材登记并参与渲染
    reg = AssetRegistry("/tmp/audiomix_lp")
    rec = reg.register(filtered, rate, note="lowpass")
    assert rec.sample_count == len(filtered)
    print("[OK] SciPy 低通素材生成/登记，RMS 能量下降且类型合法")


def run():
    test_sample_count_half_open()
    test_end_boundary_exclusive()
    test_modify_rollback_and_reopen()
    test_single_rounding_after_sum()
    test_archive_half_T_blocks()
    test_scipy_lowpass_asset()
    print("边界与语义补充测试全部通过")


if __name__ == "__main__":
    run()
