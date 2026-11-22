"""导览跳听卡与复位点布局优化（纯标准库，精确求解）。

合同：
- 起点 0 已含一个复位点（不占位置）；另有 K 个候选位置，最多可刻录 B 个。
- 每张跳听卡声明若干起播点 t（预计点播次数 w）与各自允许预读上限 L；
  对每个 t，从最近的不晚于 t 的复位点 r 起播，丢弃 t-r 个已还原样本，
  故该跳点可兑现当且仅当 t-r <= L。卡内*所有*跳点都可兑现才能销售整张卡。
- 连听（从上一节连续走现有状态）不产生新复位点、不占位置。
- 目标一：同一条母带上可兑现卡的总价值最大；
  目标二（等值布局之间）：已售卡各次跳听的加权预读总和
      sum_card sum_jump w(t) * (t - r(t))  最小。
- 本模块只在已封版母带的批准候选上做选择，绝不改刻母带；
  需要新复位点时由编带师出*新版本*母带，旧售出包继续可复核。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass(frozen=True)
class Jump:
    target: int           # 起播样本位置 t
    expected_plays: int   # 预计点播次数 w
    preread_limit: int    # 允许预读上限 L（t-r <= L 才可销售）


@dataclass(frozen=True)
class Card:
    card_id: str
    jumps: tuple[Jump, ...]
    value: int
    # 可选：沿具名 clip 的源位置锚定（节目调整后重找片头用）。
    # occurrence 消歧同一音效的反复使用——只跟随签下的那一次。
    clip_id: str | None = None
    occurrence: int | None = None
    source_sample: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Card":
        return cls(
            card_id=d["card_id"],
            jumps=tuple(Jump(int(j["target"]), int(j["expected_plays"]),
                             int(j["preread_limit"])) for j in d["jumps"]),
            value=int(d["value"]),
            clip_id=d.get("clip_id"),
            occurrence=d.get("occurrence"),
            source_sample=d.get("source_sample"),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "card_id": self.card_id,
            "jumps": [{"target": j.target, "expected_plays": j.expected_plays,
                       "preread_limit": j.preread_limit} for j in self.jumps],
            "value": self.value,
        }
        if self.clip_id is not None:
            d.update(clip_id=self.clip_id, occurrence=self.occurrence,
                     source_sample=self.source_sample)
        return d


@dataclass
class JumpDecision:
    target: int
    expected_plays: int
    preread_limit: int
    serving_reset: int
    preread: int
    feasible: bool


@dataclass
class CardDecision:
    card_id: str
    sold: bool
    value: int
    jumps: list[JumpDecision] = field(default_factory=list)


@dataclass
class LayoutResult:
    burned_positions: tuple[int, ...]      # 实际选择的额外复位点（不含起点 0）
    total_value: int
    weighted_preread: int
    decisions: list[CardDecision]
    candidates: tuple[int, ...]
    budget: int

    def sold_cards(self) -> list[str]:
        return [d.card_id for d in self.decisions if d.sold]

    def unsold_cards(self) -> list[dict[str, Any]]:
        out = []
        for d in self.decisions:
            if not d.sold:
                out.append({
                    "card_id": d.card_id,
                    "blocked_jumps": [
                        {"target": j.target, "preread_limit": j.preread_limit,
                         "best_available_reset": j.serving_reset,
                         "actual_preread": j.preread}
                        for j in d.jumps if not j.feasible],
                })
        return out


def serving_reset(target: int, resets: Sequence[int]) -> int:
    """最近的不晚于 target 的复位点（resets 含 0，已排序）。"""
    best = resets[0]
    for r in resets:
        if r <= target:
            best = r
        else:
            break
    return best


def evaluate_layout(cards: Sequence[Card], extra_resets: Sequence[int],
                    n_samples: int | None = None
                    ) -> tuple[list[CardDecision], int, int]:
    """评估一组额外复位点：逐卡判可售，并计算加权预读总数。"""
    resets = sorted([0, *extra_resets])
    decisions: list[CardDecision] = []
    total_value = 0
    weighted_preread = 0
    for card in cards:
        jds: list[JumpDecision] = []
        feasible_all = True
        for j in card.jumps:
            if n_samples is not None and j.target >= n_samples:
                r, preread = 0, j.target  # 越出母带，永远不可兑现
                feasible = False
            else:
                r = serving_reset(j.target, resets)
                preread = j.target - r
                feasible = preread <= j.preread_limit
            feasible_all &= feasible
            jds.append(JumpDecision(j.target, j.expected_plays,
                                    j.preread_limit, r, preread, feasible))
        if feasible_all:
            total_value += card.value
            weighted_preread += sum(
                jd.expected_plays * jd.preread for jd in jds)
        decisions.append(CardDecision(card.card_id, feasible_all,
                                      card.value, jds))
    return decisions, total_value, weighted_preread


def plan_layout(cards: Sequence[Card], candidates: Sequence[int],
                budget: int, n_samples: int | None = None) -> LayoutResult:
    """精确分支限界：枚举候选复位点子集（|S|<=B），价值最大、加权预读最小。

    剪枝：剩余候选即使全选也不可能超过当前最优价值时立即回溯。
    确定性：价值与预读都持平时，取字典序最小的布局，保证可复跑。
    """
    cands = sorted(set(candidates))
    if any(c <= 0 for c in cands):
        raise ValueError("候选位置必须为正整数（起点 0 已内置复位）")
    if not 0 <= budget <= len(cands):
        raise ValueError("预算 B 必须满足 0 <= B <= K")
    for card in cards:
        for j in card.jumps:
            if j.target < 0 or j.expected_plays < 0 or j.preread_limit < 0:
                raise ValueError(f"卡 {card.card_id} 存在负的跳点参数")

    best: dict[str, Any] = {"value": -1, "preread": None,
                            "layout": (), "decisions": None}

    def dfs(idx: int, chosen: list[int]) -> None:
        # 预算用尽后，更深的选择只会产生同一布局，直接在叶点评估即可
        if len(chosen) > budget:
            return
        decisions, value, preread = evaluate_layout(cards, chosen, n_samples)
        layout = tuple(chosen)
        if (value > best["value"]
                or (value == best["value"]
                    and (best["preread"] is None
                         or preread < best["preread"]
                         or (preread == best["preread"]
                             and (not best["layout"] or layout < best["layout"]))))):
            best.update(value=value, preread=preread, layout=layout,
                        decisions=decisions)
        if idx == len(cands):
            return
        # 已达到全局价值上界（全部卡可售）时，更深的选择只会得到同值布局：
        # 仍要在当前子树内枚举以执行第二目标，故仅当本分支再无可加位置才停。
        if best["value"] >= sum(c.value for c in cards) and \
                len(chosen) >= budget:
            return
        # 选 cand[idx]
        chosen.append(cands[idx])
        dfs(idx + 1, chosen)
        chosen.pop()
        # 不选 cand[idx]
        dfs(idx + 1, chosen)

    dfs(0, [])
    return LayoutResult(
        burned_positions=best["layout"],
        total_value=best["value"],
        weighted_preread=best["preread"],
        decisions=best["decisions"],
        candidates=tuple(cands),
        budget=budget,
    )
