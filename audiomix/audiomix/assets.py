"""PCM WAV 素材生成与素材登记仓。

素材约束：单声道、有符号 16 位 PCM，采样率仅允许 8000/16000/48000。
asset_id 对正文（原始 PCM 字节）做 SHA-256 截断得到，天然稳定且与
摘要、样本数绑定；同一字节内容永远是同一 asset_id。
"""
from __future__ import annotations

import hashlib
import json
import os
import wave
from dataclasses import dataclass, asdict
from typing import Iterable

import numpy as np

ALLOWED_RATES = (8000, 16000, 48000)


def asset_id_of(pcm: bytes) -> str:
    """素材身份 = 正文 PCM 字节的 SHA-256（十六进制）。"""
    return hashlib.sha256(pcm).hexdigest()


def digest_of(pcm: bytes) -> str:
    """发布产品中记录的素材摘要，同样绑定正文（完整 SHA-256）。"""
    return hashlib.sha256(pcm).hexdigest()


def write_wav(path: str, pcm16: np.ndarray, sample_rate: int) -> None:
    """写标准 16bit 单声道 WAV。"""
    if sample_rate not in ALLOWED_RATES:
        raise ValueError(f"采样率必须属于 {ALLOWED_RATES}")
    if pcm16.dtype != np.int16 or pcm16.ndim != 1:
        raise ValueError("需要一维 int16 数组")
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())


def read_wav(path: str) -> tuple[np.ndarray, int]:
    """读单声道 16bit PCM WAV，返回 (int16 样本, 采样率)。"""
    with wave.open(path, "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise ValueError("仅支持单声道 16bit PCM WAV")
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    pcm = np.frombuffer(frames, dtype="<i2").astype(np.int16)
    return pcm, rate


# ----------------------------- 波形生成器 ----------------------------- #

def tone(rate: int, freq: float, seconds: float, amp: float = 0.5,
         phase: float = 0.0) -> np.ndarray:
    """正弦波，amp 为满量程比例。"""
    n = int(round(rate * seconds))
    t = np.arange(n, dtype=np.float64) / rate
    x = amp * 32767.0 * np.sin(2 * np.pi * freq * t + phase)
    return np.rint(x).astype(np.int16)


def ramp(rate: int, n: int, start: int = -30000, stop: int = 30000) -> np.ndarray:
    """线性斜坡，便于手算插值。"""
    if n <= 0:
        raise ValueError("样本数必须为正")
    if n == 1:
        return np.array([start], dtype=np.int16)
    return np.rint(np.linspace(start, stop, n)).astype(np.int16)


def sequence(values: Iterable[int]) -> np.ndarray:
    """由短整数序列直接造素材，供手算案例使用。"""
    arr = np.asarray(list(values), dtype=np.int64)
    if np.any(arr < -32768) or np.any(arr > 32767):
        raise ValueError("超出 int16 范围")
    return arr.astype(np.int16)


def silence(rate: int, seconds: float) -> np.ndarray:
    return np.zeros(int(round(rate * seconds)), dtype=np.int16)


def lowpass(pcm16: np.ndarray, cutoff_hz: float, sample_rate: int,
            order: int = 4) -> np.ndarray:
    """用 SciPy 巴特沃斯低通滤波生成素材（返回新的 int16 素材，不改输入）。"""
    from scipy.signal import butter, sosfilt
    sos = butter(order, cutoff_hz, btype="low", fs=sample_rate, output="sos")
    y = sosfilt(sos, pcm16.astype(np.float64))
    return np.rint(np.clip(y, -32768, 32767)).astype(np.int16)


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    path: str
    sample_rate: int
    sample_count: int
    digest: str          # 正文 SHA-256（== asset_id，单列出来语义更清晰）
    note: str

    def to_json(self) -> dict:
        return asdict(self)


class AssetRegistry:
    """素材登记仓：asset_id -> 登记项；持久化为 registry.json。"""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.index_path = os.path.join(root, "registry.json")
        self._records: dict[str, AssetRecord] = {}
        if os.path.exists(self.index_path):
            for item in json.load(open(self.index_path, encoding="utf-8")):
                rec = AssetRecord(**item)
                self._records[rec.asset_id] = rec

    # ---- 登记 / 查询 ---- #
    def register(self, pcm16: np.ndarray, sample_rate: int, note: str = "",
                 filename: str | None = None) -> AssetRecord:
        pcm_bytes = np.asarray(pcm16, dtype=np.int16).tobytes()
        aid = asset_id_of(pcm_bytes)
        if aid in self._records:
            return self._records[aid]  # 同正文幂等
        fname = filename or f"{aid[:12]}_{sample_rate}hz.wav"
        path = os.path.join(self.root, fname)
        write_wav(path, np.frombuffer(pcm_bytes, dtype=np.int16), sample_rate)
        rec = AssetRecord(
            asset_id=aid,
            path=path,
            sample_rate=sample_rate,
            sample_count=len(pcm16),
            digest=digest_of(pcm_bytes),
            note=note,
        )
        self._records[aid] = rec
        self._flush()
        return rec

    def get(self, asset_id: str) -> AssetRecord:
        if asset_id not in self._records:
            raise KeyError(f"未知素材: {asset_id}")
        return self._records[asset_id]

    def contains(self, asset_id: str) -> bool:
        return asset_id in self._records

    def all(self) -> list[AssetRecord]:
        return sorted(self._records.values(), key=lambda r: r.asset_id)

    def load_pcm(self, asset_id: str) -> np.ndarray:
        rec = self.get(asset_id)
        pcm, rate = read_wav(rec.path)
        if rate != rec.sample_rate or len(pcm) != rec.sample_count:
            raise RuntimeError(f"素材 {asset_id} 磁盘正文与登记不符")
        if digest_of(pcm.tobytes()) != rec.digest:
            raise RuntimeError(f"素材 {asset_id} 摘要校验失败")
        return pcm

    def _flush(self) -> None:
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump([r.to_json() for r in self.records_sorted()], fh,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, self.index_path)

    def records_sorted(self) -> list[AssetRecord]:
        return sorted(self._records.values(), key=lambda r: r.asset_id)
