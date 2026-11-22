"""裸小端 int16 PCM 与 WAV 的读写、规范 JSON 与摘要工具（纯标准库）。

刻意不依赖 numpy：独立设备只凭交付文件就能复播、核对。
所有样本在解码后一律以 Python int（任意精度）承载，窄类型只出现在
落盘字节边界。
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import wave
from typing import Any, Sequence

from .varint import SAMPLE_MAX, SAMPLE_MIN


def pcm_to_bytes(pcm16: Sequence[int]) -> bytes:
    out = bytearray()
    for i, s in enumerate(pcm16):
        s = int(s)
        if not SAMPLE_MIN <= s <= SAMPLE_MAX:
            raise ValueError(f"样本 {i} 超出 int16 范围: {s}")
        out += struct.pack("<h", s)
    return bytes(out)


def read_pcm_file(path: str) -> list[int]:
    data = open(path, "rb").read()
    if len(data) % 2:
        raise ValueError(f"{path}: PCM 字节数必须为偶数")
    return [s for (s,) in struct.iter_unpack("<h", data)]


def pcm_sha256(pcm16: Sequence[int]) -> str:
    return hashlib.sha256(pcm_to_bytes(pcm16)).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_wav(path: str, pcm16: Sequence[int], sample_rate: int) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_to_bytes(pcm16))
    return path


def read_wav(path: str) -> tuple[list[int], int]:
    with wave.open(path, "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise ValueError("仅支持单声道 16bit PCM WAV")
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    return [s for (s,) in struct.iter_unpack("<h", frames)], rate


def canonical_json(obj: Any) -> bytes:
    """键排序、无多余空白的规范 JSON，用作身份/摘要输入。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sha256_json(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def write_json_atomic(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
