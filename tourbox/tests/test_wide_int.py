"""差分流宽整数与两类记录语义测试（不依赖 numpy，纯标准库）。"""
import io

from tourbox.tbm import (
    MAGIC, StreamingDecoder, decode_master, decode_stream, encode_master,
)
from tourbox.varint import (
    DELTA_MAX, DELTA_MIN, KIND_DELTA, KIND_RESET, decode_signed,
    encode_signed, read_uvarint, zigzag_decode, zigzag_encode,
)


def test_zigzag_roundtrip():
    for v in [0, 1, -1, 2, -2, 65535, -65535, 32767, -32768, 1 << 40, -(1 << 40)]:
        assert zigzag_decode(zigzag_encode(v)) == v
    # 合同边界的具体编码值固定，防止格式漂移
    assert zigzag_encode(0) == 0
    assert zigzag_encode(-1) == 1
    assert zigzag_encode(65535) == 131070
    assert zigzag_encode(-65535) == 131069
    print("[OK] zigzag 往返与边界编码固定")


def test_varint_streaming():
    # 多字节 varint 必须能从只支持逐字节读的流里还原
    for v in [0, 1, 127, 128, 16383, 16384, 131070]:
        buf = bytearray()
        from tourbox.varint import write_uvarint
        write_uvarint(buf, v)
        assert read_uvarint(io.BytesIO(bytes(buf))) == v
    try:
        read_uvarint(io.BytesIO(b""))
        raise AssertionError("空流应报 EOF")
    except EOFError:
        pass
    print("[OK] varint 流式读取（含截断检测）")


def test_record_kinds():
    assert encode_signed(KIND_DELTA, 65535)[0] == KIND_DELTA
    assert encode_signed(KIND_RESET, -32768)[0] == KIND_RESET
    kind, val = decode_signed(io.BytesIO(encode_signed(KIND_DELTA, -65535)))
    assert (kind, val) == (KIND_DELTA, -65535)
    print("[OK] 两类记录 kind/value 编解码")


def test_wide_delta_no_int16_wrap():
    """关键合同：32767 -> -32768 的差是 -65535，绝不能按 16 位回绕成 1。"""
    pcm = [32767, -32768]
    data = encode_master(pcm, [0])
    dec, resets = decode_master(data)
    assert dec == pcm and resets == [0]
    # 若错误地先压进 int16 再减：(-32768 - 32767) & 0xffff -> 1，必须不发生
    assert pcm[1] - pcm[0] == -65535
    # 反向同样检查 +65535
    pcm2 = [-32768, 32767]
    dec2, _ = decode_master(encode_master(pcm2, [0]))
    assert dec2 == pcm2 and pcm2[1] - pcm2[0] == 65535
    print("[OK] 差值 ±65535 宽整数域还原，无 int16 回绕")


def test_reset_restart_without_context():
    pcm = [10, 20, 30, 999, 1009, 1019]
    data = encode_master(pcm, [0, 3])
    dec, resets = decode_master(data)
    assert dec == pcm and resets == [0, 3]
    # 模拟设备只拿到位置 3 起的子流：先解析全部记录，再把 3 之后的记录
    # 喂给一个*全新*解码器（无任何上文），复位记录必须能独立起播
    reader = io.BytesIO(data)
    assert reader.read(4) == MAGIC
    records = []
    while True:
        head = reader.read(1)
        if not head:
            break
        rest = io.BytesIO(head + reader.read())
        records.append(decode_signed(rest))
        reader = rest
    dec_dev = StreamingDecoder()
    tail = [dec_dev.feed_record(k, v) for k, v in records[3:]]
    assert tail == pcm[3:] and dec_dev.reset_positions == [0]
    print("[OK] 复位点脱离上文独立起播，丢弃预读样本后发声正确")


def test_delta_without_context_rejected():
    dec = StreamingDecoder()
    try:
        dec.feed_record(KIND_DELTA, 1)
        raise AssertionError("无上文差分必须拒绝")
    except ValueError as e:
        assert "无上文" in str(e)
    # 越界差值/复位值也要拒绝
    dec.feed_record(KIND_RESET, 0)
    for bad in (DELTA_MIN - 1, DELTA_MAX + 1):
        try:
            dec.feed_record(KIND_DELTA, bad)
            raise AssertionError(f"越界差值 {bad} 必须拒绝")
        except ValueError:
            pass
    try:
        dec.feed_record(KIND_RESET, 32768)
        raise AssertionError("越界复位值必须拒绝")
    except ValueError:
        pass
    print("[OK] 无上文差分与越界值均被拒绝")


def test_stream_decode_equals_bytes_decode():
    pcm = [0, -1, 32767, -32768, 123, -45, 0, 0, 7]
    data = encode_master(pcm, [0, 4])
    b1, r1 = decode_master(data)
    b2, r2 = decode_stream(io.BytesIO(data))
    assert b1 == b2 == pcm and r1 == r2 == [0, 4]
    # 魔数损坏必须拒绝
    try:
        decode_master(b"XXXX" + data[4:])
        raise AssertionError("坏魔数应拒绝")
    except ValueError:
        pass
    print("[OK] 流式/整包解码等价，魔数校验生效")


def test_encoder_requires_origin_reset():
    try:
        encode_master([1, 2, 3], [1])
        raise AssertionError("起点 0 必须复位")
    except ValueError:
        pass
    try:
        encode_master([1, 2, 3], [0, 5])
        raise AssertionError("越界复位点应拒绝")
    except ValueError:
        pass
    print("[OK] 编码器强制起点复位且拒绝越界复位点")


if __name__ == "__main__":
    test_zigzag_roundtrip()
    test_varint_streaming()
    test_record_kinds()
    test_wide_delta_no_int16_wrap()
    test_reset_restart_without_context()
    test_delta_without_context_rejected()
    test_stream_decode_equals_bytes_decode()
    test_encoder_requires_origin_reset()
    print("差分流测试全部通过")
