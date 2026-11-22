"""端到端走查：题面例子 + 双来源版本 + 多卡比较，全部走 CLI 可复跑。

用法：python3 -m demo.cli_walkthrough [输出目录]
产出：
  <out>/spec/build.json        封版规格（两段旧 render 拼带）
  <out>/bundle-v1/             实际刻录的母带交付目录（v1）
  <out>/cards-example.json     题面三张卡
  <out>/plan-example.json      销售计划（刻 4，售甲丙=9）
  <out>/cards-example-10.json  乙提到 10 后的卡目录
  <out>/plan-example-10.json   改刻 8 的计划
  <out>/bundle-v2/             同 PCM、移动具名引用的第二来源版本
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(*args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "tourbox.cli", *args],
        cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"命令失败: {' '.join(args)}\n{proc.stderr}")
    return json.loads(proc.stdout)


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def main(out: str) -> None:
    spec_dir = os.path.join(out, "spec")
    b1, b2 = os.path.join(out, "bundle-v1"), os.path.join(out, "bundle-v2")

    # ---- 两段旧 render（合成 16kHz int16），第二段包含 32767->-32768 大跳变 ---- #
    rA = [10, 20, 30, 40, 50, 60, 32767, 300, 290, 280]
    rB = [-32768, -32000, -31000, 100, 110, 120, 130, 140, 150, 160]
    write_json(os.path.join(spec_dir, "build.json"), {
        "master_id": "exhibit-master-v1",
        "sample_rate": 16000,
        "approved_reset_positions": [0, 4, 8, 10, 15],
        "burned_reset_positions": [0, 4],       # 与“刻 4”的计划一致
        "segments": [
            {"render_id": "old-render-A", "lo": 0, "hi": 10,
             "clip_id": "clip-narr", "occurrence": 0, "license_id": "LIC-A",
             "samples": rA},
            {"render_id": "old-render-B", "lo": 0, "hi": 10,
             "clip_id": "clip-sfx", "occurrence": 0, "license_id": "LIC-B",
             "samples": rB},
        ],
    })
    print("== 1) 封版差分母带（两段旧 render 顺序拼带） ==")
    print(json.dumps(run("build", "--spec", os.path.join(spec_dir, "build.json"),
                         "--out", b1), ensure_ascii=False, indent=2))

    print("== 2) 离线核对：流还原/接缝/每个候选片头复播/来源对应 ==")
    report = run("verify", "--bundle", b1)
    print(json.dumps({"ok": report["ok"],
                      "verified_candidate_points":
                          report["verified_candidate_points"],
                      "seam_checks": report["seam_checks"],
                      "provenance_runs_checked":
                          report["provenance_runs_checked"]},
                     ensure_ascii=False, indent=2))

    # ---- 题面卡：候选 {4,8}，预算 1 ---- #
    cards = {
        "candidate_reset_positions": [4, 8],
        "budget": 1,
        "cards": [
            {"card_id": "甲", "value": 4,
             "jumps": [{"target": 5, "expected_plays": 1, "preread_limit": 1}]},
            {"card_id": "乙", "value": 6,
             "jumps": [{"target": 9, "expected_plays": 1, "preread_limit": 1}]},
            {"card_id": "丙", "value": 5,
             "jumps": [{"target": 5, "expected_plays": 1, "preread_limit": 1},
                       {"target": 9, "expected_plays": 1, "preread_limit": 5}]},
        ],
    }
    cards_path = os.path.join(out, "cards-example.json")
    write_json(cards_path, cards)
    print("== 3) 题面例：刻 4，售甲+丙=9，乙不可承诺 ==")
    plan1 = run("plan", "--bundle", b1, "--cards", cards_path,
                "--out", os.path.join(out, "plan-example.json"))
    print(json.dumps(plan1, ensure_ascii=False, indent=2))
    assert plan1["total_value"] == 9 and plan1["sold"] == ["甲", "丙"]

    print("== 4) 设备侧只凭交付文件复核计划并逐跳复播 ==")
    print(json.dumps(run("verify-plan", "--bundle", b1,
                         "--plan", os.path.join(out, "plan-example.json")),
                     ensure_ascii=False, indent=2))

    # ---- 乙提到 10：应改刻 8 ---- #
    cards10 = json.loads(json.dumps(cards))
    cards10["cards"][1]["value"] = 10
    cards10_path = os.path.join(out, "cards-example-10.json")
    write_json(cards10_path, cards10)
    print("== 5) 乙价值提到 10：改刻 8，只售乙=10 ==")
    plan2 = run("plan", "--bundle", b1, "--cards", cards10_path,
                "--out", os.path.join(out, "plan-example-10.json"))
    print(json.dumps(plan2, ensure_ascii=False, indent=2))
    assert plan2["total_value"] == 10 and plan2["burned_reset_positions"] == [0, 8]
    print("   注意：改刻需要编带师出母带新版本；旧 plan-example.json 与旧包仍可复核。")

    # ---- 双来源版本：PCM 完全相同，仅移动一个具名引用的授权身份 ---- #
    write_json(os.path.join(spec_dir, "build-v2.json"), {
        "master_id": "exhibit-master-v2",
        "sample_rate": 16000,
        "approved_reset_positions": [0, 4, 8, 10, 15],
        "burned_reset_positions": [0, 4],
        "segments": [
            {"render_id": "old-render-A", "lo": 0, "hi": 10,
             "clip_id": "clip-narr", "occurrence": 0, "license_id": "LIC-A",
             "samples": rA},
            # 同样的 10 个样本，但来源身份换成另一条 render/授权
            {"render_id": "old-render-C", "lo": 0, "hi": 10,
             "clip_id": "clip-sfx", "occurrence": 1, "license_id": "LIC-C",
             "samples": rB},
        ],
    })
    run("build", "--spec", os.path.join(spec_dir, "build-v2.json"), "--out", b2)
    v1_pcm = open(os.path.join(b1, "master.pcm"), "rb").read()
    v2_pcm = open(os.path.join(b2, "master.pcm"), "rb").read()
    assert v1_pcm == v2_pcm, "两版母带样本必须逐字节相同"
    m1, m2 = (json.load(open(os.path.join(b, "manifest.json"), encoding="utf-8"))
              for b in (b1, b2))
    assert m1["provenance_runs"][1]["provenance_id"] != \
        m2["provenance_runs"][1]["provenance_id"]
    print("== 6) 双来源版本：PCM 相同但第二段 provenance_id 不同 ==")
    print(json.dumps({"v1_run1": m1["provenance_runs"][1]["identity"],
                      "v2_run1": m2["provenance_runs"][1]["identity"]},
                     ensure_ascii=False, indent=2))

    # 旧卡（按母带位置）两版都能复播；新卡按具名 clip 源位置锚定
    old_card = {"card_id": "old-sold", "value": 3,
                "jumps": [{"target": 11, "expected_plays": 2, "preread_limit": 2}]}
    new_card = {"card_id": "source-anchored", "value": 3,
                "clip_id": "clip-sfx", "occurrence": 1, "source_sample": 1,
                "jumps": [{"target": 11, "expected_plays": 2, "preread_limit": 2}]}
    write_json(os.path.join(out, "card-old.json"), old_card)
    write_json(os.path.join(out, "card-new.json"), new_card)
    print("== 7) 旧位置卡两版忠实复播；新卡在 v2 锚到 LIC-C 的具名引用 ==")
    old_replay = run("replay-card", "--bundle", b2,
                     "--card", os.path.join(out, "card-old.json"))
    # feasible=false 表示按该卡的预读上限不能销售，但设备仍能只凭交付文件
    # 忠实还原出实际 WAV 段——“能否销售”与“能否复播”是两件事，分别留痕。
    print(json.dumps(old_replay, ensure_ascii=False, indent=2))
    print(json.dumps(run("anchor", "--bundle", b2,
                         "--card", os.path.join(out, "card-new.json")),
                     ensure_ascii=False, indent=2))
    print("\n端到端走查全部通过；所有结论均可由交付目录离线复跑。")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/tourbox_demo")
