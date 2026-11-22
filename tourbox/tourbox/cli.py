"""tourbox 命令行：封版 / 离线核对 / 布局计划 / 计划复核 / 逐卡复播 / 锚点解析。

输入全部为 JSON 规格文件与已封版交付目录，输出规范 JSON，便于脚本复跑。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import pcmio
from .master import BuildRequest, SourceSegment, build_bundle, load_manifest
from .plan import (
    replay_card, resolve_anchor, result_to_plan, save_plan, verify_plan,
)
from .planner import Card, plan_layout
from .verify import verify_bundle


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _load_render_pcm(spec: dict) -> tuple[list[int], int | None]:
    """构建规格里的来源正文：裸 PCM 或 WAV 文件，或内联整数数组。"""
    if "pcm_file" in spec:
        path = spec["pcm_file"]
        if path.lower().endswith(".wav"):
            return pcmio.read_wav(path)
        return pcmio.read_pcm_file(path), None
    if "wav_file" in spec:
        return pcmio.read_wav(spec["wav_file"])
    if "samples" in spec:
        return [int(s) for s in spec["samples"]], None
    raise ValueError(f"render {spec.get('render_id')} 缺少 pcm_file/wav_file/samples")


def cmd_build(p) -> None:
    spec = pcmio.read_json(p.spec)
    segments = []
    for s in spec["segments"]:
        pcm, rate = _load_render_pcm(s)
        meta = {"sample_count": len(pcm), "_pcm": pcm}
        if rate is not None:
            meta["sample_rate"] = rate
        if "render_meta" in s:
            meta.update(s["render_meta"])
        segments.append(SourceSegment(
            render_id=s["render_id"], lo=int(s["lo"]), hi=int(s["hi"]),
            clip_id=s["clip_id"], occurrence=int(s["occurrence"]),
            license_id=s["license_id"], render_meta=meta,
            render_wav=s.get("wav_file")))
    req = BuildRequest(
        master_id=spec["master_id"],
        sample_rate=int(spec.get("sample_rate", 16000)),
        segments=segments,
        approved_reset_positions=[int(x) for x in spec["approved_reset_positions"]],
        burned_reset_positions=[int(x) for x in spec["burned_reset_positions"]],
        note=spec.get("note", ""))
    manifest = build_bundle(p.out, req)
    _print({"built": True, "master_id": manifest["master_id"],
            "bundle": os.path.abspath(p.out),
            "n_samples": manifest["n_samples"],
            "burned_reset_positions": manifest["burned_reset_positions"],
            "provenance_runs": len(manifest["provenance_runs"]),
            "seams": len(manifest["seams"])})


def cmd_verify(p) -> None:
    report = verify_bundle(p.bundle)
    out = report.summary()
    out["seam_checks"] = report.seam_checks
    out["run_checks"] = report.run_checks
    _print(out)


def cmd_plan(p) -> None:
    spec = pcmio.read_json(p.cards)
    cards = [Card.from_dict(d) for d in spec["cards"]]
    manifest = load_manifest(p.bundle)
    candidates = spec.get("candidate_reset_positions",
                          [r for r in manifest["approved_reset_positions"] if r != 0])
    budget = int(spec.get("budget", p.budget))
    result = plan_layout(cards, candidates, budget, manifest["n_samples"])
    plan_obj = result_to_plan(manifest["master_id"], cards, result)
    save_plan(p.out, plan_obj)
    _print({"planned": True, "plan": os.path.abspath(p.out),
            "total_value": result.total_value,
            "weighted_preread": result.weighted_preread,
            "burned_reset_positions": [0, *result.burned_positions],
            "sold": result.sold_cards(),
            "unsold": result.unsold_cards()})


def cmd_verify_plan(p) -> None:
    _print(verify_plan(p.bundle, p.plan))


def cmd_replay_card(p) -> None:
    card = Card.from_dict(pcmio.read_json(p.card))
    _print(replay_card(p.bundle, card, p.wav))


def cmd_anchor(p) -> None:
    card = Card.from_dict(pcmio.read_json(p.card))
    _print(resolve_anchor(card, load_manifest(p.bundle)))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tourbox",
                                 description="巡展跳听盒差分母带成品仓")
    sub = ap.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("build", help="按构建规格封版一条差分母带")
    q.add_argument("--spec", required=True); q.add_argument("--out", required=True)
    q.set_defaults(fn=cmd_build)

    q = sub.add_parser("verify", help="离线核对整个交付目录")
    q.add_argument("--bundle", required=True); q.set_defaults(fn=cmd_verify)

    q = sub.add_parser("plan", help="复位点布局优化并封版销售计划")
    q.add_argument("--bundle", required=True)
    q.add_argument("--cards", required=True, help="卡目录+候选位置+预算 JSON")
    q.add_argument("--budget", type=int, default=1)
    q.add_argument("--out", required=True); q.set_defaults(fn=cmd_plan)

    q = sub.add_parser("verify-plan", help="设备侧复核计划并逐跳复播")
    q.add_argument("--bundle", required=True); q.add_argument("--plan", required=True)
    q.set_defaults(fn=cmd_verify_plan)

    q = sub.add_parser("replay-card", help="逐卡只凭交付文件复播")
    q.add_argument("--bundle", required=True); q.add_argument("--card", required=True)
    q.add_argument("--wav"); q.set_defaults(fn=cmd_replay_card)

    q = sub.add_parser("anchor", help="沿具名 clip 源位置解析新片头")
    q.add_argument("--bundle", required=True); q.add_argument("--card", required=True)
    q.set_defaults(fn=cmd_anchor)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.fn(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
