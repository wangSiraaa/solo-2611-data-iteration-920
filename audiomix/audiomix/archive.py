"""实际混音数据仓：只消费发布产品与实际素材字节。

- 块按内容寻址：block_key 的输入包含产品身份、块序号、T，以及该块重叠到的
  *全部* clip（完整身份与转换参数）和这些 clip 用到的 *素材摘要*。
  因此“未受影响的块可复用”，而产品更正后任何相关块 key 必然改变，
  新产品不可能误用旧块。
- 块文件为裸 int16 小端 PCM（.pcm），元数据与依赖索引在 SQLite。
- render 与产品文件不可变；旧 render 在编辑器目录移走后仍可独立读取。
- 不读取、也不依赖编辑器私有操作日志。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import wave
from typing import Any

import numpy as np

from .assets import AssetRegistry
from .engine import (
    BLOCK_SAMPLES, OUTPUT_RATE, output_sample_count, render_block,
    validate_product, load_product, product_clip_view,
)
from .rationals import as_r, dump_r


def _canonical_deps(product: dict[str, Any], block_index: int,
                    clip_ids: list[str], asset_ids: list[str]) -> bytes:
    """构造块依赖的规范序列化（排序保证稳定）。

    复用依据只包含：块序号、与该块重叠的*全部* clip 完整身份与转换参数、
    以及这些 clip 引用素材的摘要。刻意不含 product_id —— 时间轴更正产生
    新产品时，未受影响的块（依赖逐项相同）必须能被新 render 复用；而任何
    clip 参数或素材正文变化都会令 key 改变，新产品不可能误用旧块。
    """
    clip_map = {c["clip_id"]: c for c in product["clips"]}
    asset_map = {a["asset_id"]: a for a in product["assets"]}
    deps = {
        "output_rate": product["output_rate"],
        "block_index": block_index,
        "clips": [clip_map[cid] for cid in sorted(clip_ids)],
        "assets": [
            {"asset_id": aid, "digest": asset_map[aid]["digest"]}
            for aid in sorted(asset_ids)
        ],
    }
    return json.dumps(deps, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def block_key_of(product: dict[str, Any], block_index: int,
                 clip_ids: list[str], asset_ids: list[str]) -> str:
    return hashlib.sha256(
        _canonical_deps(product, block_index, clip_ids, asset_ids)).hexdigest()


class ArchiveStore:
    def __init__(self, root: str):
        self.root = root
        self.blocks_dir = os.path.join(root, "blocks")
        os.makedirs(self.blocks_dir, exist_ok=True)
        self.db_path = os.path.join(root, "archive.sqlite3")
        self._db = sqlite3.connect(self.db_path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
        self._db.close()

    def _init_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS blocks(
                block_key   TEXT PRIMARY KEY,
                pcm_path    TEXT NOT NULL,
                n_samples   INTEGER NOT NULL,
                peak        INTEGER NOT NULL,
                clipped     INTEGER NOT NULL,
                clip_ids    TEXT NOT NULL,   -- JSON 数组，按 clip_id 排序
                asset_ids   TEXT NOT NULL,   -- JSON 数组，含素材摘要依赖
                created_at  REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS renders(
                render_id   TEXT PRIMARY KEY,
                product_id  TEXT NOT NULL,
                product_path TEXT NOT NULL,
                T           TEXT NOT NULL,   -- [n,d]
                n_samples   INTEGER NOT NULL,
                n_blocks    INTEGER NOT NULL,
                created_at  REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS render_blocks(
                render_id   TEXT NOT NULL,
                block_index INTEGER NOT NULL,
                block_key   TEXT NOT NULL,
                PRIMARY KEY(render_id, block_index),
                FOREIGN KEY(render_id) REFERENCES renders(render_id)
            );
            CREATE INDEX IF NOT EXISTS idx_rb_key ON render_blocks(block_key);
            """
        )
        self._db.commit()

    # ----------------------------- 渲染 ----------------------------- #
    def render(self, product: dict[str, Any], T: Any,
               pcm_by_asset: dict[str, np.ndarray],
               render_id: str | None = None,
               product_path: str | None = None) -> dict[str, Any]:
        """全量/增量统一入口：逐块渲染，命中内容寻址块即复用。"""
        import time
        validate_product(product, pcm_by_asset)
        T = as_r(T)
        N = output_sample_count(T)
        n_blocks = (N + BLOCK_SAMPLES - 1) // BLOCK_SAMPLES
        if render_id is None:
            render_id = hashlib.sha256(
                (product["product_id"] + json.dumps(dump_r(T))).encode()
            ).hexdigest()[:16]

        existing = self._db.execute(
            "SELECT 1 FROM renders WHERE render_id=?", (render_id,)).fetchone()
        if existing:
            raise ValueError(f"render_id 已存在且不可变: {render_id}")

        reused, rendered = 0, 0
        total_clipped = 0
        clips_view = product_clip_view(product)
        for k in range(n_blocks):
            blk = render_block(product, pcm_by_asset, k, T, clips_view=clips_view)
            key = block_key_of(product, k, blk["clip_ids"], blk["asset_ids"])
            row = self._db.execute(
                "SELECT block_key, clipped FROM blocks WHERE block_key=?", (key,)).fetchone()
            if row is None:
                pcm_path = os.path.join(self.blocks_dir, f"{key}.pcm")
                blk["samples"].astype("<i2").tofile(pcm_path)
                self._db.execute(
                    "INSERT INTO blocks VALUES(?,?,?,?,?,?,?,?)",
                    (key, pcm_path, len(blk["samples"]), blk["peak"],
                     blk["clipped_samples"], json.dumps(blk["clip_ids"]),
                     json.dumps(blk["asset_ids"]), time.time()))
                rendered += 1
            else:
                reused += 1
            total_clipped += self._db.execute(
                "SELECT clipped FROM blocks WHERE block_key=?", (key,)).fetchone()[0]
            self._db.execute(
                "INSERT INTO render_blocks VALUES(?,?,?)", (render_id, k, key))

        self._db.execute(
            "INSERT INTO renders VALUES(?,?,?,?,?,?,?)",
            (render_id, product["product_id"], product_path or "",
             json.dumps(dump_r(T)), N, n_blocks, time.time()))
        self._db.commit()
        return {
            "render_id": render_id,
            "product_id": product["product_id"],
            "T": dump_r(T),
            "n_samples": N,
            "n_blocks": n_blocks,
            "blocks_rendered": rendered,
            "blocks_reused": reused,
            "clipped_samples": total_clipped,
        }

    def render_from_files(self, product_path: str, registry: AssetRegistry,
                          T: Any, render_id: str | None = None) -> dict[str, Any]:
        """外部/本地产品文件 + 实际素材字节 -> 渲染（完全不碰编辑器日志）。"""
        product = load_product(product_path)
        pcm_by_asset = {aid: registry.load_pcm(aid) for aid in
                        (a["asset_id"] for a in product["assets"])}
        return self.render(product, T, pcm_by_asset, render_id, product_path)

    # ----------------------------- 读取 ----------------------------- #
    def _load_block_pcm(self, block_key: str) -> np.ndarray:
        row = self._db.execute(
            "SELECT pcm_path, n_samples FROM blocks WHERE block_key=?",
            (block_key,)).fetchone()
        if row is None:
            raise KeyError(f"块缺失: {block_key}")
        return np.fromfile(row[0], dtype="<i2", count=row[1]).astype(np.int16)

    def read_samples(self, render_id: str, start: int, end: int) -> np.ndarray:
        """按输出样本下标范围 [start,end) 读取（跨块拼接）。"""
        meta = self.render_meta(render_id)
        start = max(0, start)
        end = min(meta["n_samples"], end)
        if start >= end:
            return np.empty(0, dtype=np.int16)
        rows = self._db.execute(
            "SELECT block_index, block_key FROM render_blocks WHERE render_id=? "
            "ORDER BY block_index", (render_id,)).fetchall()
        pieces = []
        for bidx, key in rows:
            b0, b1 = bidx * BLOCK_SAMPLES, (bidx + 1) * BLOCK_SAMPLES
            if b1 <= start or b0 >= end:
                continue
            pcm = self._load_block_pcm(key)
            pieces.append(pcm[max(0, start - b0): min(len(pcm), end - b0)])
        return np.concatenate(pieces).astype(np.int16) if pieces else np.empty(0, np.int16)

    def read_trace(self, render_id: str, start: int, end: int) -> list[dict[str, Any]]:
        """范围读取 + 逐块追溯：峰值、削顶数、clip/asset 来源与复用依据。"""
        meta = self.render_meta(render_id)
        start = max(0, start)
        end = min(meta["n_samples"], end)
        rows = self._db.execute(
            "SELECT block_index, block_key FROM render_blocks WHERE render_id=? "
            "ORDER BY block_index", (render_id,)).fetchall()
        trace = []
        for bidx, key in rows:
            b0, b1 = bidx * BLOCK_SAMPLES, (bidx + 1) * BLOCK_SAMPLES
            if b1 <= start or b0 >= end:
                continue
            brow = self._db.execute(
                "SELECT peak, clipped, clip_ids, asset_ids FROM blocks WHERE block_key=?",
                (key,)).fetchone()
            trace.append({
                "block_index": bidx,
                "sample_range": [max(b0, start), min(b1, end)],
                "block_key": key,
                "peak": brow[0],
                "clipped_samples": brow[1],
                "clip_ids": json.loads(brow[2]),
                "asset_ids": json.loads(brow[3]),
            })
        return trace

    def render_meta(self, render_id: str) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT render_id, product_id, product_path, T, n_samples, n_blocks, created_at "
            "FROM renders WHERE render_id=?", (render_id,)).fetchone()
        if row is None:
            raise KeyError(f"未知 render: {render_id}")
        return {
            "render_id": row[0], "product_id": row[1], "product_path": row[2],
            "T": json.loads(row[3]), "n_samples": row[4], "n_blocks": row[5],
            "created_at": row[6],
        }

    def list_renders(self) -> list[dict[str, Any]]:
        return [self.render_meta(r[0]) for r in self._db.execute(
            "SELECT render_id FROM renders ORDER BY created_at").fetchall()]

    def full_pcm(self, render_id: str) -> np.ndarray:
        meta = self.render_meta(render_id)
        return self.read_samples(render_id, 0, meta["n_samples"])

    def export_wav(self, render_id: str, path: str) -> str:
        pcm = self.full_pcm(render_id)
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(OUTPUT_RATE)
            wf.writeframes(pcm.tobytes())
        return path

    # ----------------------------- 维护 ----------------------------- #
    def block_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]

    def verify_block_pcm_integrity(self) -> None:
        """校验所有块 PCM 文件可解析且样本数与索引一致（防静默损坏/替换）。"""
        for key, path, n in self._db.execute(
                "SELECT block_key, pcm_path, n_samples FROM blocks"):
            if not os.path.exists(path):
                raise RuntimeError(f"块 {key} 文件缺失: {path}")
            data = open(path, "rb").read()
            if len(data) != n * 2:
                raise RuntimeError(f"块 {key} 字节长度与索引样本数不符")
            np.frombuffer(data, dtype="<i2")  # 可解析性检查
