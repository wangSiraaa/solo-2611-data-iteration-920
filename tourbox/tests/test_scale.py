"""布局求解的规模与边界测试：精确解在候选较多时仍可复跑、可解释。"""
import time

from tourbox.planner import Card, Jump, plan_layout


def _make_cards(n_cards: int, n_cands: int) -> list[Card]:
    cards = []
    for i in range(n_cards):
        # 每张卡 1~3 个跳点，位置散布在候选轴上，部分卡共享同一片头
        jumps = []
        base = (i * 3) % n_cands
        for k in range(1 + (i % 3)):
            t = min(base + k + 1, n_cands) * 10
            jumps.append(Jump(t, expected_plays=1 + (i % 5),
                              preread_limit=10))
        cards.append(Card(f"c{i:02d}", tuple(jumps), value=(i % 7) + 1))
    return cards


def test_scale_exact_solver():
    cands = list(range(10, 160, 10))   # K=15 个候选
    cards = _make_cards(n_cards=40, n_cands=len(cands))
    t0 = time.time()
    res = plan_layout(cards, cands, budget=4)
    elapsed = time.time() - t0
    # 精确枚举 2^15 量级子集，秒级完成
    assert elapsed < 10, elapsed
    assert len(res.burned_positions) <= 4
    # 可解释性：每张已售卡的每个跳点都带着服务复位点与实际预读
    for d in res.decisions:
        if d.sold:
            assert all(j.feasible for j in d.jumps)
            for j in d.jumps:
                assert j.serving_reset in (0,) + res.burned_positions
                assert j.preread == j.target - j.serving_reset
        else:
            assert any(not j.feasible for j in d.jumps)
    # 价值口径自洽：独立按最优布局重算一遍
    from tourbox.planner import evaluate_layout
    decs, value, preread = evaluate_layout(cards, res.burned_positions)
    assert value == res.total_value and preread == res.weighted_preread
    print(f"[OK] 规模案例 K=15/40 卡/预算4：精确解 {elapsed:.2f}s，"
          f"价值 {res.total_value}，售 {len(res.sold_cards())} 张，"
          f"加权预读 {res.weighted_preread}")


def test_brute_force_agreement_small():
    """小空间暴力枚举所有子集，验证分支限界结果与全局最优完全一致。"""
    import itertools
    cands = [3, 7, 11]
    cards = _make_cards(n_cards=8, n_cands=3)
    res = plan_layout(cards, cands, budget=2)

    def score(subset):
        from tourbox.planner import evaluate_layout
        _, v, w = evaluate_layout(cards, subset)
        return v, -w, tuple(sorted(subset))

    brute = max(
        (s for r in range(3) for s in itertools.combinations(cands, r)),
        key=score)
    assert res.burned_positions == tuple(sorted(brute))
    print("[OK] 与暴力枚举的全局最优（价值→预读→字典序）完全一致")


def test_edge_cases():
    # 无卡：价值 0，布局为空
    r0 = plan_layout([], [4, 8], 1)
    assert r0.total_value == 0 and r0.burned_positions == ()
    # 预算 0：只能靠起点 0 服务
    card = Card("only", (Jump(2, 1, 2),), 3)
    r1 = plan_layout([card], [4, 8], 0)
    assert r1.sold_cards() == ["only"] and r1.burned_positions == ()
    # 目标越出母带：永远不可承诺，并在阻塞跳点中留痕
    far = Card("far", (Jump(99, 1, 100),), 3)
    r2 = plan_layout([far], [4, 8], 2, n_samples=50)
    assert r2.sold_cards() == []
    assert r2.unsold_cards()[0]["blocked_jumps"][0]["target"] == 99
    # 空候选 + 预算 0 合法
    r3 = plan_layout([card], [], 0)
    assert r3.burned_positions == ()
    print("[OK] 边界：无卡、零预算、越界目标、空候选均行为确定")


if __name__ == "__main__":
    test_scale_exact_solver()
    test_brute_force_agreement_small()
    test_edge_cases()
    print("规模/边界测试全部通过")
