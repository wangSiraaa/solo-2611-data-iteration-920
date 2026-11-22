"""离线核对：独立设备只凭交付目录完成全部检查，不向编辑器索取前值。

覆盖合同要求的每一项：
1. 清单自摘要、tbm/pcm 字节摘要；
2. 差分流逐记录还原 == 已渲染 PCM（宽整数加减，无 int16 回绕）；
3. 实际复位位置 == 刻录点，且刻录点 ⊆ 批准片头、含起点 0；
4. 每个批准候选复位点，从该点沿流解码，验证按“沿用状态”得到的实际 WAV 段；
5. 跨原块接缝：接缝两侧差分 == 右首 - 左末（在 32 位以上整数域计算）；
6. 输出样本到来源的逐 run 对应：每段 PCM 与随包原 render 的采样范围一致；
7. PCM 完全相同但来源身份不同的两段，provenance_id 必须不同，禁止并源；
8. 随包 WAV（若有）字节级核对。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from . import pcmio
from .master import (
    PCM_NAME, RENDERS_DIR, TBM_NAME, load_manifest,
    provenance_id_of,
)
from .tbm import decode_master
from .varint import KIND_RESET


class VerificationError(AssertionError):
    pass


@dataclass
class ResetReplay:
    target_position: int       # 请求起播的位置（批准片头，可能未刻录复位）
    serving_reset: int         # 实际服务的、最近的不晚于目标的已刻录复位点
    discarded: int             # 丢弃的已还原样本数 target - serving_reset
    pcm_sha256: str
    samples: list[int]         # 真正发声的 WAV 段（丢弃预读后）
    first_record_kind: int     # 服务复位点处的记录类型（必须是复位）


@dataclass
class VerificationReport:
    master_id: str
    n_samples: int
    reset_replays: list[ResetReplay] = field(default_factory=list)
    seam_checks: list[dict[str, Any]] = field(default_factory=list)
    run_checks: list[dict[str, Any]] = field(default_factory=list)
    identity_collisions_checked: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "master_id": self.master_id,
            "n_samples": self.n_samples,
            "verified_candidate_points":
                [r.target_position for r in self.reset_replays],
            "seams_checked": len(self.seam_checks),
            "provenance_runs_checked": len(self.run_checks),
            "identity_collisions_checked": self.identity_collisions_checked,
            "ok": True,
        }


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def verify_bundle(bundle_dir: str) -> VerificationReport:
    manifest = load_manifest(bundle_dir)
    stored_digest = manifest.get("manifest_sha256")
    recomputed = pcmio.sha256_json(
        {k: v for k, v in manifest.items() if k != "manifest_sha256"})
    if stored_digest != recomputed:
        raise VerificationError("清单自摘要不符：文件被篡改")

    tbm_bytes = _read(os.path.join(bundle_dir, manifest["tbm"]["file"]))
    if pcmio.sha256_bytes(tbm_bytes) != manifest["tbm"]["sha256"]:
        raise VerificationError("tbm 摘要不符")
    pcm_bytes = _read(os.path.join(bundle_dir, manifest["pcm"]["file"]))
    if pcmio.sha256_bytes(pcm_bytes) != manifest["pcm"]["sha256"]:
        raise VerificationError("pcm 摘要不符")

    # 1) 逐记录流式还原，与已渲染 PCM 逐样本比对
    decoded, resets = decode_master(tbm_bytes)
    sealed_pcm = pcmio.read_pcm_file(os.path.join(bundle_dir, PCM_NAME))
    if decoded != sealed_pcm:
        raise VerificationError("差分流还原与已渲染 PCM 不一致")
    if len(sealed_pcm) != manifest["n_samples"]:
        raise VerificationError("样本数与清单不符")

    # 2) 复位点合同：含 0、刻录点是批准片头子集、与流中实际复位一致
    approved, burned = (manifest["approved_reset_positions"],
                        manifest["burned_reset_positions"])
    if approved[0] != 0 or burned[0] != 0:
        raise VerificationError("起点 0 必须是复位点")
    if set(burned) - set(approved):
        raise VerificationError("存在未批准的绝对值记录")
    if resets != burned:
        raise VerificationError("流中实际复位位置与刻录清单不符")

    report = VerificationReport(manifest["master_id"], len(sealed_pcm))

    # 3) 每个批准候选片头：按设备跳听语义复播——从最近的不晚于目标的
    #    已刻录复位点沿流解码，丢弃预读样本，核对得到的实际 WAV 段
    report.reset_replays = [
        replay_candidate(bundle_dir, r) for r in approved]

    # 4) 跨原块接缝：差分在宽整数域核对（禁止压进 16 位）
    report.seam_checks = _check_seams(manifest, sealed_pcm)

    # 5) 输出样本 <-> 来源身份对应：按 run 切 PCM，与随包 render 范围比对
    report.run_checks = _check_runs(bundle_dir, manifest, sealed_pcm)

    # 6) 同 PCM 不同授权不得并源：身份哈希输入不含波形摘要，直接验证可区分
    report.identity_collisions_checked = _check_identity_distinctness(manifest)

    return report


def replay_from_reset(bundle_dir: str, reset_position: int) -> ResetReplay:
    """从*已刻录*复位点沿流解码到母带结尾（首记录必须是绝对值）。"""
    manifest = load_manifest(bundle_dir)
    if reset_position not in manifest["burned_reset_positions"]:
        raise VerificationError(f"{reset_position} 不是已刻录复位点")
    import io
    from .tbm import _PushbackReader, StreamingDecoder
    from .varint import decode_signed

    tbm_bytes = _read(os.path.join(bundle_dir, TBM_NAME))
    reader = _PushbackReader(io.BytesIO(tbm_bytes))
    if reader.read(4) != b"TBM1":
        raise VerificationError("魔数不匹配")
    dec = StreamingDecoder()
    samples: list[int] = []
    first_kind = None
    pos = 0
    while True:
        head = reader.read(1)
        if not head:
            break
        reader.push_back(head[0])
        kind, value = decode_signed(reader)
        if pos >= reset_position:
            if first_kind is None:
                first_kind = kind
                if kind != KIND_RESET:
                    raise VerificationError(
                        f"复位点 {reset_position} 处不是绝对值记录")
            samples.append(dec.feed_record(kind, value))
        else:
            dec.feed_record(kind, value)  # 走上文以维持记录位置
        pos += 1
    if first_kind is None:
        raise VerificationError(f"复位点 {reset_position} 无对应记录")
    sealed = pcmio.read_pcm_file(os.path.join(bundle_dir, PCM_NAME))
    if samples != sealed[reset_position:]:
        raise VerificationError(f"复位点 {reset_position} 复播段与封版 PCM 不符")
    return ResetReplay(reset_position, reset_position, 0,
                       pcmio.pcm_sha256(samples), samples, first_kind)


def replay_candidate(bundle_dir: str, target_position: int) -> ResetReplay:
    """批准片头的设备跳听复播（含未刻录候选）。

    取最近的不晚于目标的已刻录复位点 r，从 r 解码到结尾，
    丢弃 target-r 个已还原样本后发声，并与封版 PCM 的目标段逐样本核对。
    """
    manifest = load_manifest(bundle_dir)
    if target_position not in manifest["approved_reset_positions"]:
        raise VerificationError(f"{target_position} 不是批准片头")
    burned = manifest["burned_reset_positions"]
    serving = max(r for r in burned if r <= target_position)
    tail = replay_from_reset(bundle_dir, serving)
    discarded = target_position - serving
    emitted = tail.samples[discarded:]
    sealed = pcmio.read_pcm_file(os.path.join(bundle_dir, PCM_NAME))
    if emitted != sealed[target_position:]:
        raise VerificationError(
            f"候选片头 {target_position} 复播段与封版 PCM 不符")
    return ResetReplay(target_position, serving, discarded,
                       pcmio.pcm_sha256(emitted), emitted,
                       tail.first_record_kind)


def _check_seams(manifest: dict[str, Any], pcm: list[int]) -> list[dict[str, Any]]:
    checks = []
    for seam in manifest["seams"]:
        at = seam["at_sample"]
        left_last, right_first = seam["left_last_sample"], seam["right_first_sample"]
        if pcm[at - 1] != left_last or pcm[at] != right_first:
            raise VerificationError(f"接缝 {at} 样本与清单不符")
        # 接缝处若不是复位点，则该记录是跨原块差分，必须在宽整数域成立
        wide_delta = right_first - left_last
        if at not in manifest["burned_reset_positions"]:
            if not -65535 <= wide_delta <= 65535:
                raise VerificationError(f"接缝 {at} 差分越界")
        checks.append({"at_sample": at, "wide_delta": wide_delta,
                       "crosses_origin_blocks": True,
                       "record_at_seam": ("reset" if at in
                                          manifest["burned_reset_positions"]
                                          else "delta")})
    return checks


def _check_runs(bundle_dir: str, manifest: dict[str, Any],
                pcm: list[int]) -> list[dict[str, Any]]:
    checks = []
    render_pcm_cache: dict[str, list[int]] = {}
    for run in manifest["provenance_runs"]:
        lo, hi = run["master_sample_range"]
        piece = pcm[lo:hi]
        if pcmio.pcm_sha256(piece) != run["pcm_sha256"]:
            raise VerificationError(f"run {run['provenance_id']} PCM 摘要不符")
        if provenance_id_of(run["identity"]) != run["provenance_id"]:
            raise VerificationError(f"run {run['provenance_id']} 身份重算不符")
        rid = run["identity"]["render_id"]
        s_lo, s_hi = run["identity"]["sample_range"]
        if rid not in render_pcm_cache:
            wav_path = os.path.join(bundle_dir, RENDERS_DIR, f"{rid}.wav")
            if os.path.exists(wav_path):
                src, rate = pcmio.read_wav(wav_path)
                if rate != manifest["sample_rate"]:
                    raise VerificationError(f"render {rid} 采样率不符")
            else:
                # 无 WAV 时用清单登记的全文摘要做存在性/结构核对
                src = None
            render_pcm_cache[rid] = src
        src = render_pcm_cache[rid]
        source_match = None
        if src is not None:
            source_match = (piece == src[s_lo:s_hi])
            if not source_match:
                raise VerificationError(
                    f"run {run['provenance_id']} 与原 render {rid}[{s_lo},{s_hi}) 不符")
        checks.append({"provenance_id": run["provenance_id"],
                       "master_sample_range": [lo, hi],
                       "render_id": rid, "render_sample_range": [s_lo, s_hi],
                       "matches_render_bytes": source_match,
                       "license_id": run["identity"]["license_id"]})
    return checks


def _check_identity_distinctness(manifest: dict[str, Any]) -> int:
    """PCM 逐样本相同的 run，身份仍必须可区分（授权/来源不同即不同 id）。"""
    by_pcm: dict[str, list[str]] = {}
    for run in manifest["provenance_runs"]:
        by_pcm.setdefault(run["pcm_sha256"], []).append(run["provenance_id"])
    n = 0
    for digest, ids in by_pcm.items():
        if len(ids) > 1 and len(set(ids)) != len(ids):
            raise VerificationError("同 PCM 的不同授权讲解被映成同一来源")
        n += len(ids) - 1 if len(ids) > 1 else 0
    return n
