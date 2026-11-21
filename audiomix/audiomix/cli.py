"""audiomix 命令行：素材登记 / 编辑 / 发布 / 渲染 / 读取 / 验证。

有理数参数统一接受 '1/3'、'0.25'、'7' 等字符串（内部精确 Fraction）。
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np

from . import assets as assets_mod
from .archive import ArchiveStore
from .assets import AssetRegistry
from .engine import (
    OUTPUT_RATE, render_full_vectorized, render_reference, validate_product,
)
from .rationals import as_r, dump_r
from .timeline import TimelineEditor, ValidationError


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _registry(root: str) -> AssetRegistry:
    return AssetRegistry(root)


# ----------------------------- 素材命令 ----------------------------- #

def cmd_asset_seq(p):
    reg = _registry(p.root)
    values = [int(v) for v in p.values.split(",") if v.strip() != ""]
    rec = reg.register(assets_mod.sequence(values), p.rate, note=p.note)
    _print(rec.to_json())


def cmd_asset_ramp(p):
    reg = _registry(p.root)
    rec = reg.register(assets_mod.ramp(p.rate, p.n, p.start, p.stop), p.rate, note=p.note)
    _print(rec.to_json())


def cmd_asset_tone(p):
    reg = _registry(p.root)
    rec = reg.register(assets_mod.tone(p.rate, p.freq, p.seconds, p.amp), p.rate, note=p.note)
    _print(rec.to_json())


def cmd_asset_silence(p):
    reg = _registry(p.root)
    rec = reg.register(assets_mod.silence(p.rate, p.seconds), p.rate, note=p.note)
    _print(rec.to_json())


def cmd_asset_list(p):
    _print([r.to_json() for r in _registry(p.root).all()])


def cmd_asset_show(p):
    _print(_registry(p.root).get(p.asset_id).to_json())


# ----------------------------- 编辑命令 ----------------------------- #

def _editor(p) -> TimelineEditor:
    return TimelineEditor(p.root, _registry(p.root), name=p.name)


def _clip_spec_from_args(p) -> dict:
    spec = {"clip_id": p.clip_id, "asset_id": p.asset_id, "a": p.a, "b": p.b,
            "start": p.start}
    for key in ("speed", "gain", "fade_in", "fade_out"):
        val = getattr(p, key, None)
        if val is not None:
            spec[key] = val
    return spec


def cmd_tl_insert(p):
    ed = _editor(p)
    try:
        ids = ed.insert(_clip_spec_from_args(p))
    except ValidationError as e:
        _print({"rejected": True, "reason": str(e)})
        sys.exit(2)
    _print({"inserted": ids, "editor_seq": ed._seq})


def cmd_tl_move(p):
    ed = _editor(p)
    try:
        ed.move(p.clip_id, p.new_start)
    except ValidationError as e:
        _print({"rejected": True, "reason": str(e)})
        sys.exit(2)
    _print({"moved": p.clip_id, "editor_seq": ed._seq})


def cmd_tl_modify(p):
    ed = _editor(p)
    changes = {}
    for item in p.set:
        k, v = item.split("=", 1)
        k = k.strip()
        if k in ("a", "b"):
            changes[k] = int(v)
        else:
            changes[k] = v
    try:
        ed.modify(p.clip_id, changes)
    except ValidationError as e:
        _print({"rejected": True, "reason": str(e)})
        sys.exit(2)
    _print({"modified": p.clip_id, "editor_seq": ed._seq})


def cmd_tl_delete(p):
    ed = _editor(p)
    try:
        ed.delete(p.clip_id)
    except ValidationError as e:
        _print({"rejected": True, "reason": str(e)})
        sys.exit(2)
    _print({"deleted": p.clip_id, "editor_seq": ed._seq})


def cmd_tl_query(p):
    _print(_editor(p).query_interval(p.lo, p.hi))


def cmd_tl_at(p):
    _print(_editor(p).query_at(p.time))


def cmd_tl_publish(p):
    ed = _editor(p)
    product = ed.publish()
    ed.save_product(product, p.out)
    _print({"product_id": product["product_id"], "path": p.out,
            "clips": len(product["clips"]), "assets": len(product["assets"])})


# ----------------------------- 渲染/读取/验证命令 ----------------------------- #

def _load_pcm_for_product(product_path: str, registry_root: str) -> tuple[dict, dict]:
    from .engine import load_product
    product = load_product(product_path)
    reg = AssetRegistry(registry_root)
    pcm = {a["asset_id"]: reg.load_pcm(a["asset_id"]) for a in product["assets"]}
    return product, pcm


def cmd_render(p):
    store = ArchiveStore(p.archive_root)
    info = store.render_from_files(p.product, AssetRegistry(p.registry_root),
                                   as_r(p.T), render_id=p.render_id)
    _print(info)
    store.close()


def cmd_render_list(p):
    store = ArchiveStore(p.archive_root)
    _print(store.list_renders())
    store.close()


def cmd_render_show(p):
    store = ArchiveStore(p.archive_root)
    _print(store.render_meta(p.render_id))
    store.close()


def cmd_read(p):
    store = ArchiveStore(p.archive_root)
    pcm = store.read_samples(p.render_id, p.start, p.end)
    result = {"render_id": p.render_id, "range": [p.start, p.end],
              "samples": [int(v) for v in pcm]}
    if p.wav:
        # 范围导出 WAV
        import wave, os
        os.makedirs(os.path.dirname(os.path.abspath(p.wav)) or ".", exist_ok=True)
        with wave.open(p.wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(OUTPUT_RATE)
            wf.writeframes(pcm.tobytes())
        result["wav"] = p.wav
    _print(result)
    store.close()


def cmd_trace(p):
    store = ArchiveStore(p.archive_root)
    _print(store.read_trace(p.render_id, p.start, p.end))
    store.close()


def cmd_export_wav(p):
    store = ArchiveStore(p.archive_root)
    path = store.export_wav(p.render_id, p.out)
    _print({"render_id": p.render_id, "wav": path})
    store.close()


def cmd_verify_product(p):
    product, pcm = _load_pcm_for_product(p.product, p.registry_root)
    validate_product(product, pcm)
    _print({"valid": True, "product_id": product["product_id"],
            "clips": len(product["clips"])})


def cmd_verify_integrity(p):
    store = ArchiveStore(p.archive_root)
    store.verify_block_pcm_integrity()
    _print({"integrity_ok": True, "blocks": store.block_count(),
            "renders": len(store.list_renders())})
    store.close()


def cmd_render_compare(p):
    """整段向量化渲染 vs 独立逐样本解释器，逐样本一致性核对。"""
    product, pcm = _load_pcm_for_product(p.product, p.registry_root)
    T = as_r(p.T)
    ref = render_reference(product, pcm, T)
    fast = render_full_vectorized(product, pcm, T)
    mismatch = int(np.count_nonzero(ref != fast))
    _print({"T": dump_r(T), "n_samples": len(ref),
            "mismatch_samples": mismatch,
            "identical": bool(mismatch == 0)})


# ----------------------------- 参数解析 ----------------------------- #

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="audiomix",
                                 description="多采样率剪辑时间轴与真实混音档案")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_asset(sp, name, fn):
        q = sp.add_parser(name)
        q.add_argument("--root", required=True)
        q.add_argument("--rate", type=int, required=True, choices=assets_mod.ALLOWED_RATES)
        q.add_argument("--note", default="")
        q.set_defaults(fn=fn)
        return q

    q = add_asset(sub, "asset-seq", cmd_asset_seq)
    q.add_argument("--values", required=True, help="逗号分隔短整数，如 10,20,30")
    q = add_asset(sub, "asset-ramp", cmd_asset_ramp)
    q.add_argument("--n", type=int, required=True)
    q.add_argument("--start", type=int, default=-30000)
    q.add_argument("--stop", type=int, default=30000)
    q = add_asset(sub, "asset-tone", cmd_asset_tone)
    q.add_argument("--freq", type=float, required=True)
    q.add_argument("--seconds", type=float, required=True)
    q.add_argument("--amp", type=float, default=0.5)
    q = add_asset(sub, "asset-silence", cmd_asset_silence)
    q.add_argument("--seconds", type=float, required=True)

    q = sub.add_parser("asset-list"); q.add_argument("--root", required=True)
    q.set_defaults(fn=cmd_asset_list)
    q = sub.add_parser("asset-show"); q.add_argument("--root", required=True)
    q.add_argument("--asset-id", required=True); q.set_defaults(fn=cmd_asset_show)

    def tl_parser(name, fn):
        q = sub.add_parser(name)
        q.add_argument("--root", required=True)
        q.add_argument("--name", default="main")
        q.set_defaults(fn=fn)
        return q

    q = tl_parser("tl-insert", cmd_tl_insert)
    q.add_argument("--clip-id", required=True); q.add_argument("--asset-id", required=True)
    q.add_argument("--a", type=int, required=True); q.add_argument("--b", type=int, required=True)
    q.add_argument("--start", required=True)
    q.add_argument("--speed"); q.add_argument("--gain")
    q.add_argument("--fade-in", dest="fade_in"); q.add_argument("--fade-out", dest="fade_out")

    q = tl_parser("tl-move", cmd_tl_move)
    q.add_argument("--clip-id", required=True); q.add_argument("--new-start", required=True)
    q = tl_parser("tl-modify", cmd_tl_modify)
    q.add_argument("--clip-id", required=True)
    q.add_argument("--set", action="append", required=True,
                   help="key=value，可多次；key 取 a/b/start/speed/gain/fade_in/fade_out/asset_id")
    q = tl_parser("tl-delete", cmd_tl_delete)
    q.add_argument("--clip-id", required=True)
    q = tl_parser("tl-query", cmd_tl_query)
    q.add_argument("--lo", required=True); q.add_argument("--hi", required=True)
    q = tl_parser("tl-at", cmd_tl_at)
    q.add_argument("--time", required=True)
    q = tl_parser("tl-publish", cmd_tl_publish)
    q.add_argument("--out", required=True)

    q = sub.add_parser("render")
    q.add_argument("--archive-root", required=True)
    q.add_argument("--product", required=True)
    q.add_argument("--registry-root", required=True)
    q.add_argument("--T", required=True); q.add_argument("--render-id")
    q.set_defaults(fn=cmd_render)

    q = sub.add_parser("render-list"); q.add_argument("--archive-root", required=True)
    q.set_defaults(fn=cmd_render_list)
    q = sub.add_parser("render-show"); q.add_argument("--archive-root", required=True)
    q.add_argument("--render-id", required=True); q.set_defaults(fn=cmd_render_show)

    q = sub.add_parser("read"); q.add_argument("--archive-root", required=True)
    q.add_argument("--render-id", required=True)
    q.add_argument("--start", type=int, required=True); q.add_argument("--end", type=int, required=True)
    q.add_argument("--wav"); q.set_defaults(fn=cmd_read)

    q = sub.add_parser("trace"); q.add_argument("--archive-root", required=True)
    q.add_argument("--render-id", required=True)
    q.add_argument("--start", type=int, required=True); q.add_argument("--end", type=int, required=True)
    q.set_defaults(fn=cmd_trace)

    q = sub.add_parser("export-wav"); q.add_argument("--archive-root", required=True)
    q.add_argument("--render-id", required=True); q.add_argument("--out", required=True)
    q.set_defaults(fn=cmd_export_wav)

    q = sub.add_parser("verify-product"); q.add_argument("--product", required=True)
    q.add_argument("--registry-root", required=True); q.set_defaults(fn=cmd_verify_product)

    q = sub.add_parser("verify-integrity"); q.add_argument("--archive-root", required=True)
    q.set_defaults(fn=cmd_verify_integrity)

    q = sub.add_parser("render-compare"); q.add_argument("--product", required=True)
    q.add_argument("--registry-root", required=True); q.add_argument("--T", required=True)
    q.set_defaults(fn=cmd_render_compare)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.fn(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
