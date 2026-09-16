"""Independent SAMPLE-AES fixture construction; never imports the decryptor."""

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# NIST SP 800-38A, F.2.1 CBC-AES128. Public test material, not account keys.
KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
IV = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
PLAIN = bytes.fromhex(
    "6bc1bee22e409f96e93d7e117393172a"
    "ae2d8a571e03ac9c9eb76fac45af8e51"
    "30c81c46a35ce411e5fbc1191a0a52ef"
    "f69f2445df4f9b17ad2b417be66c3710"
)
CIPHER = bytes.fromhex(
    "7649abac8119b246cee98e9b12e9197d"
    "5086cb9b507219ee95db113a917678b2"
    "73bed6b8e3c1743b7116e69e22229516"
    "3ff1caa1681fac09120eca307586e1a7"
)


def adts(payload: bytes, *, crc: bool = False) -> bytes:
    size = len(payload) + (9 if crc else 7)
    header = bytes(
        (
            0xFF,
            0xF0 if crc else 0xF1,
            0x50,
            0x80 | (size >> 11),
            (size >> 3) & 255,
            ((size & 7) << 5) | 0x1F,
            0xFC,
        )
    )
    return header + (b"\x12\x34" if crc else b"") + payload


def escape_nal(nal: bytes) -> bytes:
    result = bytearray()
    zeros = 0
    for value in nal:
        if zeros == 2 and value <= 3:
            result.append(3)
            zeros = 0
        result.append(value)
        zeros = zeros + 1 if value == 0 else 0
    return bytes(result)


def crc32_mpeg(data: bytes) -> bytes:
    crc = 0xFFFFFFFF
    for value in data:
        crc ^= value << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
    return crc.to_bytes(4, "big")


def packetize(pid: int, data: bytes, *, chunk_size: int = 184, counter: int = 0) -> bytes:
    packets = []
    for index, start in enumerate(range(0, len(data), chunk_size)):
        chunk = data[start : start + chunk_size]
        header = bytes(
            (
                0x47,
                (pid >> 8) | (0x40 if index == 0 else 0),
                pid & 255,
                (0x30 if len(chunk) < 184 else 0x10) | ((counter + index) & 15),
            )
        )
        padding = b""
        if len(chunk) < 184:
            size = 183 - len(chunk)
            padding = bytes((size,)) + (b"\x00" + b"\xff" * (size - 1) if size else b"")
        packets.append(header + padding + chunk)
    return b"".join(packets)


def pes(payload: bytes, stream_id: int = 0xC0) -> bytes:
    length = len(payload) + 3
    return (
        b"\x00\x00\x01"
        + bytes((stream_id,))
        + length.to_bytes(2, "big")
        + b"\x80\x00\x00"
        + payload
    )


def transport_stream(payload: bytes, *, video: bool = False, chunk_size: int = 184) -> bytes:
    pat = bytes.fromhex("00b00d0001c100000001f000")
    stream_type = 0xDB if video else 0xCF
    pmt = bytes.fromhex("02b0120001c10000e100f000") + bytes((stream_type, 0xE1, 0, 0xF0, 0))
    return (
        packetize(0, b"\0" + pat + crc32_mpeg(pat))
        + packetize(0x1000, b"\0" + pmt + crc32_mpeg(pmt))
        + packetize(0x100, pes(payload, 0xE0 if video else 0xC0), chunk_size=chunk_size)
    )


def encrypt_aac(data: bytes, key: bytes = KEY, iv: bytes = IV) -> bytes:
    result = bytearray(data)
    offset = 0
    while offset < len(data):
        size = ((data[offset + 3] & 3) << 11) | (data[offset + 4] << 3) | (data[offset + 5] >> 5)
        header = 7 if data[offset + 1] & 1 else 9
        start = offset + header + 16
        end = start + max(0, (size - header - 16) // 16) * 16
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        result[start:end] = encryptor.update(data[start:end]) + encryptor.finalize()
        offset += size
    return bytes(result)
