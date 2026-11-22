"""复位点布局优化、计划封版/复核与具名锚点测试。"""
import os
import shutil

from tourbox.master import BuildRequest, SourceSegment, build_bundle
from tourbox.plan import (
    replay_card, resolve_anchor, result_to_plan, save_plan, verify_plan,
)
from tourbox.planner import Card, Jump, plan_layout, serving_reset

BASE = "/tmp/tourbox_plan"


def _card(cid, jumps, value, anchor=None):
    kw = {} if anchor is None else anchor
    return Card(cid, tuple(Jump(*j) for j in jumps), value, **kw)


def test_worked_example_exact():
    """题面例：起点 0 已复位，候选 {4,8} 选一个。"""
    jia = _card("甲", [(5, 1, 1)], 4)
    yi = _card("乙", [(9, 1, 1)], 6)
    bing = _card("丙", [(5, 1, 1), (9, 1, 5)], 5)
    cards = [jia, yi, bing]

    res = plan_layout(cards, [4, 8], budget=1)
    assert res.total_value == 9
    assert res.burned_positions == (4,)
    assert set(res.sold_cards()) == {"甲", "丙"}
    unsold = {u["card_id"]: u for u in res.unsold_cards()}
    assert set(unsold) == {"乙"}
    # 乙不能承诺的具体跳点：目标 9，最近复位点 0（4 不晚于 9？是 4），预读 5 > 上限 1
    blocked = unsold["乙"]["blocked_jumps"][0]
    assert blocked["target"] == 9 and blocked["actual_preread"] == 5
    print("[OK] 题面例：刻 4 售甲+丙=9，乙因跳点 9 预读 5>1 不可承诺")

    # 将乙的价值提到 10：单售乙(10) > 甲+丙(9)，应当改刻 8
    yi2 = _card("乙", [(9, 1, 1)], 10)
    res2 = plan_layout([jia, yi2, bing], [4, 8], budget=1)
    assert res2.total_value == 10 and res2.burned_positions == (8,)
    assert res2.sold_cards() == ["乙"]
    print("[OK] 乙价值提到 10 后改刻 8（10 > 9），甲丙具体阻塞跳点均在案")


def test_preread_feasibility_math():
    # 从最近的不晚于目标的复位点 r 跳到 t，丢弃 t-r 个样本
    assert serving_reset(5, [0, 4]) == 4
    assert serving_reset(4, [0, 4]) == 4       # 复位点本身：预读 0
    assert serving_reset(3, [0, 4]) == 0       # 4 晚于目标，不能用
    assert serving_reset(9, [0, 4, 8]) == 8
    print("[OK] 服务复位点 = 最近不晚于目标者")


def test_tie_break_min_weighted_preread():
    """等值布局之间，已售卡各次跳听的加权预读总数最少。"""
    # 两张互斥等价的卡：选复位点 2 或 6 都只能售一张，价值都为 7
    c_left = _card("L", [(3, 10, 1)], 7)    # 刻 2：预读 1，加权 10
    c_right = _card("R", [(7, 10, 1)], 7)   # 刻 6：预读 1，加权 10
    r1 = plan_layout([c_left, c_right], [2, 6], 1)
    assert r1.total_value == 7 and r1.burned_positions == (2,)  # 完全等平时字典序
    # 加大左侧卡点播次数：两布局价值仍相同，但刻 2 的加权预读更小？
    # 构造真正同卡集、两布局同价值不同预读：
    card = _card("C", [(5, 3, 10), (9, 2, 10)], 7)
    # 刻4: t5->r4 预读1(加权3), t9->r4 预读5(加权10) => 13
    # 刻8: t5->r0 预读5(加权15), t9->r8 预读1(加权2) => 17
    r2 = plan_layout([card], [4, 8], 1)
    assert r2.total_value == 7 and r2.burned_positions == (4,)
    assert r2.weighted_preread == 3 * 1 + 2 * 5
    print("[OK] 同值布局按加权预读最小选择，完全等平时确定性字典序兜底")


def test_multi_jump_cards_shared_and_disjoint():
    # 高度共享片头的两张卡（都跳 5）与互不重叠的卡，预算 2
    shared1 = _card("S1", [(5, 4, 2), (6, 2, 2)], 3)
    shared2 = _card("S2", [(5, 9, 2)], 3)
    far = _card("F", [(20, 1, 1)], 5)
    res = plan_layout([shared1, shared2, far], [4, 8, 19], 2)
    # 刻 4 服务 S1/S2（预读 1/2/1），刻 19 服务 F（预读 1）；8 落选
    assert set(res.burned_positions) == {4, 19}
    assert set(res.sold_cards()) == {"S1", "S2", "F"}
    assert res.total_value == 11
    print("[OK] 多跳点卡：共享片头被一次复位覆盖，互不重叠卡各自占点")


def _build_master(bdir, runs_pcm, approved, burned):
    segs = []
    for idx, (rid, pcm, clip, occ, lic) in enumerate(runs_pcm):
        segs.append(SourceSegment(
            rid, 0, len(pcm), clip, occ, lic,
            {"sample_count": len(pcm), "sample_rate": 16000, "_pcm": list(pcm)}))
    req = BuildRequest("m-plan", 16000, segs, approved, burned)
    return build_bundle(bdir, req)


def test_plan_seal_replay_and_anchor():
    shutil.rmtree(BASE, ignore_errors=True)
    # 24 样本母带；同一音效 clip-sfx 出现两次（occurrence 0/1），正文恰好相同
    sfx = [1000, 1001, 1002, 1003]
    pcm_runs = [
        ("r1", list(range(0, 8)), "clip-narr", 0, "LIC-A"),
        ("r2", sfx, "clip-sfx", 0, "LIC-A"),
        ("r3", list(range(20, 28)), "clip-narr", 1, "LIC-A"),
        ("r4", sfx, "clip-sfx", 1, "LIC-B"),   # 同 PCM、不同授权、另一次引用
    ]
    bdir = os.path.join(BASE, "v1")
    manifest = _build_master(bdir, pcm_runs, [0, 8, 16, 20], [0, 8, 20])
    assert manifest["n_samples"] == 24

    jia = _card("甲", [(9, 3, 2)], 4)
    bing = _card("丙", [(21, 2, 2)], 5,
                 anchor={"clip_id": "clip-sfx", "occurrence": 1,
                         "source_sample": 1})
    cards = [jia, bing]
    result = plan_layout(cards, [8, 16, 20], 2, manifest["n_samples"])
    plan_obj = result_to_plan(manifest["master_id"], cards, result)
    plan_path = os.path.join(BASE, "v1_plan.json")
    save_plan(plan_path, plan_obj)

    # 设备侧：只凭交付文件复核计划并逐跳复播
    check = verify_plan(bdir, plan_path)
    assert check["plan_ok"] and set(check["sold"]) == {"甲", "丙"}
    for rp in check["replays"]:
        assert rp["discarded"] >= 0 and rp["emitted_samples"] > 0
    print("[OK] 计划封版 + 设备侧重解优化问题 + 逐跳丢弃预读复播全部通过")

    # 锚点：沿具名 clip 的源位置找片头；同音效两次使用，只跟随签下的 occurrence=1
    anchor = resolve_anchor(bing, manifest)
    assert anchor["found"] and anchor["provenance_id"] == \
        manifest["provenance_runs"][3]["provenance_id"]
    # r4 起始于母带样本 20，源位置 1 => 新目标 21
    assert anchor["new_target"] == 21 and anchor["license_id"] == "LIC-B"
    wrong = Card("丙x", (Jump(21, 2, 2),), 5,
                 clip_id="clip-sfx", occurrence=0, source_sample=1)
    a0 = resolve_anchor(wrong, manifest)
    assert a0["found"] and a0["new_target"] == 9   # 跟随的是第一次（r2，起点 8）
    print("[OK] 同音效反复使用时按 occurrence 只跟随签下的那一次")

    # 节目调整出母带 v2：删掉 occurrence=1 的那次引用 -> 新卡不能承诺
    bdir2 = os.path.join(BASE, "v2")
    pcm_v2 = [r for r in pcm_runs if not (r[0] == "r4")]
    man2 = _build_master(bdir2, pcm_v2, [0, 8, 16], [0, 8])
    gone = resolve_anchor(bing, man2)
    assert not gone["found"] and "不能承诺" in gone["reason"]
    # 旧售出包（v1 + 旧计划）仍可原样复核
    old_check = verify_plan(bdir, plan_path)
    assert old_check["plan_ok"]
    print("[OK] 新版本无法兑现的锚点明确拒承诺；旧售出包不受影响仍可复核")


def test_two_source_versions_same_samples_moved_named_reference():
    """题面要求：两个来源版本实际样本完全相同，只移动其中一个具名引用。

    旧售出的包按母带样本位置继续复播（位置合同不变）；
    按源位置（具名 clip + occurrence）生成的新卡必须跟随引用移动，
    且同音效多次使用时只跟随签下的那一次，绝不让两份授权互相串用。
    """
    shutil.rmtree(os.path.join(BASE, "ver"), ignore_errors=True)
    sfx = [1000, 1001, 1002]            # 同一音效正文，出现两次、授权不同
    narr_a = list(range(0, 6))
    narr_b = list(range(50, 56))
    # v1 顺序：narrA(6) + sfx(occ0,LIC-X)(3) + narrB(6) + sfx(occ1,LIC-Y)(3)
    v1_runs = [
        ("r1", narr_a, "clip-narr", 0, "LIC-A"),
        ("r2", sfx, "clip-sfx", 0, "LIC-X"),
        ("r3", narr_b, "clip-narr", 1, "LIC-A"),
        ("r4", sfx, "clip-sfx", 1, "LIC-Y"),
    ]
    bdir1 = os.path.join(BASE, "ver", "v1")
    man1 = _build_master(bdir1, v1_runs, [0, 6, 9, 15], [0, 6, 15])
    # v2：PCM 与顺序完全不变，仅把第二次 sfx 的具名引用改挂到另一来源身份
    # （不同 render_id / 授权 LIC-Z），样本字节逐位相同
    v2_runs = [
        ("r1", narr_a, "clip-narr", 0, "LIC-A"),
        ("r2", sfx, "clip-sfx", 0, "LIC-X"),
        ("r3", narr_b, "clip-narr", 1, "LIC-A"),
        ("r5", sfx, "clip-sfx", 1, "LIC-Z"),   # 位置仍是母带 [15,18)
    ]
    bdir2 = os.path.join(BASE, "ver", "v2")
    man2 = _build_master(bdir2, v2_runs, [0, 6, 9, 15], [0, 6, 15])

    # 两版已渲染 PCM 必须逐样本相同
    from tourbox.pcmio import read_pcm_file
    p1 = read_pcm_file(os.path.join(bdir1, "master.pcm"))
    p2 = read_pcm_file(os.path.join(bdir2, "master.pcm"))
    assert p1 == p2
    # 但第 4 段的来源身份不同：同 PCM 不并源
    assert man1["provenance_runs"][3]["provenance_id"] != \
        man2["provenance_runs"][3]["provenance_id"]
    assert man1["provenance_runs"][3]["pcm_sha256"] == \
        man2["provenance_runs"][3]["pcm_sha256"]

    # 旧卡：按母带样本位置 16 起播。两版位置合同一致 => 都能原样复播
    old_card = _card("old-pkg", [(16, 2, 2)], 5)
    info1 = replay_card(bdir1, old_card)
    info2 = replay_card(bdir2, old_card)
    assert info1["jumps"][0]["feasible"] and info2["jumps"][0]["feasible"]
    assert info1["jumps"][0]["serving_reset"] == \
        info2["jumps"][0]["serving_reset"] == 15
    print("[OK] 同 PCM 的两个来源版本：旧位置卡两版均可原样复播（售出包不受影响）")

    # 新卡：沿具名 clip 源位置生成。occurrence=1 在 v1 解析到 LIC-Y 的 r4，
    # 在 v2 解析到 LIC-Z 的 r5；锚点跟随的是签下的那次引用而非同字节音效
    new_card = Card("new-pkg", (Jump(0, 2, 2),), 5,
                    clip_id="clip-sfx", occurrence=1, source_sample=1)
    a1 = resolve_anchor(new_card, man1)
    a2 = resolve_anchor(new_card, man2)
    assert a1["found"] and a2["found"]
    assert a1["license_id"] == "LIC-Y" and a2["license_id"] == "LIC-Z"
    assert a1["provenance_id"] != a2["provenance_id"]
    assert a1["new_target"] == a2["new_target"] == 16  # 母带位置未变
    # occurrence=0 的卡始终锚在 r2（LIC-X），不受第二次引用换源影响
    card_occ0 = Card("occ0", (Jump(0, 2, 2),), 5,
                     clip_id="clip-sfx", occurrence=0, source_sample=1)
    assert resolve_anchor(card_occ0, man1)["license_id"] == "LIC-X"
    assert resolve_anchor(card_occ0, man2)["license_id"] == "LIC-X"
    print("[OK] 按源位置生成的新卡跟随具名引用换授权；同音效另一次引用不串台")

    # 再做 v3：把 occ1 的 sfx 挪到 narrB 之前（母带位置变为 [6,9)），
    # 同时用静音补足，保证这是一次真正的节目调整
    v3_runs = [
        ("r1", narr_a, "clip-narr", 0, "LIC-A"),
        ("r5", sfx, "clip-sfx", 1, "LIC-Z"),    # 具名引用前移
        ("r3", narr_b, "clip-narr", 1, "LIC-A"),
        ("r2", sfx, "clip-sfx", 0, "LIC-X"),
    ]
    bdir3 = os.path.join(BASE, "ver", "v3")
    man3 = _build_master(bdir3, v3_runs, [0, 6, 9, 15], [0, 6, 15])
    a3 = resolve_anchor(new_card, man3)
    assert a3["found"] and a3["new_target"] == 7   # 源位置 1 => 6+1
    # 旧位置卡仍按样本位置 16 复播，但那里现在是另一段内容；设备只忠实复播，
    # 由锚点解析负责告知编带师该卡需要重新签约，旧包本身的复核结论不变
    old_replay = replay_card(bdir3, old_card)
    assert old_replay["jumps"][0]["feasible"]
    print("[OK] 引用移动后新卡锚到新片头（16->7）；旧包复核语义保持位置忠实")


def test_replay_card_uses_only_bundle_files():
    bdir = os.path.join(BASE, "v1")
    card = _card("甲", [(9, 3, 2)], 4)
    info = replay_card(bdir, card, os.path.join(BASE, "jia.wav"))
    j = info["jumps"][0]
    assert j["serving_reset"] == 8 and j["discarded"] == 1 and j["feasible"]
    assert os.path.exists(os.path.join(BASE, "jia.wav"))
    print("[OK] 逐卡复播导出实际 WAV，全过程只读交付目录")


if __name__ == "__main__":
    test_worked_example_exact()
    test_preread_feasibility_math()
    test_tie_break_min_weighted_preread()
    test_multi_jump_cards_shared_and_disjoint()
    test_plan_seal_replay_and_anchor()
    test_two_source_versions_same_samples_moved_named_reference()
    test_replay_card_uses_only_bundle_files()
    print("计划/锚点测试全部通过")
