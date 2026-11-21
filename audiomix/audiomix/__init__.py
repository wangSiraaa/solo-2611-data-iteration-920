"""audiomix：多采样率剪辑时间轴与真实混音档案。"""
from .assets import AssetRegistry
from .timeline import TimelineEditor, Clip, ValidationError
from .engine import (
    render_reference, render_block, render_full_vectorized,
    validate_product, output_sample_count, BLOCK_SAMPLES, OUTPUT_RATE,
)
from .archive import ArchiveStore, block_key_of

__all__ = [
    "AssetRegistry", "TimelineEditor", "Clip", "ValidationError",
    "render_reference", "render_block", "render_full_vectorized",
    "validate_product", "output_sample_count", "BLOCK_SAMPLES", "OUTPUT_RATE",
    "ArchiveStore", "block_key_of",
]
