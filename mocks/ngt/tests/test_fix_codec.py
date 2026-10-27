"""FIX codec: encoding, checksums and stream framing (no network)."""

import pytest

from mock_ngt.fix import FixParseError, FrameReader, as_dict, checksum, decode, encode


def test_codec_round_trip_and_framing():
    raw = encode("D", [(11, "A1"), (38, 100)], "LOANDESK", "VENUEA", 7, sending_time="20261027-13:00:00.000")
    f = as_dict(decode(raw))
    assert f[35] == "D" and f[34] == "7" and f[11] == "A1"
    assert raw.endswith(f"10={checksum(raw[: raw.rindex('10=')])}\x01")
    with pytest.raises(FixParseError):
        decode(raw.replace("A1", "A2"))  # checksum no longer matches
    r = FrameReader()
    assert r.feed(raw[:20]) == [] and r.feed(raw[20:] + raw) == [raw, raw]
