"""可独立编辑的音频时间轴服务。

设计要点：
- clip_id 与素材身份完全独立；同一 asset_id 可被任意多 clip 反复引用。
- 一切时间/速度/增益/淡化都是精确有理数（Fraction）。
- 插入时做整笔校验：未知素材、源区间越界、参数非法、淡化超长 -> 整笔拒绝，
  时间轴状态不变；多个 clip 允许重叠。
- 普通编辑客户不渲染即可查询“某输出时刻有哪些片段、各自对应源的哪个实数样本”。
- publish() 产出不可变发布产品（自包含 JSON），编辑器私有操作日志不进入产品。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .assets import AssetRegistry
from .rationals import R, as_r, dump_r, load_r  # noqa: F401  (R 对外再导出)

OUTPUT_RATE = 16000


@dataclass(frozen=True)
class Clip:
    clip_id: str
    asset_id: str
    a: int                 # 源样本半开区间 [a, b)
    b: int
    start: Fraction        # 输出起始时刻（秒，有理数）
    speed: Fraction        # 正有理播放速度
    gain: Fraction         # 有理增益
    fade_in: Fraction      # 淡入时长（秒），>= 0
    fade_out: Fraction     # 淡出时长（秒），>= 0

    def source_rate(self, registry: AssetRegistry) -> int:
        return registry.get(self.asset_id).sample_rate

    def duration(self, registry: AssetRegistry) -> Fraction:
        """(b-a) / (源采样率 * 速度)。"""
        rate = self.source_rate(registry)
        return Fraction(self.b - self.a, rate) / self.speed

    def end(self, registry: AssetRegistry) -> Fraction:
        return self.start + self.duration(registry)

    def is_active_at(self, t: Fraction, registry: AssetRegistry) -> bool:
        return self.start <= t < self.end(registry)

    def source_position(self, t: Fraction, registry: AssetRegistry) -> Fraction:
        """输出时刻 t 映射到源的实数样本位置（精确有理数）。"""
        rate = self.source_rate(registry)
        # 源位置 = a + (t - start) * rate * speed
        return self.a + (t - self.start) * rate * self.speed


def _clip_from_json(obj: dict[str, Any]) -> Clip:
    return Clip(
        clip_id=obj["clip_id"],
        asset_id=obj["asset_id"],
        a=int(obj["a"]),
        b=int(obj["b"]),
        start=load_r(obj["start"]),
        speed=load_r(obj["speed"]),
        gain=load_r(obj["gain"]),
        fade_in=load_r(obj["fade_in"]),
        fade_out=load_r(obj["fade_out"]),
    )


class ValidationError(ValueError):
    """整笔拒绝类错误。"""


class TimelineEditor:
    """时间轴编辑器（带私有操作日志，日志绝不进入发布产品）。"""

    def __init__(self, root: str, registry: AssetRegistry, name: str = "main"):
        self.root = root
        self.registry = registry
        self.name = name
        os.makedirs(root, exist_ok=True)
        self.state_path = os.path.join(root, f"timeline_{name}.json")
        self.log_path = os.path.join(root, f"timeline_{name}.oplog.jsonl")
        self.clips: dict[str, Clip] = {}
        self._seq = 0
        if os.path.exists(self.state_path):
            data = json.load(open(self.state_path, encoding="utf-8"))
            self._seq = data.get("seq", 0)
            for cobj in data["clips"]:
                clip = _clip_from_json(cobj)
                self.clips[clip.clip_id] = clip

    # ------------------------- 内部工具 ------------------------- #
    def _log(self, op: str, payload: Any) -> None:
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"seq": self._seq, "op": op, "payload": payload},
                                ensure_ascii=False) + "\n")

    def _persist(self) -> None:
        data = {
            "seq": self._seq,
            "clips": [self._clip_public_json(c) for c in
                      sorted(self.clips.values(), key=lambda c: c.clip_id)],
        }
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    def _clip_public_json(self, clip: Clip) -> dict[str, Any]:
        d = {
            "clip_id": clip.clip_id,
            "asset_id": clip.asset_id,
            "a": clip.a,
            "b": clip.b,
            "start": dump_r(clip.start),
            "speed": dump_r(clip.speed),
            "gain": dump_r(clip.gain),
            "fade_in": dump_r(clip.fade_in),
            "fade_out": dump_r(clip.fade_out),
        }
        return d

    def _validate_clip(self, spec: dict[str, Any], known_id: str | None = None) -> Clip:
        """把外部规格校验成 Clip；任何问题抛 ValidationError（整笔拒绝）。"""
        clip_id = str(spec["clip_id"])
        if known_id is None and clip_id in self.clips:
            raise ValidationError(f"clip_id 已存在: {clip_id}")
        asset_id = str(spec["asset_id"])
        if not self.registry.contains(asset_id):
            raise ValidationError(f"未知素材，整笔拒绝: {asset_id}")
        rec = self.registry.get(asset_id)

        a = int(spec["a"])
        b = int(spec["b"])
        if not (0 <= a < b <= rec.sample_count):
            raise ValidationError(
                f"源区间 [{a},{b}) 越界（素材 {asset_id} 仅 {rec.sample_count} 样本），整笔拒绝")

        start = as_r(spec.get("start", 0))
        if start < 0:
            raise ValidationError("输出起始时刻不能为负")
        speed = as_r(spec.get("speed", 1))
        if speed <= 0:
            raise ValidationError("播放速度必须为正有理数")
        gain = as_r(spec.get("gain", 1))
        fade_in = as_r(spec.get("fade_in", 0))
        fade_out = as_r(spec.get("fade_out", 0))
        if fade_in < 0 or fade_out < 0:
            raise ValidationError("淡化时长必须非负")

        duration = Fraction(b - a, rec.sample_rate * speed.numerator) * speed.denominator
        if fade_in + fade_out > duration:
            raise ValidationError(
                f"淡化之和 {fade_in + fade_out} 超过 clip 输出时长 {duration}，整笔拒绝")
        return Clip(clip_id, asset_id, a, b, start, speed, gain, fade_in, fade_out)

    # ------------------------- 编辑操作 ------------------------- #
    def insert(self, specs: list[dict[str, Any]] | dict[str, Any]) -> list[str]:
        """插入一个或一批 clip；任一非法则整批拒绝、状态不变。"""
        if isinstance(specs, dict):
            specs = [specs]
        # 先全部校验（不动状态）
        prepared = [self._validate_clip(s) for s in specs]
        ids = [c.clip_id for c in prepared]
        if len(set(ids)) != len(ids):
            raise ValidationError("本批内 clip_id 重复，整笔拒绝")
        for clip in prepared:
            self.clips[clip.clip_id] = clip
        self._seq += 1
        self._log("insert", [self._clip_public_json(c) for c in prepared])
        self._persist()
        return ids

    def move(self, clip_id: str, new_start: Any) -> None:
        """移动 clip 到新的输出起始时刻；clip 身份与其它参数不变。"""
        if clip_id not in self.clips:
            raise ValidationError(f"未知 clip: {clip_id}")
        new_start = as_r(new_start)
        if new_start < 0:
            raise ValidationError("输出起始时刻不能为负")
        old = self.clips[clip_id]
        moved = Clip(clip_id, old.asset_id, old.a, old.b, new_start,
                     old.speed, old.gain, old.fade_in, old.fade_out)
        self.clips[clip_id] = moved
        self._seq += 1
        self._log("move", {"clip_id": clip_id, "new_start": dump_r(new_start)})
        self._persist()

    def modify(self, clip_id: str, changes: dict[str, Any]) -> None:
        """修改 clip 参数（合并修改后整体重新校验，非法则保持原状）。"""
        if clip_id not in self.clips:
            raise ValidationError(f"未知 clip: {clip_id}")
        old = self.clips[clip_id]
        merged = {
            "clip_id": clip_id,
            "asset_id": changes.get("asset_id", old.asset_id),
            "a": changes.get("a", old.a),
            "b": changes.get("b", old.b),
            "start": changes.get("start", old.start),
            "speed": changes.get("speed", old.speed),
            "gain": changes.get("gain", old.gain),
            "fade_in": changes.get("fade_in", old.fade_in),
            "fade_out": changes.get("fade_out", old.fade_out),
        }
        updated = self._validate_clip(merged, known_id=clip_id)
        self.clips[clip_id] = updated
        self._seq += 1
        self._log("modify", {"clip_id": clip_id, "changes": changes})
        self._persist()

    def delete(self, clip_id: str) -> None:
        if clip_id not in self.clips:
            raise ValidationError(f"未知 clip: {clip_id}")
        del self.clips[clip_id]
        self._seq += 1
        self._log("delete", {"clip_id": clip_id})
        self._persist()

    # ------------------------- 查询（不渲染） ------------------------- #
    def query_interval(self, lo: Any, hi: Any) -> list[dict[str, Any]]:
        """返回与输出区间 [lo, hi) 有交集的全部 clip（重叠 clip 全部返回）。"""
        lo, hi = as_r(lo), as_r(hi)
        out = []
        for clip in self.clips.values():
            if clip.start < hi and clip.end(self.registry) > lo:
                out.append(self._clip_public_json(clip) | {
                    "end": dump_r(clip.end(self.registry)),
                    "duration": dump_r(clip.duration(self.registry)),
                })
        return sorted(out, key=lambda d: (d["start"], d["clip_id"]))

    def query_at(self, t: Any) -> list[dict[str, Any]]:
        """精确查询输出时刻 t 上有哪些片段及各自对应源的实数样本位置。"""
        t = as_r(t)
        hits = []
        for clip in self.clips.values():
            if clip.is_active_at(t, self.registry):
                pos = clip.source_position(t, self.registry)
                hits.append({
                    "clip_id": clip.clip_id,
                    "asset_id": clip.asset_id,
                    "source_position": dump_r(pos),
                    "source_position_decimal": float(pos),
                    "clip_start": dump_r(clip.start),
                    "gain": dump_r(clip.gain),
                })
        return sorted(hits, key=lambda d: d["clip_id"])

    # ------------------------- 发布 ------------------------- #
    def publish(self) -> dict[str, Any]:
        """生成不可变发布产品：固定完整 clip 身份、素材摘要与全部转换参数。"""
        asset_ids = sorted({c.asset_id for c in self.clips.values()})
        assets = []
        for aid in asset_ids:
            rec = self.registry.get(aid)
            assets.append({
                "asset_id": rec.asset_id,
                "digest": rec.digest,
                "sample_rate": rec.sample_rate,
                "sample_count": rec.sample_count,
            })
        clips = []
        for clip in sorted(self.clips.values(), key=lambda c: c.clip_id):
            clips.append(self._clip_public_json(clip) | {
                "duration": dump_r(clip.duration(self.registry)),
            })
        product = {
            "format": "audiomix.timeline/1",
            "output_rate": OUTPUT_RATE,
            "published_at": time.time(),
            "editor_seq": self._seq,
            "assets": assets,
            "clips": clips,
        }
        product["product_id"] = self.canonical_product_id(product)
        return product

    def save_product(self, product: dict[str, Any], path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(product, fh, ensure_ascii=False, indent=2)
        return path

    @staticmethod
    def canonical_product_id(product: dict[str, Any]) -> str:
        """产品身份只由 clip 身份/参数 + 素材摘要决定（不含时间戳）。"""
        canonical = json.dumps(
            {"assets": product["assets"], "clips": product["clips"],
             "output_rate": product["output_rate"]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
