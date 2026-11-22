"""旧 audiomix 数据仓 -> tourbox 来源段适配器。

只消费 audiomix 的不可变 render（块仓 + 发布产品），不读取编辑器私有日志；
与 audiomix/archive.py 的离线原则一致。本模块是 tourbox 中唯一允许依赖
numpy 的地方：核心封版/解码/核对/计划全部纯标准库。
"""
from __future__ import annotations

from typing import Any

from .master import SourceSegment


def render_pcm_from_archive(archive_store: Any, render_id: str) -> list[int]:
    """从 audiomix ArchiveStore 取整条 render 的已渲染 PCM（int16 -> Python int）。"""
    pcm = archive_store.full_pcm(render_id)  # numpy int16 一维数组
    return [int(s) for s in pcm]


def render_meta_from_archive(archive_store: Any, render_id: str) -> dict[str, Any]:
    meta = archive_store.render_meta(render_id)
    pcm = render_pcm_from_archive(archive_store, render_id)
    return {**meta, "_pcm": pcm}


def segment_from_render(render_id: str, lo: int, hi: int,
                        clip_id: str, occurrence: int, license_id: str,
                        render_meta: dict[str, Any],
                        render_wav: str | None = None) -> SourceSegment:
    """一条旧 render 的采样范围 -> tourbox 编带来源段。"""
    return SourceSegment(
        render_id=render_id, lo=lo, hi=hi,
        clip_id=clip_id, occurrence=occurrence,
        license_id=license_id, render_meta=render_meta,
        render_wav=render_wav)


def segments_from_concat(specs: list[dict[str, Any]]) -> list[SourceSegment]:
    """把两段（或多段）旧 render 按明确顺序拼成一条新母带的来源段列表。

    每项：{render_id, lo, hi, clip_id, occurrence, license_id,
           render_meta, render_wav?}，顺序即拼接顺序。
    """
    return [segment_from_render(
        s["render_id"], int(s["lo"]), int(s["hi"]),
        s["clip_id"], int(s["occurrence"]), s["license_id"],
        s["render_meta"], s.get("render_wav")) for s in specs]
