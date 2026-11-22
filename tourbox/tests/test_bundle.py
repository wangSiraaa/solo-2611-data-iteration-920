"""母带封版/核对测试：来源身份、接缝、离线复核与防篡改。"""
import json
import os
import shutil

from tourbox.master import (
    BuildRequest, SourceSegment, build_bundle, load_manifest,
    provenance_id_of, provenance_identity,
)
from tourbox.pcmio import read_pcm_file, read_wav, write_wav
from tourbox.tbm import decode_master
from tourbox.verify import VerificationError, verify_bundle


BASE = "/tmp/tourbox_bundle"


def _seg(render_id, pcm, lo, hi, clip, occ, lic, rate=16000, wav=None):
    return SourceSegment(render_id, lo, hi, clip, occ, lic,
                         {"sample_count": len(pcm), "sample_rate": rate,
                          "_pcm": list(pcm)}, wav)


def _clean():
    shutil.rmtree(BASE, ignore_errors=True)
    os.makedirs(BASE, exist_ok=True)


def test_identical_pcm_distinct_provenance():
    """两块来源不同但 PCM 完全相同：授权不同 => provenance_id 必须不同。"""
    i1 = provenance_identity("rA", 0, 4, "c1", 0, "license-museum")
    i2 = provenance_identity("rB", 2, 6, "c2", 1, "license-thirdparty")
    assert provenance_id_of(i1) != provenance_id_of(i2)
    # 仅授权不同也要区分（同一 render、同范围、同 clip）
    i3 = provenance_identity("rA", 0, 4, "c1", 0, "license-other")
    assert provenance_id_of(i1) != provenance_id_of(i3)
    # 身份输入刻意不含波形摘要：同结构对象稳定复算
    assert provenance_id_of(i1) == provenance_id_of(dict(i1))
    print("[OK] 来源身份不按 PCM 内容合并：不同授权/来源 => 不同 id")


def test_build_two_render_concat_and_verify():
    _clean()
    # 两段旧 render，接缝处制造跨原块大差分（含 ±65535 边界）
    rA = [100, 200, 32767, 300]
    rB = [-32768, -32000, 50, 60]
    wav_path = os.path.join(BASE, "rB.wav")
    write_wav(wav_path, rB, 16000)
    segs = [
        _seg("rA", rA, 0, 4, "clip-narr", 0, "LIC-A"),
        _seg("rB", rB, 0, 4, "clip-sfx", 2, "LIC-B", wav=wav_path),
    ]
    req = BuildRequest(
        master_id="m-demo", sample_rate=16000, segments=segs,
        approved_reset_positions=[0, 4, 7],
        burned_reset_positions=[0, 4])
    manifest = build_bundle(os.path.join(BASE, "bundle"), req)

    # 母带 PCM = 按明确顺序拼接
    sealed = read_pcm_file(os.path.join(BASE, "bundle", "master.pcm"))
    assert sealed == rA + rB
    # 接缝在样本 4：刻录为复位点；同时检查另一条“不复位接缝”的差分路径
    assert manifest["seams"][0]["at_sample"] == 4
    assert manifest["seams"][0]["left_last_sample"] == 300
    assert manifest["seams"][0]["right_first_sample"] == -32768

    # tbm 还原逐样本一致，实际复位点 == 刻录点
    tbm = open(os.path.join(BASE, "bundle", "master.tbm"), "rb").read()
    dec, resets = decode_master(tbm)
    assert dec == sealed and resets == [0, 4]

    report = verify_bundle(os.path.join(BASE, "bundle"))
    assert [r.target_position for r in report.reset_replays] == [0, 4, 7]
    # 未刻录的批准候选 7：从已刻录复位点 4 沿状态解码、丢弃 3 个预读样本
    rp7 = next(r for r in report.reset_replays if r.target_position == 7)
    assert rp7.serving_reset == 4 and rp7.discarded == 3
    assert rp7.samples == sealed[7:]
    # run 与随包 WAV 逐字节对应
    assert all(c["matches_render_bytes"] in (True, None)
               for c in report.run_checks)
    rb = next(c for c in report.run_checks if c["render_id"] == "rB")
    assert rb["matches_render_bytes"] is True and rb["license_id"] == "LIC-B"
    print("[OK] 两段 render 顺序拼带、封版自查、逐复位点复播、run-WAV 对应全部通过")


def test_cross_origin_delta_seam_without_reset():
    """接缝不刻复位时，跨原块差分必须在宽整数域成立（含 -65535）。"""
    _clean()
    rA = [32767]          # 左末样本
    rB = [-32768, 7]      # 右首样本：差分 -65535
    segs = [_seg("rA", rA, 0, 1, "c1", 0, "L1"),
            _seg("rB", rB, 0, 2, "c2", 0, "L2")]
    req = BuildRequest("m-seam", 16000, segs, [0], [0])
    manifest = build_bundle(os.path.join(BASE, "b2"), req)
    report = verify_bundle(os.path.join(BASE, "b2"))
    seam = report.seam_checks[0]
    assert seam["at_sample"] == 1 and seam["wide_delta"] == -65535
    assert seam["record_at_seam"] == "delta"
    sealed = read_pcm_file(os.path.join(BASE, "b2", "master.pcm"))
    assert sealed == [32767, -32768, 7]
    print("[OK] 跨原块接缝差分 -65535 宽整数核对通过（无 16 位回绕）")


def test_unapproved_reset_rejected():
    _clean()
    segs = [_seg("rA", [1, 2, 3, 4], 0, 4, "c1", 0, "L1")]
    req = BuildRequest("m-x", 16000, segs, [0], [0, 2])  # 2 未批准
    try:
        build_bundle(os.path.join(BASE, "b3"), req)
        raise AssertionError("未批准复位点必须拒绝")
    except ValueError as e:
        assert "批准片头" in str(e)
    print("[OK] 只有批准片头允许写绝对值，临时复位点被拒")


def test_tamper_rejected_offline():
    _clean()
    segs = [_seg("rA", [1, 2, 3, 4, 5], 0, 5, "c1", 0, "L1")]
    req = BuildRequest("m-t", 16000, segs, [0, 3], [0, 3])
    bdir = os.path.join(BASE, "b4")
    build_bundle(bdir, req)
    # 篡改清单正文（不动自摘要）必须被离线核对发现
    mp = os.path.join(bdir, "manifest.json")
    man = json.load(open(mp, encoding="utf-8"))
    man["note"] = "tampered"
    json.dump(man, open(mp, "w", encoding="utf-8"))
    try:
        verify_bundle(bdir)
        raise AssertionError("篡改清单应被拒")
    except VerificationError as e:
        assert "自摘要" in str(e)
    # 篡改 tbm 字节同样被拒
    _clean()
    build_bundle(bdir, req)
    tp = os.path.join(bdir, "master.tbm")
    raw = bytearray(open(tp, "rb").read())
    raw[-1] ^= 0xFF
    open(tp, "wb").write(bytes(raw))
    try:
        verify_bundle(bdir)
        raise AssertionError("篡改 tbm 应被拒")
    except (VerificationError, ValueError, EOFError):
        pass
    print("[OK] 离线核对拒绝清单/流字节篡改")


def test_pure_stdlib_decode_without_numpy():
    """模拟独立设备：子解释器禁用 numpy 后仍能只凭交付文件复播核对。"""
    import subprocess
    code = (
        "import sys; sys.modules['numpy']=None\n"
        "from tourbox.verify import verify_bundle\n"
        f"r = verify_bundle({os.path.join(BASE, 'b4')!r})\n"
        "print(r.summary()['ok'])")
    # 先重建一个完好包
    _clean()
    segs = [_seg("rA", [1, 2, 3, 4, 5], 0, 5, "c1", 0, "L1")]
    build_bundle(os.path.join(BASE, "b4"),
                 BuildRequest("m-t", 16000, segs, [0, 3], [0, 3]))
    out = subprocess.run([__import__("sys").executable, "-c", code],
                         cwd=os.path.dirname(os.path.dirname(__file__)),
                         capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip().endswith("True"), out.stderr
    print("[OK] numpy 不可用的环境下离线核对/复播照常工作")


if __name__ == "__main__":
    test_identical_pcm_distinct_provenance()
    test_build_two_render_concat_and_verify()
    test_cross_origin_delta_seam_without_reset()
    test_unapproved_reset_rejected()
    test_tamper_rejected_offline()
    test_pure_stdlib_decode_without_numpy()
    print("封版/核对测试全部通过")
