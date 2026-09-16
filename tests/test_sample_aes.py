import pytest

from sample_aes_fixtures import CIPHER, IV, KEY, PLAIN, adts, escape_nal, transport_stream


@pytest.mark.parametrize("crc", [False, True])
@pytest.mark.parametrize("trailer", [b"", b"tail", b"x" * 15])
def test_aac_nist_vector_preserves_leader_trailer_and_resets_iv(crc, trailer):
    from getcourse_downloader.infrastructure.media.sample_aes import decrypt_aac

    encrypted = adts(b"L" * 16 + CIPHER + trailer, crc=crc)
    expected = adts(b"L" * 16 + PLAIN + trailer, crc=crc)
    assert decrypt_aac(encrypted * 2, KEY, IV) == expected * 2


@pytest.mark.parametrize("size", [0, 15, 16, 31])
def test_short_aac_frame_remains_clear(size):
    from getcourse_downloader.infrastructure.media.sample_aes import decrypt_aac

    frame = adts(b"a" * size)
    assert decrypt_aac(frame, KEY, IV) == frame


def test_h264_nist_vector_skip_pattern_and_emulation_prevention():
    from getcourse_downloader.infrastructure.media.sample_aes import decrypt_h264

    leader = b"\x65" + b"L" * 31
    skipped = b"x" * 140 + b"\x00\x00\x03\x01"
    nal = leader + CIPHER[:16] + skipped + CIPHER[16:32] + b"z"
    expected = leader + PLAIN[:16] + skipped + PLAIN[16:32] + b"z"
    encrypted = escape_nal(nal)
    # The transport-preserving API replaces the removed outer escape layer with
    # Annex B trailing_zero_8bits, retaining PES sizes and timestamp positions.
    expected += b"\0" * (len(encrypted) - len(expected))
    unit = b"\x00\x00\x01" + encrypted
    assert decrypt_h264(unit * 2, KEY, IV) == (b"\x00\x00\x01" + expected) * 2


@pytest.mark.parametrize("nal_type,size", [(1, 48), (5, 48), (7, 300), (8, 60), (6, 100)])
def test_h264_unprotected_units_and_exact_final_block_remain_clear(nal_type, size):
    from getcourse_downloader.infrastructure.media.sample_aes import decrypt_h264

    data = b"\x00\x00\x00\x01" + bytes((nal_type,)) + b"q" * (size - 1)
    assert decrypt_h264(data, KEY, IV) == data
    # Exactly 16 bytes at the next encrypted position must remain clear too.
    data = b"\x00\x00\x01\x61" + b"L" * 31 + CIPHER[:16] + b"s" * 160
    expected = b"\x00\x00\x01\x61" + b"L" * 31 + PLAIN[:16] + b"s" * 160
    assert decrypt_h264(data, KEY, IV) == expected


@pytest.mark.parametrize("chunk_size", [184, 17, 5])
def test_ts_reassembles_aac_across_packets_and_split_pes_header(chunk_size):
    from getcourse_downloader.infrastructure.media.sample_aes import decrypt_sample_aes_ts

    encrypted = transport_stream(adts(b"L" * 16 + CIPHER) * 3, chunk_size=chunk_size)
    expected = bytearray(transport_stream(adts(b"L" * 16 + PLAIN) * 3, chunk_size=chunk_size))
    result = decrypt_sample_aes_ts(encrypted, KEY, IV)
    # PES/TS layout is retained; PMT and its CRC are the only header changes.
    assert result[:188] == expected[:188]
    assert result[376:] == expected[376:]
    assert len(result) == len(encrypted)
    assert b"\x0f\xe1\x00\xf0\x00" in result[188:376]
    assert b"\xcf\xe1\x00\xf0\x00" not in result[188:376]


@pytest.mark.parametrize("data", [b"", b"not a TS", b"\x47" + b"\0" * 187])
def test_invalid_transport_fails_without_producing_clear_output(data):
    from getcourse_downloader.infrastructure.media.sample_aes import (
        SampleAesError,
        decrypt_sample_aes_ts,
    )

    with pytest.raises(SampleAesError):
        decrypt_sample_aes_ts(data, KEY, IV)


@pytest.mark.parametrize("data", [b"bad", adts(b"x" * 70)[:-1]])
def test_truncated_aac_fails(data):
    from getcourse_downloader.infrastructure.media.sample_aes import SampleAesError, decrypt_aac

    with pytest.raises(SampleAesError):
        decrypt_aac(data, KEY, IV)
