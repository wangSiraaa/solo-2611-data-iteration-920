"""差分母带（tourbox master, .tbm）：两类记录的流式编解码。

合同语义：
- 差分记录（kind=0）携带有符号整数差值，相对*前一个已解码样本*还原：
      s[i] = s[i-1] + delta
- 复位记录（kind=1）携带本处绝对样本值，不依赖上文即可重新起播：
      s[i] = abs
- 差值可达 ±65535。所有加减都在 Python 任意精度整数上完成，
  禁止先压进 int16 再相减/相加（那会发生回绕，详见 tests/test_wide_int.py）。
- 只有编带师批准的片头（含起点 0）允许写复位记录；编码者必须显式传入
  复位位置集合，编码器不自行决定任何复位点，母带一旦封版不可改写。
"""
from __future__ import annotations

import hashlib
from typing import BinaryIO, Iterable, Sequence

from .varint import (
    DELTA_MAX, DELTA_MIN, KIND_DELTA, KIND_RESET, SAMPLE_MAX, SAMPLE_MIN,
    decode_signed, encode_signed,
)

MAGIC = b"TBM1"  # 差分母带流头：纯记录流之前的 4 字节魔数


class StreamError(ValueError):
    pass


def encode_master(pcm16: Sequence[int], reset_positions: Iterable[int]) -> bytes:
    """把已渲染 int16 PCM 编码为差分母带字节。

    ``reset_positions`` 必须包含 0，且只能是落在 [0, n) 内的批准片头；
    编码器不会自作主张增加复位点。
    """
    n = len(pcm16)
    resets = sorted(set(reset_positions))
    if not resets or resets[0] != 0:
        raise StreamError("起点 0 必须是复位点")
    if resets[-1] >= n:
        raise StreamError("复位位置越出母带范围")
    reset_set = set(resets)

    out = bytearray(MAGIC)
    prev = 0  # 仅用于非复位处的差分；0 处必须是复位，此值不会被使用
    for i, sample in enumerate(pcm16):
        sample = int(sample)
        if not SAMPLE_MIN <= sample <= SAMPLE_MAX:
            raise StreamError(f"样本 {i} 超出 int16 范围: {sample}")
        if i in reset_set:
            out += encode_signed(KIND_RESET, sample)
        else:
            delta = sample - prev  # 宽整数减法，绝不先转 int16
            if not DELTA_MIN <= delta <= DELTA_MAX:
                # 两个 int16 样本之差理论上落在 [-65535,65535]；越界说明输入被污染
                raise StreamError(f"位置 {i} 差值越界: {delta}")
            out += encode_signed(KIND_DELTA, delta)
        prev = sample
    return bytes(out)


class StreamingDecoder:
    """逐记录流式解码器，状态只保留前一个已解码样本（宽整数）。

    典型巡展设备用法：从最近的不晚于目标的复位点开始喂字节流，
    到目标样本后丢弃前 t-r 个已还原样本再发声，全程不向编辑器索取前值。
    """

    def __init__(self) -> None:
        self.prev: int | None = None  # None = 尚无上文，遇到差分记录即非法
        self.index = 0                # 已还原样本数（本解码器实例的本地序号）
        self.reset_positions: list[int] = []

    def feed_record(self, kind: int, value: int) -> int:
        if kind == KIND_RESET:
            if not SAMPLE_MIN <= value <= SAMPLE_MAX:
                raise StreamError(f"复位绝对值越界: {value}")
            sample = value
            self.reset_positions.append(self.index)
        else:
            if self.prev is None:
                raise StreamError("无上文时遇到差分记录，无法起播")
            if not DELTA_MIN <= value <= DELTA_MAX:
                raise StreamError(f"差值越界: {value}")
            sample = self.prev + value  # 宽整数加法
            if not SAMPLE_MIN <= sample <= SAMPLE_MAX:
                raise StreamError(f"还原样本越界，流已损坏: {sample}")
        self.prev = sample
        self.index += 1
        return sample


def decode_master(data: bytes) -> tuple[list[int], list[int]]:
    """一次性解码（测试/校对用），返回 (PCM 样本, 实际复位位置)。"""
    import io
    return _decode_stream(io.BytesIO(data), expect_magic=True)


def decode_stream(stream: BinaryIO) -> tuple[list[int], list[int]]:
    """从二进制流解码，先消费魔数，再逐条读记录直到 EOF。"""
    return _decode_stream(stream, expect_magic=True)


class _PushbackReader:
    """允许回推 1 字节的流式读取器：用于先探 EOF 再把 kind 字节交回。"""

    def __init__(self, stream: BinaryIO):
        self._stream = stream
        self._pushback: bytes = b""

    def read(self, n: int) -> bytes:
        if self._pushback:
            chunk, self._pushback = self._pushback, b""
            if n > 1:
                chunk += self._stream.read(n - 1)
            return chunk
        return self._stream.read(n)

    def push_back(self, byte: int) -> None:
        self._pushback = bytes([byte])


def _decode_stream(stream: BinaryIO, expect_magic: bool) -> tuple[list[int], list[int]]:
    if expect_magic:
        magic = stream.read(4)
        if magic != MAGIC:
            raise StreamError("魔数不匹配，不是受信差分母带")
    reader = _PushbackReader(stream)
    dec = StreamingDecoder()
    samples: list[int] = []
    while True:
        head = reader.read(1)  # 先探 EOF，再把 kind 字节交回统一 varint 读取器
        if not head:
            break
        reader.push_back(head[0])
        kind, value = decode_signed(reader)
        samples.append(dec.feed_record(kind, value))
    return samples, dec.reset_positions


def tbm_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
