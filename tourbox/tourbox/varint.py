"""LEB128 无符号/有符号（zigzag）变长整数，纯标准库、无任何第三方依赖。

设备合同只认两类记录（差分 / 复位），二者统一编码为：

    kind:   无符号 varint，0=差分记录，1=复位记录
    value:  zigzag 有符号 varint
        差分记录：相对前一个已解码样本的差值，合法域 [-65535, 65535]
        复位记录：本处绝对样本值，合法域 [-32768, 32767]

读写都在 Python 任意精度整数上进行，解码侧永不先压进 16 位，
差值 ±65535 不会因为窄类型回绕而损坏。
"""
from __future__ import annotations

from typing import BinaryIO

# 设备合同显式给出的两条数值边界
DELTA_MIN, DELTA_MAX = -65535, 65535
SAMPLE_MIN, SAMPLE_MAX = -32768, 32767

KIND_DELTA, KIND_RESET = 0, 1


def zigzag_encode(n: int) -> int:
    if n < 0:
        return (-n) * 2 - 1
    return n * 2


def zigzag_decode(u: int) -> int:
    if u & 1:
        return -((u + 1) // 2)
    return u // 2


def write_uvarint(buf: bytearray, value: int) -> None:
    if value < 0:
        raise ValueError("uvarint 不能为负数")
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            return


def read_uvarint(stream: BinaryIO) -> int:
    """流式读取：逐字节取，直到终止字节；不要求一次性持有整条母带。"""
    result = 0
    shift = 0
    while True:
        chunk = stream.read(1)
        if not chunk:
            raise EOFError("uvarint 提前结束")
        b = chunk[0]
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result
        shift += 7
        if shift > 70:  # 合同内最大差值 zigzag 后远小于此，越界即视为损坏
            raise ValueError("uvarint 超出受信长度")


def encode_signed(kind: int, value: int) -> bytes:
    out = bytearray()
    write_uvarint(out, kind)
    write_uvarint(out, zigzag_encode(value))
    return bytes(out)


def decode_signed(stream: BinaryIO) -> tuple[int, int]:
    kind = read_uvarint(stream)
    if kind not in (KIND_DELTA, KIND_RESET):
        raise ValueError(f"非法记录类型: {kind}")
    return kind, zigzag_decode(read_uvarint(stream))
