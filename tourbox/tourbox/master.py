"""母带封版：把已渲染 PCM、原 render、采样范围与来源身份打成可离线核对的包。

目录布局（所有路径相对 bundle 根，全部字节/JSON 均可离线复核）：

    manifest.json      母带清单（含封版摘要、复位点、来源 run、接缝）
    master.tbm         差分母带记录流（魔数 TBM1 + 两类记录）
    master.pcm         已渲染 PCM（裸小端 int16），封版时逐样本等于 tbm 还原值
    renders/<id>.json  原 render 元数据（来源侧登记的样本范围与正文摘要）
    renders/<id>.wav   原 render 正文（可选；存在即逐字节核对）

关键身份规则：
- 来源身份 = (render_id, render 内 [lo,hi) 范围, clip_id, occurrence, 授权方)
  的规范哈希，**不含 PCM 内容摘要**。两段授权不同的讲解即使 PCM 逐样本相同，
  provenance_id 也必然不同，永远不会被并成同一条来源。
- 复位记录只能落在编带师批准的片头（approved 列表，必须含 0）；
  实际刻录点 burned 是 approved 的子集。封版后母带不可改写，
  临时请求不能新增绝对值记录。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Sequence

from . import pcmio
from .pcmio import read_json, sha256_json, write_json_atomic
from .tbm import MAGIC, decode_master, encode_master
from .varint import SAMPLE_MAX, SAMPLE_MIN

MANIFEST_NAME = "manifest.json"
TBM_NAME = "master.tbm"
PCM_NAME = "master.pcm"
RENDERS_DIR = "renders"


# ----------------------------- 来源身份 ----------------------------- #

def provenance_identity(render_id: str, lo: int, hi: int,
                        clip_id: str, occurrence: int,
                        license_id: str) -> dict[str, Any]:
    """来源身份的规范化输入。刻意不含任何 PCM/波形摘要字段。"""
    return {
        "render_id": render_id,
        "sample_range": [int(lo), int(hi)],
        "clip_id": clip_id,
        "occurrence": int(occurrence),
        "license_id": license_id,
    }


def provenance_id_of(identity: dict[str, Any]) -> str:
    return sha256_json({"kind": "tourbox.provenance/1", **identity})[:16]


@dataclass(frozen=True)
class SourceSegment:
    """编带指令：取原 render ``render_id`` 的 [lo,hi) 拼到母带当前尾部。"""
    render_id: str
    lo: int
    hi: int
    clip_id: str
    occurrence: int          # 同一音效在该 render 中被引用的第几次（从 0 起）
    license_id: str          # 授权身份；PCM 相同但授权不同 => 身份不同
    render_meta: dict[str, Any]
    render_wav: str | None = None  # 原 render WAV 路径，将随包带走并核对


@dataclass
class BuildRequest:
    master_id: str
    sample_rate: int
    segments: Sequence[SourceSegment]
    approved_reset_positions: Sequence[int]   # 编带师批准的全部候选片头（含 0）
    burned_reset_positions: Sequence[int]     # 本次实际刻录的复位点（approved 子集）
    note: str = ""


# ----------------------------- 封版 ----------------------------- #

def build_bundle(bundle_dir: str, req: BuildRequest) -> dict[str, Any]:
    if not req.segments:
        raise ValueError("至少需要一段来源 render")
    approved = sorted(set(req.approved_reset_positions))
    burned = sorted(set(req.burned_reset_positions))
    if not approved or approved[0] != 0:
        raise ValueError("起点 0 必须在批准片头内")
    if not burned or burned[0] != 0:
        raise ValueError("起点 0 必须刻录复位记录")
    if set(burned) - set(approved):
        raise ValueError("实际刻录点必须是批准片头的子集，禁止临时加绝对值")

    pcm: list[int] = []
    runs: list[dict[str, Any]] = []
    seams: list[dict[str, Any]] = []
    render_records: dict[str, dict[str, Any]] = {}

    for seg_idx, seg in enumerate(req.segments):
        if seg.lo < 0 or seg.hi <= seg.lo:
            raise ValueError(f"段 {seg_idx}: 采样范围非法 [{seg.lo},{seg.hi})")
        src_pcm = _load_render_pcm(seg)
        if seg.hi > len(src_pcm):
            raise ValueError(
                f"段 {seg_idx}: 范围 [{seg.lo},{seg.hi}) 越出 render "
                f"{seg.render_id}（共 {len(src_pcm)} 样本）")
        piece = [int(s) for s in src_pcm[seg.lo:seg.hi]]
        for s in piece:
            if not SAMPLE_MIN <= s <= SAMPLE_MAX:
                raise ValueError(f"段 {seg_idx}: 非 int16 样本 {s}")

        run_lo = len(pcm)
        pcm.extend(piece)
        run_hi = len(pcm)
        identity = provenance_identity(
            seg.render_id, seg.lo, seg.hi, seg.clip_id,
            seg.occurrence, seg.license_id)
        pid = provenance_id_of(identity)
        runs.append({
            "provenance_id": pid,
            "identity": identity,
            "master_sample_range": [run_lo, run_hi],
            "pcm_sha256": pcmio.pcm_sha256(piece),
            "n_samples": run_hi - run_lo,
        })
        if seg_idx > 0:
            # 接缝：两侧最后/最前的已还原样本，供跨原块差分核对
            seams.append({
                "at_sample": run_lo,
                "left_run": runs[-2]["provenance_id"],
                "right_run": pid,
                "left_last_sample": pcm[run_lo - 1],
                "right_first_sample": pcm[run_lo],
            })

        meta = {k: v for k, v in seg.render_meta.items()
                if not k.startswith("_")}
        meta.setdefault("render_id", seg.render_id)
        meta.setdefault("sample_count", len(src_pcm))
        meta.setdefault("sample_rate", req.sample_rate)
        meta["full_pcm_sha256"] = pcmio.pcm_sha256(src_pcm)
        render_records[seg.render_id] = {
            "meta": meta,
            "wav_bundled": seg.render_wav is not None,
        }

    n = len(pcm)
    if approved[-1] >= n:
        raise ValueError("批准片头越出母带范围")

    tbm = encode_master(pcm, burned)
    # 封版前自查：tbm 还原必须与已渲染 PCM 逐样本一致，复位位置与刻录点一致
    decoded, actual_resets = decode_master(tbm)
    if decoded != pcm:
        raise AssertionError("封版自查失败：差分流还原与已渲染 PCM 不一致")
    if actual_resets != burned:
        raise AssertionError("封版自查失败：实际复位位置与刻录点不符")

    os.makedirs(os.path.join(bundle_dir, RENDERS_DIR), exist_ok=True)
    with open(os.path.join(bundle_dir, TBM_NAME), "wb") as fh:
        fh.write(tbm)
    with open(os.path.join(bundle_dir, PCM_NAME), "wb") as fh:
        fh.write(pcmio.pcm_to_bytes(pcm))

    # 原 render 证据随包带走（WAV 存在则复制并核对摘要）
    for seg in req.segments:
        rec = render_records[seg.render_id]
        write_json_atomic(os.path.join(
            bundle_dir, RENDERS_DIR, f"{seg.render_id}.json"), rec["meta"])
        if seg.render_wav:
            wav_pcm, wav_rate = pcmio.read_wav(seg.render_wav)
            if wav_rate != req.sample_rate or wav_pcm != _load_render_pcm(seg):
                raise ValueError(f"render {seg.render_id} WAV 与登记 PCM 不符")
            pcmio.write_wav(os.path.join(
                bundle_dir, RENDERS_DIR, f"{seg.render_id}.wav"),
                wav_pcm, wav_rate)

    manifest = {
        "format": "tourbox.master/1",
        "master_id": req.master_id,
        "sample_rate": req.sample_rate,
        "n_samples": n,
        "magic": MAGIC.decode("ascii"),
        "tbm": {"file": TBM_NAME, "sha256": pcmio.sha256_bytes(tbm),
                "n_bytes": len(tbm)},
        "pcm": {"file": PCM_NAME, "sha256": pcmio.pcm_sha256(pcm)},
        "approved_reset_positions": approved,
        "burned_reset_positions": burned,
        "segments": [
            {"render_id": s.render_id, "sample_range": [s.lo, s.hi],
             "clip_id": s.clip_id, "occurrence": s.occurrence,
             "license_id": s.license_id}
            for s in req.segments],
        "provenance_runs": runs,
        "seams": seams,
        "renders": sorted(render_records),
        "note": req.note,
    }
    manifest["manifest_sha256"] = sha256_json(
        {k: v for k, v in manifest.items() if k != "manifest_sha256"})
    write_json_atomic(os.path.join(bundle_dir, MANIFEST_NAME), manifest)
    return manifest


def _load_render_pcm(seg: SourceSegment) -> list[int]:
    """来源 PCM 只从显式登记的字节读取，绝不按内容哈希反查来源。"""
    pcm = seg.render_meta.get("_pcm")
    if pcm is None:
        raise ValueError(f"render {seg.render_id} 缺少随附 PCM 正文")
    return [int(s) for s in pcm]


def load_manifest(bundle_dir: str) -> dict[str, Any]:
    return read_json(os.path.join(bundle_dir, MANIFEST_NAME))
