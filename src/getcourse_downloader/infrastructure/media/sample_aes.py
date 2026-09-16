"""SAMPLE-AES decryption for H.264/AAC carried in MPEG-2 transport streams.

RFC 8216 delegates the MPEG-TS sample layout to Apple's HLS Sample Encryption
specification.  This module intentionally supports that clear-key layout only:
AES-128-CBC, H.264 Annex B NAL units, and AAC ADTS frames.  It does not accept
license protocols or Common Encryption (fMP4/cbcs).
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_TS_PACKET_SIZE = 188
_H264_SAMPLE_AES_STREAM_TYPE = 0xDB
_AAC_SAMPLE_AES_STREAM_TYPE = 0xCF
_H264_STREAM_TYPE = 0x1B
_AAC_STREAM_TYPE = 0x0F


class SampleAesError(ValueError):
    """Raised when a clear-key MPEG-TS SAMPLE-AES segment is malformed."""


def _validate_key_and_iv(key: bytes, iv: bytes) -> None:
    if len(key) != 16:
        raise SampleAesError("SAMPLE-AES key must contain exactly 16 bytes")
    if len(iv) != 16:
        raise SampleAesError("SAMPLE-AES IV must contain exactly 16 bytes")


def _cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    if len(data) % 16:
        raise SampleAesError("encrypted SAMPLE-AES data is not a whole AES block")

    decryptor = Cipher(
        algorithms.AES(key),
        modes.CBC(iv),
    ).decryptor()

    decrypted = decryptor.update(data) + decryptor.finalize()
    return bytes(decrypted)


def _adts_frame_length(data: bytes, offset: int) -> tuple[int, int]:
    if offset + 7 > len(data) or data[offset] != 0xFF or data[offset + 1] & 0xF6 != 0xF0:
        raise SampleAesError("invalid ADTS frame header")
    header_length = 7 if data[offset + 1] & 1 else 9
    frame_length = (
        ((data[offset + 3] & 0x03) << 11) | (data[offset + 4] << 3) | (data[offset + 5] >> 5)
    )
    if frame_length < header_length or offset + frame_length > len(data):
        raise SampleAesError("truncated ADTS frame")
    return header_length, frame_length


def decrypt_aac(data: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt ADTS AAC protected blocks, resetting the IV for each frame."""

    _validate_key_and_iv(key, iv)
    result = bytearray(data)
    offset = 0
    found_frame = False
    while offset < len(data):
        if offset + 1 >= len(data):
            raise SampleAesError("truncated ADTS frame")
        if data[offset] != 0xFF or data[offset + 1] & 0xF6 != 0xF0:
            raise SampleAesError("expected an ADTS frame at an AAC PES boundary")
        header_length, frame_length = _adts_frame_length(data, offset)
        encrypted_start = offset + header_length + 16
        encrypted_length = max(0, (frame_length - header_length - 16) // 16) * 16
        if encrypted_length:
            encrypted_end = encrypted_start + encrypted_length
            result[encrypted_start:encrypted_end] = _cbc_decrypt(
                data[encrypted_start:encrypted_end], key, iv
            )
        found_frame = True
        offset += frame_length
    if not found_frame:
        raise SampleAesError("AAC PES contains no ADTS frame")
    return bytes(result)


def _start_codes(data: bytes) -> list[tuple[int, int]]:
    codes: list[tuple[int, int]] = []
    offset = 0
    while offset + 3 <= len(data):
        if data[offset : offset + 3] == b"\x00\x00\x01":
            if offset > 0 and data[offset - 1] == 0:
                codes.append((offset - 1, 4))
            else:
                codes.append((offset, 3))
            offset += 3
        else:
            offset += 1
    deduplicated: list[tuple[int, int]] = []
    for code in codes:
        if not deduplicated or code[0] != deduplicated[-1][0]:
            deduplicated.append(code)
    return deduplicated


def _remove_emulation_prevention(data: bytes) -> bytes:
    result = bytearray()
    zeroes = 0
    for value in data:
        if zeroes >= 2 and value == 0x03:
            zeroes = 0
            continue
        result.append(value)
        zeroes = zeroes + 1 if value == 0 else 0
    return bytes(result)


def _decrypt_h264_nal(nal: bytes, key: bytes, iv: bytes) -> bytes:
    if len(nal) <= 48 or (nal[0] & 0x1F) not in {1, 5}:
        return nal
    result = bytearray(_remove_emulation_prevention(nal))
    position = 32
    remaining = len(result) - position
    current_iv = iv
    while remaining > 0:
        if remaining > 16:
            cipher_block = bytes(result[position : position + 16])
            result[position : position + 16] = _cbc_decrypt(cipher_block, key, current_iv)
            current_iv = cipher_block
            position += 16
            remaining -= 16
        skipped = min(144, remaining)
        position += skipped
        remaining -= skipped
    return bytes(result)


def decrypt_h264(data: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt H.264 SAMPLE-AES NAL payloads while retaining TS/PES byte size."""

    _validate_key_and_iv(key, iv)
    codes = _start_codes(data)
    if not codes:
        raise SampleAesError("H.264 PES contains no Annex B NAL unit")
    result = bytearray()
    if codes[0][0]:
        result.extend(data[: codes[0][0]])
    for index, (start, start_length) in enumerate(codes):
        nal_start = start + start_length
        nal_end = codes[index + 1][0] if index + 1 < len(codes) else len(data)
        nal = data[nal_start:nal_end]
        if not nal:
            raise SampleAesError("empty H.264 NAL unit")
        decrypted = _decrypt_h264_nal(nal, key, iv)
        # Removing the encryption-layer emulation-prevention byte shortens a
        # NAL.  Annex B permits trailing_zero_8bits before the following start
        # code, so retain packet offsets without changing unrelated TS packets.
        result.extend(data[start:nal_start])
        result.extend(decrypted)
        result.extend(b"\0" * (len(nal) - len(decrypted)))
    return bytes(result)


@dataclass(frozen=True, slots=True)
class _Packet:
    offset: int
    pid: int
    payload_start: int | None
    payload_end: int
    payload_unit_start: bool


def _parse_packets(data: bytes | bytearray) -> list[_Packet]:
    if not data or len(data) % _TS_PACKET_SIZE:
        raise SampleAesError("MPEG-TS segment must contain complete 188-byte packets")
    packets: list[_Packet] = []
    for offset in range(0, len(data), _TS_PACKET_SIZE):
        if data[offset] != 0x47:
            raise SampleAesError("MPEG-TS synchronization byte is missing")
        adaptation_control = (data[offset + 3] >> 4) & 0x03
        if adaptation_control == 0:
            raise SampleAesError("invalid MPEG-TS adaptation-field control")
        payload_start = offset + 4
        if adaptation_control & 0x02:
            adaptation_length = data[payload_start]
            payload_start += adaptation_length + 1
        packet_end = offset + _TS_PACKET_SIZE
        if payload_start > packet_end:
            raise SampleAesError("MPEG-TS adaptation field exceeds packet boundary")
        packets.append(
            _Packet(
                offset=offset,
                pid=((data[offset + 1] & 0x1F) << 8) | data[offset + 2],
                payload_start=(
                    payload_start
                    if adaptation_control & 0x01 and payload_start < packet_end
                    else None
                ),
                payload_end=packet_end,
                payload_unit_start=bool(data[offset + 1] & 0x40),
            )
        )
    return packets


def _packet_payload(data: bytes | bytearray, packet: _Packet) -> tuple[bytes, list[int]]:
    if packet.payload_start is None:
        return b"", []
    positions = list(range(packet.payload_start, packet.payload_end))
    return bytes(data[position] for position in positions), positions


def _psi_section(
    data: bytes | bytearray, packets: list[_Packet], pid: int
) -> tuple[bytearray, list[int]]:
    relevant = [
        packet for packet in packets if packet.pid == pid and packet.payload_start is not None
    ]
    first = next((packet for packet in relevant if packet.payload_unit_start), None)
    if first is None:
        raise SampleAesError("MPEG-TS PSI section is missing")
    payload, positions = _packet_payload(data, first)
    if not payload:
        raise SampleAesError("empty MPEG-TS PSI section")
    pointer = payload[0]
    start = 1 + pointer
    if start + 3 > len(payload):
        raise SampleAesError("truncated MPEG-TS PSI section")
    length = ((payload[start + 1] & 0x0F) << 8) | payload[start + 2]
    end = start + 3 + length
    if end > len(payload):
        raise SampleAesError("fragmented MPEG-TS PSI sections are unsupported")
    return bytearray(payload[start:end]), positions[start:end]


def _pat_pmt_pid(section: bytes | bytearray) -> int:
    if len(section) < 12 or section[0] != 0:
        raise SampleAesError("invalid MPEG-TS PAT")
    end = len(section) - 4
    for offset in range(8, end, 4):
        if offset + 4 > end:
            break
        program = (section[offset] << 8) | section[offset + 1]
        if program:
            return ((section[offset + 2] & 0x1F) << 8) | section[offset + 3]
    raise SampleAesError("MPEG-TS PAT declares no program map")


def _crc32_mpeg(data: bytes | bytearray) -> bytes:
    crc = 0xFFFFFFFF
    for value in data:
        crc ^= value << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
    return crc.to_bytes(4, "big")


def _parse_and_normalize_pmt(section: bytearray) -> dict[int, str]:
    if len(section) < 16 or section[0] != 2:
        raise SampleAesError("invalid MPEG-TS PMT")
    section_length = ((section[1] & 0x0F) << 8) | section[2]
    if len(section) != section_length + 3:
        raise SampleAesError("truncated MPEG-TS PMT")
    program_info_length = ((section[10] & 0x0F) << 8) | section[11]
    end = len(section) - 4
    offset = 12 + program_info_length
    streams: dict[int, str] = {}
    while offset + 5 <= end:
        stream_type = section[offset]
        pid = ((section[offset + 1] & 0x1F) << 8) | section[offset + 2]
        info_length = ((section[offset + 3] & 0x0F) << 8) | section[offset + 4]
        if offset + 5 + info_length > end:
            raise SampleAesError("invalid MPEG-TS PMT elementary-stream descriptor")
        if stream_type == _H264_SAMPLE_AES_STREAM_TYPE:
            section[offset] = _H264_STREAM_TYPE
            streams[pid] = "h264"
        elif stream_type == _AAC_SAMPLE_AES_STREAM_TYPE:
            section[offset] = _AAC_STREAM_TYPE
            streams[pid] = "aac"
        offset += 5 + info_length
    if offset != end:
        raise SampleAesError("invalid MPEG-TS PMT trailing bytes")
    if not streams:
        raise SampleAesError("MPEG-TS PMT has no SAMPLE-AES H.264 or AAC stream")
    section[-4:] = _crc32_mpeg(section[:-4])
    return streams


def _pes_groups(packets: list[_Packet], pid: int) -> list[list[_Packet]]:
    relevant = [
        packet for packet in packets if packet.pid == pid and packet.payload_start is not None
    ]
    groups: list[list[_Packet]] = []
    for packet in relevant:
        if packet.payload_unit_start:
            groups.append([])
        if groups:
            groups[-1].append(packet)
    if not groups:
        raise SampleAesError(f"MPEG-TS encrypted PID {pid} contains no PES packet")
    return groups


def _rewrite_pes(
    data: bytearray, packets: list[list[_Packet]], codec: str, key: bytes, iv: bytes
) -> None:
    for group in packets:
        payload = bytearray()
        positions: list[int] = []
        for packet in group:
            packet_data, packet_positions = _packet_payload(data, packet)
            payload.extend(packet_data)
            positions.extend(packet_positions)
        if len(payload) < 9 or payload[:3] != b"\0\0\1":
            raise SampleAesError("invalid encrypted PES header")
        header_size = 9 + payload[8]
        if header_size > len(payload):
            raise SampleAesError("truncated encrypted PES header")
        elementary = bytes(payload[header_size:])
        decrypted = (
            decrypt_h264(elementary, key, iv)
            if codec == "h264"
            else decrypt_aac(elementary, key, iv)
        )
        if len(decrypted) != len(elementary):
            raise SampleAesError("SAMPLE-AES transform unexpectedly changed PES length")
        for position, value in zip(positions[header_size:], decrypted, strict=True):
            data[position] = value


def decrypt_sample_aes_ts(data: bytes, key: bytes, iv: bytes) -> bytes:
    """Return an ordinary H.264/AAC MPEG-TS segment decrypted from SAMPLE-AES."""

    _validate_key_and_iv(key, iv)
    result = bytearray(data)
    packets = _parse_packets(result)
    pat, _ = _psi_section(result, packets, 0)
    pmt_pid = _pat_pmt_pid(pat)
    pmt, pmt_positions = _psi_section(result, packets, pmt_pid)
    encrypted_streams = _parse_and_normalize_pmt(pmt)
    for position, value in zip(pmt_positions, pmt, strict=True):
        result[position] = value
    for pid, codec in encrypted_streams.items():
        _rewrite_pes(result, _pes_groups(packets, pid), codec, key, iv)
    return bytes(result)
