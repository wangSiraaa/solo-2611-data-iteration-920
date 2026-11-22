"""销售计划封版、逐卡离线复播与具名 clip 锚点解析。

计划文件与母带一样不可变，并记录所依据的候选集合/预算，使独立设备
能重跑同一个优化问题来复核“为何这张卡可售/不可售”，无需联系编辑器。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Sequence

from . import pcmio
from .master import load_manifest
from .planner import (
    Card, LayoutResult, plan_layout, serving_reset,
)
from .verify import replay_from_reset

PLAN_FORMAT = "tourbox.plan/1"


def result_to_plan(master_id: str, cards: Sequence[Card],
                   result: LayoutResult) -> dict[str, Any]:
    return {
        "format": PLAN_FORMAT,
        "master_id": master_id,
        "candidate_reset_positions": list(result.candidates),
        "budget": result.budget,
        "total_value": result.total_value,
        "weighted_preread": result.weighted_preread,
        "burned_reset_positions": list(result.burned_positions),
        "cards_catalog": [c.to_dict() for c in cards],
        "sold_card_ids": result.sold_cards(),
        "unsold": result.unsold_cards(),
        "decisions": [_decision_to_dict(d) for d in result.decisions],
    }


def _decision_to_dict(d: CardDecision) -> dict[str, Any]:
    return {
        "card_id": d.card_id, "sold": d.sold, "value": d.value,
        "jumps": [{"target": j.target, "expected_plays": j.expected_plays,
                   "preread_limit": j.preread_limit,
                   "serving_reset": j.serving_reset, "preread": j.preread,
                   "feasible": j.feasible} for j in d.jumps],
    }


def save_plan(path: str, plan: dict[str, Any]) -> None:
    plan = dict(plan)
    plan["plan_sha256"] = pcmio.sha256_json(
        {k: v for k, v in plan.items() if k != "plan_sha256"})
    pcmio.write_json_atomic(path, plan)


def verify_plan(bundle_dir: str, plan_path: str) -> dict[str, Any]:
    """独立设备侧复核：重解优化问题 + 对照实际刻录母带 + 逐跳设备复播。"""
    plan = pcmio.read_json(plan_path)
    if plan.get("plan_sha256") != pcmio.sha256_json(
            {k: v for k, v in plan.items() if k != "plan_sha256"}):
        raise AssertionError("计划文件自摘要不符")
    manifest = load_manifest(bundle_dir)
    if manifest["master_id"] != plan["master_id"]:
        raise AssertionError("计划与母带身份不匹配")

    # 1) 实际刻录点必须与计划一致，且全部属于批准片头
    expect_burned = [0, *plan["burned_reset_positions"]]
    if manifest["burned_reset_positions"] != expect_burned:
        raise AssertionError("母带实际刻录复位点与计划不符")
    if set(expect_burned) - set(manifest["approved_reset_positions"]):
        raise AssertionError("存在未批准片头被写为绝对值")

    # 2) 重跑同一个优化问题，确认价值最大且同值下加权预读最小
    cards = [Card.from_dict(d) for d in plan["cards_catalog"]]
    optimal = plan_layout(cards, plan["candidate_reset_positions"],
                          plan["budget"], manifest["n_samples"])
    if optimal.total_value != plan["total_value"]:
        raise AssertionError("计划价值不是最优")
    if optimal.weighted_preread != plan["weighted_preread"]:
        raise AssertionError("等值布局下加权预读不是最小")
    if tuple(plan["burned_reset_positions"]) != optimal.burned_positions:
        raise AssertionError("选择的布局与确定性最优解不一致")

    # 3) 每张已售卡逐跳复播：只凭交付文件，从服务复位点解码并丢弃预读样本
    sealed = pcmio.read_pcm_file(os.path.join(bundle_dir, manifest["pcm"]["file"]))
    replays = []
    reset_cache: dict[int, list[int]] = {}
    for cid in plan["sold_card_ids"]:
        card = next(c for c in cards if c.card_id == cid)
        for jump in card.jumps:
            r = serving_reset(jump.target, manifest["burned_reset_positions"])
            if jump.target - r > jump.preread_limit:
                raise AssertionError(f"已售卡 {cid} 跳点 {jump.target} 实际超限")
            if r not in reset_cache:
                reset_cache[r] = replay_from_reset(bundle_dir, r).samples
            tail = reset_cache[r]
            emitted = tail[jump.target - r:]           # 丢弃 t-r 个已还原样本
            if not emitted or emitted[0] != sealed[jump.target]:
                raise AssertionError(
                    f"卡 {cid} 跳点 {jump.target} 复播首样本与母带不符")
            if emitted != sealed[jump.target:]:
                raise AssertionError(f"卡 {cid} 跳点 {jump.target} 复播段不符")
            replays.append({"card_id": cid, "target": jump.target,
                            "serving_reset": r, "discarded": jump.target - r,
                            "emitted_samples": len(emitted)})

    # 4) 未售卡必须给出具体不能承诺的跳点
    for item in plan["unsold"]:
        if not item["blocked_jumps"]:
            raise AssertionError(f"卡 {item['card_id']} 标记未售但无阻塞跳点")
    return {"plan_ok": True, "master_id": plan["master_id"],
            "sold": plan["sold_card_ids"],
            "replays": replays,
            "total_value": plan["total_value"],
            "weighted_preread": plan["weighted_preread"]}


def replay_card(bundle_dir: str, card: Card, out_wav: str | None = None
                ) -> dict[str, Any]:
    """设备侧逐卡复播：每个跳点从服务复位点起播，返回实际 WAV 段信息。"""
    manifest = load_manifest(bundle_dir)
    resets = manifest["burned_reset_positions"]
    sealed = pcmio.read_pcm_file(os.path.join(bundle_dir, manifest["pcm"]["file"]))
    jumps_out = []
    for j in card.jumps:
        r = serving_reset(j.target, resets)
        tail = replay_from_reset(bundle_dir, r).samples
        emitted = tail[j.target - r:]
        assert emitted == sealed[j.target:], "复播与封版 PCM 不符"
        jumps_out.append({"target": j.target, "serving_reset": r,
                          "discarded": j.target - r,
                          "feasible": j.target - r <= j.preread_limit,
                          "n_emitted": len(emitted)})
        if out_wav:
            pcmio.write_wav(out_wav, emitted, manifest["sample_rate"])
    return {"card_id": card.card_id, "master_id": manifest["master_id"],
            "jumps": jumps_out}


# --------------------- 节目调整：具名 clip 锚点解析 --------------------- #

def resolve_anchor(card: Card, manifest: dict[str, Any]) -> dict[str, Any]:
    """沿具名 clip 的*源位置*在当前母带版本上找新片头。

    occurrence 消歧同一音效的多次使用：只跟随该卡签下的那一次，
    绝不因为 PCM 相同而跳到别的授权讲解上。
    """
    if card.clip_id is None or card.occurrence is None or \
            card.source_sample is None:
        raise ValueError("卡缺少 clip_id/occurrence/source_sample 锚点")
    for run in manifest["provenance_runs"]:
        ident = run["identity"]
        if ident["clip_id"] != card.clip_id or \
                ident["occurrence"] != card.occurrence:
            continue
        lo, hi = ident["sample_range"]
        if lo <= card.source_sample < hi:
            mlo, mhi = run["master_sample_range"]
            new_target = mlo + (card.source_sample - lo)
            return {"card_id": card.card_id, "found": True,
                    "provenance_id": run["provenance_id"],
                    "license_id": ident["license_id"],
                    "new_target": new_target,
                    "master_sample_range": [mlo, mhi]}
    return {"card_id": card.card_id, "found": False,
            "clip_id": card.clip_id, "occurrence": card.occurrence,
            "source_sample": card.source_sample,
            "reason": "当前母带版本不含该次具名引用，不能承诺"}
