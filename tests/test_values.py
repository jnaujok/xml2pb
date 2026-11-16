"""Tests for value conversion (XSD-decoded values -> proto field values)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from elementpath.datatypes import Base64Binary, HexBinary
from google.protobuf import timestamp_pb2
from google.protobuf.descriptor import FieldDescriptor as FD

from xml2pb import ConversionError
from xml2pb.protos import load_proto
from xml2pb.values import (
    parse_datetime,
    set_timestamp,
    to_bytes,
    to_enum,
    to_scalar,
    type_name,
    unwrap,
)

SCALARS = """
syntax = "proto3";
package t;
enum Color { COLOR_UNSPECIFIED = 0; COLOR_RED = 1; COLOR_DARK_BLUE = 2; }
enum Bare { ZERO = 0; ONE = 1; }
message S {
  bool b = 1;
  int32 i32 = 2;
  int64 i64 = 3;
  uint32 u32 = 4;
  uint64 u64 = 5;
  sint32 s32 = 6;
  fixed64 f64 = 7;
  float f = 8;
  double d = 9;
  string s = 10;
  bytes by = 11;
  Color color = 12;
  Bare bare = 13;
  S nested = 14;
}
"""


@pytest.fixture(scope="module")
def fields(tmp_path_factory):
    p = tmp_path_factory.mktemp("v") / "s.proto"
    p.write_text(SCALARS)
    pool, _, _ = load_proto(p)
    return pool.FindMessageTypeByName("t.S").fields_by_name


class TestUnwrap:
    def test_plain(self):
        assert unwrap(5, "p") == 5

    def test_text_of_element_with_attributes(self):
        assert unwrap({"@currency": "USD", "$": Decimal("5")}, "p") == Decimal("5")

    def test_complex_element_rejected(self):
        with pytest.raises(ConversionError, match=r"p: expected a simple value"):
            unwrap({"a": 1}, "p")

    def test_xs_list_joined(self):
        assert unwrap([1, 2, 3], "p") == "1 2 3"


class TestBool:
    @pytest.mark.parametrize("v", [True, "true", "TRUE", " 1 ", 1])
    def test_true(self, fields, v):
        assert to_scalar(fields["b"], v, "p") is True

    @pytest.mark.parametrize("v", [False, "false", "0", 0])
    def test_false(self, fields, v):
        assert to_scalar(fields["b"], v, "p") is False

    def test_invalid(self, fields):
        with pytest.raises(ConversionError, match=r"p: 'yes' is not a boolean"):
            to_scalar(fields["b"], "yes", "p")


class TestIntegers:
    @pytest.mark.parametrize(
        "v, expected", [(5, 5), ("7", 7), (" -3 ", -3), (Decimal("4.000"), 4), ("1e3", 1000)]
    )
    def test_accepts_whole_numbers(self, fields, v, expected):
        assert to_scalar(fields["i32"], v, "p") == expected

    @pytest.mark.parametrize("v", [Decimal("1.5"), "2.25", 3.5])
    def test_rejects_fractions(self, fields, v):
        with pytest.raises(ConversionError, match="not a whole number"):
            to_scalar(fields["i64"], v, "p")

    @pytest.mark.parametrize("v", ["NaN", "Infinity", float("inf")])
    def test_rejects_non_finite(self, fields, v):
        with pytest.raises(ConversionError, match="not a whole number"):
            to_scalar(fields["i64"], v, "p")

    def test_rejects_garbage(self, fields):
        with pytest.raises(ConversionError, match=r"^p: 'abc' is not an integer$"):
            to_scalar(fields["i32"], "abc", "p")

    def test_rejects_bool(self, fields):
        with pytest.raises(ConversionError, match="boolean given for integer field"):
            to_scalar(fields["i32"], True, "p")

    @pytest.mark.parametrize(
        "field, ok, bad",
        [
            ("i32", 2**31 - 1, 2**31),
            ("i32", -(2**31), -(2**31) - 1),
            ("i64", 2**63 - 1, 2**63),
            ("u32", 2**32 - 1, 2**32),
            ("u32", 0, -1),
            ("u64", 2**64 - 1, 2**64),
            ("s32", -(2**31), -(2**31) - 1),
            ("f64", 2**64 - 1, -1),
        ],
    )
    def test_ranges(self, fields, field, ok, bad):
        assert to_scalar(fields[field], ok, "p") == ok
        with pytest.raises(ConversionError, match=f"out of range for {type_name(fields[field])}"):
            to_scalar(fields[field], bad, "p")


class TestFloats:
    @pytest.mark.parametrize("v, expected", [(1.5, 1.5), ("2.5", 2.5), (Decimal("0.1"), 0.1)])
    def test_double(self, fields, v, expected):
        assert to_scalar(fields["d"], v, "p") == expected

    def test_double_special_values(self, fields):
        assert to_scalar(fields["d"], "INF", "p") == float("inf")

    def test_float32_overflow(self, fields):
        with pytest.raises(ConversionError, match="overflows float32"):
            to_scalar(fields["f"], "1e39", "p")

    def test_float32_accepts_infinity(self, fields):
        assert to_scalar(fields["f"], float("-inf"), "p") == float("-inf")

    def test_double_allows_large(self, fields):
        assert to_scalar(fields["d"], "1e39", "p") == 1e39

    def test_rejects_bool(self, fields):
        with pytest.raises(ConversionError, match="boolean given for floating-point"):
            to_scalar(fields["d"], True, "p")

    def test_rejects_garbage(self, fields):
        with pytest.raises(ConversionError, match=r"^p: "):
            to_scalar(fields["f"], "abc", "p")


class TestStrings:
    @pytest.mark.parametrize(
        "v, expected",
        [("x", "x"), (5, "5"), (Decimal("5.00"), "5.00"), (True, "true"), (False, "false")],
    )
    def test_string(self, fields, v, expected):
        assert to_scalar(fields["s"], v, "p") == expected

    def test_string_from_text_dict(self, fields):
        assert to_scalar(fields["s"], {"@a": "1", "$": "body"}, "p") == "body"


class TestBytes:
    def test_hex(self, fields):
        assert to_scalar(fields["by"], HexBinary(b"DEADBEEF"), "p") == b"\xde\xad\xbe\xef"

    def test_base64(self, fields):
        assert to_scalar(fields["by"], Base64Binary(b"3q2+7w=="), "p") == b"\xde\xad\xbe\xef"

    def test_raw_bytes(self):
        assert to_bytes(b"\x00\x01") == b"\x00\x01"
        assert to_bytes(bytearray(b"\x02")) == b"\x02"

    def test_plain_text_is_utf8_not_base64(self):
        # "abcd" happens to be valid base64; a plain xs:string must not be decoded.
        assert to_bytes("abcd") == b"abcd"
        assert to_bytes("déjà") == "déjà".encode()

    def test_object_whose_decode_returns_text(self):
        weird = SimpleNamespace(decode=lambda: "not bytes")
        assert to_bytes(weird) == str(weird).encode()


class TestEnum:
    @pytest.mark.parametrize(
        "v, expected",
        [
            ("COLOR_RED", 1),
            ("1", 1),
            (2, 2),
            ("red", 1),
            ("Red", 1),
            ("dark-blue", 2),
            ("DARK_BLUE", 2),
            ("darkBlue", 2),
            ("  red  ", 1),
            ("unspecified", 0),
        ],
    )
    def test_matches(self, fields, v, expected):
        assert to_scalar(fields["color"], v, "p") == expected

    def test_unprefixed_enum(self, fields):
        assert to_scalar(fields["bare"], "one", "p") == 1

    def test_unknown_name(self, fields):
        with pytest.raises(ConversionError, match=r"'green' is not a value of enum Color"):
            to_scalar(fields["color"], "green", "p")

    def test_unknown_number(self, fields):
        with pytest.raises(ConversionError, match=r"9 is not a value of enum Color"):
            to_scalar(fields["color"], "9", "p")

    def test_error_lists_values(self, fields):
        # to_enum raises ValueError; to_scalar turns it into a ConversionError with a path.
        with pytest.raises(ValueError, match="COLOR_UNSPECIFIED, COLOR_RED, COLOR_DARK_BLUE"):
            to_enum(fields["color"].enum_type, "nope")


def test_message_field_is_unsupported(fields):
    with pytest.raises(ConversionError, match="unsupported field type message"):
        to_scalar(fields["nested"], "x", "p")


def test_type_name_unknown():
    assert type_name(SimpleNamespace(type=999)) == "999"


def test_type_names(fields):
    assert type_name(fields["s32"]) == "sint32"
    assert fields["s32"].type == FD.TYPE_SINT32


class TestParseDatetime:
    @pytest.mark.parametrize(
        "s, expected",
        [
            ("1970-01-01T00:00:00Z", (0, 0)),
            ("1970-01-01T00:00:00", (0, 0)),  # no zone -> UTC
            ("1970-01-01T01:00:00+01:00", (0, 0)),
            ("1969-12-31T19:00:00-05:00", (0, 0)),
            ("1970-01-01T00:00:01.5Z", (1, 500_000_000)),
            ("1970-01-01T00:00:00.123456789Z", (0, 123_456_789)),
            ("1970-01-01T00:00:00.1234567891234Z", (0, 123_456_789)),  # truncated
            ("1970-01-02", (86400, 0)),  # xs:date
            ("1970-01-02Z", (86400, 0)),
            ("1970-01-01T24:00:00Z", (86400, 0)),  # end of day
            ("1970-01-01T24:00:00.000Z", (86400, 0)),
            ("1960-01-01T00:00:00Z", (-315619200, 0)),
            ("  2026-01-02T03:04:05Z  ", (1767323045, 0)),
        ],
    )
    def test_values(self, s, expected):
        assert parse_datetime(s) == expected

    def test_matches_stdlib(self):
        s = "2026-07-14T23:59:58.25+05:30"
        dt = datetime(2026, 7, 14, 23, 59, 58, 250000, tzinfo=timezone(timedelta(hours=5.5)))
        assert parse_datetime(s) == (int(dt.timestamp()), 250_000_000)

    def test_datetime_objects(self):
        dt = datetime(1970, 1, 1, 0, 0, 2, 7, tzinfo=timezone.utc)
        assert parse_datetime(dt) == (2, 7000)
        assert parse_datetime(datetime(1970, 1, 1, 0, 0, 3)) == (3, 0)  # naive -> UTC

    @pytest.mark.parametrize(
        "s",
        ["yesterday", "2026-13-01T00:00:00Z", "2026-02-30", "2026-01-01T25:00:00Z", "2026-1-1"],
    )
    def test_invalid(self, s):
        with pytest.raises(ValueError):
            parse_datetime(s)

    def test_24_with_minutes_is_invalid(self):
        with pytest.raises(ValueError, match="not a valid date/time"):
            parse_datetime("2026-01-01T24:00:01Z")


class TestSetTimestamp:
    def test_sets_fields(self):
        ts = timestamp_pb2.Timestamp()
        set_timestamp(ts, "1970-01-01T00:00:10.000000001Z", "p")
        assert (ts.seconds, ts.nanos) == (10, 1)

    def test_unwraps_text(self):
        ts = timestamp_pb2.Timestamp()
        set_timestamp(ts, {"@zone": "x", "$": "1970-01-01T00:00:01Z"}, "p")
        assert ts.seconds == 1

    def test_error_has_path(self):
        with pytest.raises(ConversionError, match=r"^a/b: 'nope' is not an ISO-8601"):
            set_timestamp(timestamp_pb2.Timestamp(), "nope", "a/b")
