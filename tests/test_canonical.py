"""Canonical encoding is the foundation every other guarantee rests on.

If two logically-equal documents can encode differently, deduplication breaks,
diffs report phantom changes, and lineage claims become unverifiable. These
tests exist to make that failure loud.
"""

from __future__ import annotations

import math

import pytest

from continuum.canonical import (
    canonical_bytes,
    canonical_json,
    digest,
    digest_bytes,
    is_digest,
    short,
)


def test_key_order_does_not_affect_encoding():
    a = {"zebra": 1, "alpha": 2, "middle": {"z": 1, "a": 2}}
    b = {"alpha": 2, "middle": {"a": 2, "z": 1}, "zebra": 1}
    assert canonical_json(a) == canonical_json(b)
    assert digest(a) == digest(b)


def test_encoding_is_stable_across_calls():
    doc = {"b": [1, 2, {"y": None, "x": True}], "a": "text"}
    assert len({digest(doc) for _ in range(50)}) == 1


def test_list_order_is_significant():
    assert digest([1, 2, 3]) != digest([3, 2, 1])


def test_no_insignificant_whitespace():
    assert canonical_json({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'


def test_non_ascii_is_emitted_literally_not_escaped():
    encoded = canonical_json({"note": "ledger — reconciliação"})
    assert "\\u" not in encoded
    assert canonical_bytes({"note": "café"}).decode("utf-8").count("café") == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_are_rejected(value):
    # These would round-trip through most JSON parsers as null or crash them.
    # Silently accepting one means a checkpoint that cannot be read back.
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json({"score": value})


def test_non_string_keys_are_rejected():
    with pytest.raises(TypeError, match="keys must be strings"):
        canonical_json({1: "a"})


def test_unencodable_types_are_rejected_with_a_path():
    with pytest.raises(TypeError, match=r"\$\.outer\[1\]\.inner"):
        canonical_json({"outer": [{}, {"inner": {1, 2}}]})


def test_finite_floats_round_trip():
    import json

    for value in (0.1, -2.5, 1e-9, 1.7976931348623157e308):
        assert json.loads(canonical_json({"v": value}))["v"] == value
        assert math.isfinite(value)


def test_digest_is_prefixed_sha256_of_canonical_bytes():
    import hashlib

    doc = {"a": 1}
    expected = "sha256:" + hashlib.sha256(canonical_bytes(doc)).hexdigest()
    assert digest(doc) == expected
    assert is_digest(digest(doc))


def test_is_digest_rejects_malformed_addresses():
    assert not is_digest("sha256:xyz")
    assert not is_digest("md5:" + "a" * 64)
    assert not is_digest("a" * 64)
    assert not is_digest("sha256:" + "A" * 64)  # uppercase hex is not canonical


def test_empty_and_absent_encode_differently():
    # The model layer normalizes empty values away precisely because these two
    # are distinct at this level.
    assert digest({"a": 1, "b": []}) != digest({"a": 1})


def test_short_is_display_only():
    address = digest_bytes(b"x")
    assert short(address) == address.split(":")[1][:12]
    assert len(short(address, 8)) == 8
