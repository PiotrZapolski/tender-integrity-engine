import hashlib

import pytest

from tie.rawstore import RawStore


def test_put_get_roundtrip(tmp_path):
    rs = RawStore(tmp_path)
    data = "Zażółć gęślą jaźń".encode() * 100
    sha = rs.put(data, source="bzp")
    assert sha == hashlib.sha256(data).hexdigest()
    assert rs.exists(sha, source="bzp")
    assert rs.get(sha, source="bzp") == data
    path = tmp_path / "bzp" / sha[:2] / sha[2:4] / f"{sha}.zst"
    assert path.exists()
    assert path.stat().st_size < len(data)


def test_put_twice_is_noop(tmp_path):
    rs = RawStore(tmp_path)
    a = rs.put(b"x", source="ezam")
    mtime = (tmp_path / "ezam" / a[:2] / a[2:4] / f"{a}.zst").stat().st_mtime_ns
    assert rs.put(b"x", source="ezam") == a
    assert (tmp_path / "ezam" / a[:2] / a[2:4] / f"{a}.zst").stat().st_mtime_ns == mtime


def test_get_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        RawStore(tmp_path).get("0" * 64, source="bzp")
