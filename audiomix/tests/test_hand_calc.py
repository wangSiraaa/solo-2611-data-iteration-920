"""手算案例：短整数序列素材，人工推导每个输出样本，再由程序核对。

参考解释器（engine.render_reference）与分块向量化渲染（render_block 拼接）
必须与手算结果三者一致。
"""
from fractions import Fraction as F

import numpy as np

from audiomix.assets import AssetRegistry, sequence
from audiomix.engine import (
    BLOCK_SAMPLES, render_block, render_full_vectorized, render_reference,
    validate_product, output_sample_count,
)
from audiomix.timeline import TimelineEditor


def _build(tmp_path):
    reg = AssetRegistry(tmp_path)
    # 8kHz 与 16kHz 两套短序列素材
    a8 = reg.register(sequence([0, 100, 200, 300, 400]), 8000, note="8k 短序列")
    b8 = reg.register(sequence([10, 20, 30, 40, 50, 60]), 8000, note="8k 第二素材")
    a16 = reg.register(sequence([0, 100, 200, 300, 400, 500, 600, 700]), 16000,
                       note="16k 短序列")
    a48 = reg.register(sequence([0, 1, 2, 3, 4, 5, 6]), 48000, note="48k 短序列")
    ed = TimelineEditor(tmp_path, reg, name="hand")
    return reg, ed, {"a8": a8.asset_id, "b8": b8.asset_id,
                     "a16": a16.asset_id, "a48": a48.asset_id}


def _publish(ed):
    product = ed.publish()
    return product


def _pcm(reg, product):
    return {a["asset_id"]: reg.load_pcm(a["asset_id"]) for a in product["assets"]}


def _check_both(product, pcm, T, expected, label):
    ref = render_reference(product, pcm, T)
    fast = render_full_vectorized(product, pcm, T)
    exp = np.asarray(expected, dtype=np.int16)
    assert output_sample_count(T) == len(exp), f"{label}: 样本数公式错误"
    assert np.array_equal(ref, exp), f"{label}: 参考解释器={ref.tolist()} 期望={exp.tolist()}"
    assert np.array_equal(fast, exp), f"{label}: 分块渲染={fast.tolist()} 期望={exp.tolist()}"
    # 再逐块核对分块结果与整段完全一致（块边界语义）
    nb = (len(exp) + BLOCK_SAMPLES - 1) // BLOCK_SAMPLES
    glued = np.concatenate([
        render_block(product, pcm, k, T)["samples"] for k in range(nb)])
    assert np.array_equal(glued, exp), f"{label}: 逐块拼接与整段不一致"
    print(f"  [OK] {label}: {exp.tolist()}")


def test_case_a_copy(tmp_path):
    """16k 素材原样拷贝：t=n/16000 -> 源位置 n，直接取样本。"""
    reg, ed, ids = _build(tmp_path)
    ed.insert({"clip_id": "c1", "asset_id": ids["a16"], "a": 1, "b": 5,
               "start": "0"})
    product, pcm = _publish(ed), None
    pcm = _pcm(reg, product)
    validate_product(product, pcm)
    # 时长 4/16000 -> 4 个输出样本：100,200,300,400
    _check_both(product, pcm, F(4, 16000), [100, 200, 300, 400], "A 同速原样拷贝")


def test_case_b_resample(tmp_path):
    """8k->16k 半倍重采样：源位置 n/2，线性插值 [0,50,100,150,200,250,300]。"""
    reg, ed, ids = _build(tmp_path)
    ed.insert({"clip_id": "c1", "asset_id": ids["a8"], "a": 0, "b": 4,
               "start": "0"})
    product = _publish(ed)
    pcm = _pcm(reg, product)
    # 时长 4/8000=1/2000 秒，16k 下 8 样本；最后一样本保持 x[3]=300
    _check_both(product, pcm, F(1, 2000),
                [0, 50, 100, 150, 200, 250, 300, 300], "B 跨采样率 8k->16k 插值+末样本保持")


def test_case_c_speed2(tmp_path):
    """速度 2 倍：16k 素材源位置 2n，隔点取 [100,300,500]。"""
    reg, ed, ids = _build(tmp_path)
    ed.insert({"clip_id": "c1", "asset_id": ids["a16"], "a": 1, "b": 7,
               "start": "0", "speed": "2"})
    product = _publish(ed)
    pcm = _pcm(reg, product)
    # 时长 6/(16000*2)=3/16000 -> 3 样本
    _check_both(product, pcm, F(3, 16000), [100, 300, 500], "C 2 倍速抽点")


def test_case_d_half_speed(tmp_path):
    """速度 1/2：16k 素材源位置 n/2，每点间插一个中点。"""
    reg, ed, ids = _build(tmp_path)
    ed.insert({"clip_id": "c1", "asset_id": ids["a16"], "a": 0, "b": 3,
               "start": "0", "speed": "1/2"})
    product = _publish(ed)
    pcm = _pcm(reg, product)
    # 时长 3/(16000*0.5)=6/16000 -> 6 样本，末点保持 x[2]=200
    _check_both(product, pcm, F(6, 16000),
                [0, 50, 100, 150, 200, 200], "D 半倍速插值")


def test_case_e_fades(tmp_path):
    """淡入 2 样本 + 淡出 2 样本，增益 1：手算 [0,50,200,150]。

    源 [0,100,200,300]，输出 4 样本：
      n0: x=0,   淡入 d/fi=0/2=0          -> 0*0   = 0
      n1: x=100, 淡入 1/2                 -> 100/2 = 50
      n2: x=200, d==fi 淡入因子=1；剩余 2/16000 == fo，严格小于才淡化 -> 1 -> 200
      n3: x=300, 剩余 1/16000 < fo=2/16000，淡出因子 1/2 -> 300/2 = 150
    """
    reg, ed, ids = _build(tmp_path)
    ed.insert({"clip_id": "c1", "asset_id": ids["a16"], "a": 0, "b": 4,
               "start": "0", "fade_in": F(2, 16000), "fade_out": F(2, 16000)})
    product = _publish(ed)
    pcm = _pcm(reg, product)
    _check_both(product, pcm, F(4, 16000), [0, 50, 200, 150],
                "E 淡入淡出（严格小于边界，零淡化因子恒 1）")


def test_case_f_overlap_cancel_and_clip(tmp_path):
    """重叠相消 + 削顶：两 clip 同区间，增益 2 与 -1。"""
    reg, ed, ids = _build(tmp_path)
    ed.insert([
        {"clip_id": "c1", "asset_id": ids["a16"], "a": 0, "b": 3,
         "start": "0", "gain": "2"},
        {"clip_id": "c2", "asset_id": ids["a16"], "a": 0, "b": 3,
         "start": "0", "gain": "-1"},
    ])
    product = _publish(ed)
    pcm = _pcm(reg, product)
    # 和 = (2-1)*x = x：[0,100,200]，验证多 clip 浮点先求和再单次舍入
    _check_both(product, pcm, F(3, 16000), [0, 100, 200], "F1 重叠增益代数和")

    ed2 = TimelineEditor(tmp_path, reg, name="hand_clip")
    ed2.insert({"clip_id": "loud", "asset_id": ids["a16"], "a": 1, "b": 4,
                "start": "0", "gain": "200"})
    p2 = ed2.publish()
    pcm2 = _pcm(reg, p2)
    # 200*[100,200,300]=[20000,40000,60000] -> 饱和 [20000,32767,32767]，2 个削顶
    blk = render_block(p2, pcm2, 0, F(3, 16000))
    assert blk["samples"].tolist() == [20000, 32767, 32767]
    assert blk["clipped_samples"] == 2
    assert blk["peak"] == 32767
    print("  [OK] F2 削顶饱和与削顶计数:", blk["samples"].tolist(),
          "clipped=", blk["clipped_samples"])


def test_case_g_round_half_even(tmp_path):
    """恰半取偶：0.5->0、1.5->2、2.5->2、-0.5->0、-1.5->-2。"""
    reg = AssetRegistry(tmp_path)
    # 用 16k 素材 [1,3,5,...] 配 1/2 增益制造 .5 边界
    aid = reg.register(sequence([1, 3, 5, 7, 9]), 16000, note="奇数序列").asset_id
    ed = TimelineEditor(tmp_path, reg, name="halfeven")
    ed.insert({"clip_id": "c", "asset_id": aid, "a": 0, "b": 5,
               "start": "0", "gain": "1/2"})
    product = ed.publish()
    pcm = _pcm(reg, product)
    # 0.5->0, 1.5->2, 2.5->2, 3.5->4, 4.5->4
    _check_both(product, pcm, F(5, 16000), [0, 2, 2, 4, 4], "G 恰半取偶（banker's rounding）")


def test_case_h_block_boundary(tmp_path):
    """跨块边界：clip 跨过 1600 样本块界，分块与整段必须一致。"""
    reg = AssetRegistry(tmp_path)
    aid = reg.register(sequence([0, 100, 200, 300]), 8000, note="边界素材").asset_id
    ed = TimelineEditor(tmp_path, reg, name="boundary")
    # 8k 4 样本 -> 16k 输出 8 样本；起点放在块界前 3 样本处
    ed.insert({"clip_id": "c", "asset_id": aid, "a": 0, "b": 4,
               "start": F(BLOCK_SAMPLES - 3, 16000)})
    product = ed.publish()
    pcm = _pcm(reg, product)
    T = F(BLOCK_SAMPLES + 5, 16000)
    ref = render_reference(product, pcm, T)
    fast = render_full_vectorized(product, pcm, T)
    assert np.array_equal(ref, fast)
    # 块 0 末尾 3 个非零、块 1 开头 5 个非零（插值序列 0,50,...300,300 共 8 样本，首样本为 0）
    assert np.count_nonzero(ref) == 7
    b0 = render_block(product, pcm, 0, T)
    b1 = render_block(product, pcm, 1, T)
    assert b0["clip_ids"] == ["c"] and b1["clip_ids"] == ["c"]
    print("  [OK] H 跨块边界一致，块0尾=", b0["samples"][-3:].tolist(),
          "块1头=", b1["samples"][:5].tolist())


def test_case_i_repeated_asset_and_reject(tmp_path):
    """同一素材反复引用；越界/未知素材整笔拒绝。"""
    reg, ed, ids = _build(tmp_path)
    before = len(ed.clips)
    try:
        ed.insert({"clip_id": "bad1", "asset_id": ids["a8"], "a": 0, "b": 99,
                   "start": "0"})
        raise AssertionError("越界应被拒绝")
    except Exception as e:
        assert "越界" in str(e)
    try:
        ed.insert({"clip_id": "bad2", "asset_id": "deadbeef", "a": 0, "b": 1,
                   "start": "0"})
        raise AssertionError("未知素材应被拒绝")
    except Exception as e:
        assert "未知素材" in str(e)
    assert len(ed.clips) == before, "整笔拒绝后状态必须不变"
    # 同素材三处引用 + 重叠允许
    ed.insert([
        {"clip_id": "r1", "asset_id": ids["a8"], "a": 0, "b": 2, "start": "0"},
        {"clip_id": "r2", "asset_id": ids["a8"], "a": 1, "b": 3, "start": "1/8000"},
        {"clip_id": "r3", "asset_id": ids["a8"], "a": 0, "b": 2, "start": "0",
         "gain": "1/2"},  # 与 r1 完全重叠
    ])
    product = ed.publish()
    assert len(product["clips"]) == 3
    # 时刻 0 查询：r1、r3 有效，源位置都是 0
    hits = ed.query_at(0)
    assert {h["clip_id"] for h in hits} == {"r1", "r3"}
    assert all(h["source_position"] == [0, 1] for h in hits)
    print("  [OK] I 重复引用/重叠/整笔拒绝/不渲染点查询")


def test_case_j_48k(tmp_path):
    """48k->16k：源位置 3n，取 [0,3,6]。"""
    reg, ed, ids = _build(tmp_path)
    ed.insert({"clip_id": "c", "asset_id": ids["a48"], "a": 0, "b": 7,
               "start": "0"})
    product = _publish(ed)
    pcm = _pcm(reg, product)
    # 时长 7/48000 秒，16k 样本数 ceil(16000*7/48000)=ceil(7/3)=3
    _check_both(product, pcm, F(7, 48000), [0, 3, 6], "J 48k->16k 抽点")


def run_all(base_path):
    import shutil
    shutil.rmtree(base_path, ignore_errors=True)
    print("== 手算案例（短整数序列，人工推导期望输出） ==")
    cases = [test_case_a_copy, test_case_b_resample, test_case_c_speed2,
             test_case_d_half_speed, test_case_e_fades,
             test_case_f_overlap_cancel_and_clip, test_case_g_round_half_even,
             test_case_h_block_boundary, test_case_i_repeated_asset_and_reject,
             test_case_j_48k]
    for i, fn in enumerate(cases):
        fn(f"{base_path}/case_{i:02d}_{fn.__name__}")
    print("全部手算案例通过\n")


if __name__ == "__main__":
    import sys
    run_all(sys.argv[1] if len(sys.argv) > 1 else "/tmp/audiomix_hand")
