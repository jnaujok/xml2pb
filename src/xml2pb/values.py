"""Convert decoded XSD values into protobuf field values."""

from __future__ import annotations

import binascii
import calendar
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from google.protobuf.descriptor import FieldDescriptor as FD

from .errors import ConversionError
from .names import norm

INT_RANGES = {
    FD.CPPTYPE_INT32: (-(2**31), 2**31 - 1),
    FD.CPPTYPE_INT64: (-(2**63), 2**63 - 1),
    FD.CPPTYPE_UINT32: (0, 2**32 - 1),
    FD.CPPTYPE_UINT64: (0, 2**64 - 1),
}
FLOAT32_MAX = 3.4028234663852886e38

_TYPE_NAMES = {
    getattr(FD, f"TYPE_{n.upper()}"): n
    for n in (
        "double float int64 uint64 int32 fixed64 fixed32 bool string group "
        "message bytes uint32 enum sfixed32 sfixed64 sint32 sint64"
    ).split()
}

# xs:dateTime / xs:date lexical form. Fractional seconds may have any number of
# digits; protobuf Timestamps keep nanoseconds (9 digits), the rest is truncated.
_DATETIME_RE = re.compile(
    r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
    r"(?:T(?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})(?:\.(?P<frac>\d+))?)?"
    r"(?P<tz>Z|[+-]\d{2}:\d{2})?"
)


def type_name(f: Any) -> str:
    """Human-readable proto type of a field, e.g. ``int32``."""
    return _TYPE_NAMES.get(f.type, str(f.type))


def unwrap(v: Any, path: str) -> Any:
    """Reduce a decoded XML node to a simple value.

    An element with attributes decodes to ``{'@attr': ..., '$': text}``; this
    returns the text. An ``xs:list`` value decodes to a list; this joins it with
    spaces (the list's lexical form).
    """
    if isinstance(v, dict):
        if "$" not in v:
            raise ConversionError(f"{path}: expected a simple value, got a complex element")
        return v["$"]
    if isinstance(v, list):
        return " ".join(str(x) for x in v)
    return v


def to_scalar(f: Any, v: Any, path: str) -> Any:
    """Convert a decoded value to the Python value for scalar/enum field ``f``.

    Raises :class:`ConversionError` (with ``path``) rather than silently losing
    data: fractional numbers for integer fields, out-of-range integers, float32
    overflow, unknown enum names, ...
    """
    v = unwrap(v, path)
    c = f.cpp_type
    try:
        if c == FD.CPPTYPE_BOOL:
            if isinstance(v, bool):
                return v
            s = str(v).strip().lower()
            if s in ("true", "1"):
                return True
            if s in ("false", "0"):
                return False
            raise ValueError(f"'{v}' is not a boolean")

        if c in INT_RANGES:
            if isinstance(v, bool):
                raise ValueError("boolean given for integer field")
            if isinstance(v, int):
                n = v
            else:
                try:
                    d = Decimal(str(v).strip())
                except InvalidOperation:
                    raise ValueError(f"'{v}' is not an integer") from None
                if not d.is_finite() or d != d.to_integral_value():
                    raise ValueError(f"'{v}' is not a whole number (would lose data)")
                n = int(d)
            lo, hi = INT_RANGES[c]
            if not lo <= n <= hi:
                raise ValueError(f"{n} out of range for {type_name(f)} [{lo}, {hi}]")
            return n

        if c in (FD.CPPTYPE_FLOAT, FD.CPPTYPE_DOUBLE):
            if isinstance(v, bool):
                raise ValueError("boolean given for floating-point field")
            x = float(v)
            if c == FD.CPPTYPE_FLOAT and abs(x) != float("inf") and abs(x) > FLOAT32_MAX:
                raise ValueError(f"{x} overflows float32")
            return x

        if c == FD.CPPTYPE_ENUM:
            return to_enum(f.enum_type, v)

        if c == FD.CPPTYPE_STRING:
            if f.type == FD.TYPE_BYTES:
                return to_bytes(v)
            if isinstance(v, bool):
                return "true" if v else "false"
            return str(v)
    except (ValueError, ArithmeticError, binascii.Error) as e:
        raise ConversionError(f"{path}: {e}") from None
    raise ConversionError(f"{path}: unsupported field type {type_name(f)}")


def to_bytes(v: Any) -> bytes:
    """Bytes for a ``bytes`` field.

    ``xs:hexBinary`` / ``xs:base64Binary`` values (decoded as typed binary objects)
    are decoded to their raw bytes; any other text is encoded as UTF-8.
    """
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    decode = getattr(v, "decode", None)
    if not isinstance(v, str) and callable(decode):  # elementpath HexBinary/Base64Binary
        raw = decode()
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw)
    return str(v).encode("utf-8")


def to_enum(ed: Any, v: Any) -> int:
    """Number of the enum value matching ``v``.

    Accepts the exact value name (``STATUS_IN_PROGRESS``), its number, or the XML
    lexical value matched ignoring case and separators, with or without the
    enum-name prefix (``in-progress``, ``IN_PROGRESS``).
    """
    s = str(v).strip()
    if s in ed.values_by_name:
        return ed.values_by_name[s].number
    if re.fullmatch(r"-?\d+", s):
        n = int(s)
        if n in ed.values_by_number:
            return n
        raise ValueError(f"{n} is not a value of enum {ed.name}")
    target, prefix = norm(s), norm(ed.name)
    for val in ed.values:
        n = norm(val.name)
        short = n[len(prefix) :] if n.startswith(prefix) else n
        if target in (n, short):
            return val.number
    raise ValueError(
        f"'{s}' is not a value of enum {ed.name} ({', '.join(x.name for x in ed.values)})"
    )


def parse_datetime(v: Any) -> tuple[int, int]:
    """Parse an ``xs:dateTime`` or ``xs:date`` into ``(seconds, nanos)`` since the epoch.

    Values without a timezone are taken as UTC. ``24:00:00`` (end of day) is
    accepted, as the XSD spec allows.
    """
    if isinstance(v, datetime):
        dt = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        return calendar.timegm(dt.utctimetuple()), dt.microsecond * 1000
    s = str(v).strip()
    m = _DATETIME_RE.fullmatch(s)
    if not m:
        raise ValueError(f"'{s}' is not an ISO-8601 date/time")
    y, mo, d = int(m["y"]), int(m["mo"]), int(m["d"])
    h, mi, sec = int(m["h"] or 0), int(m["mi"] or 0), int(m["s"] or 0)
    frac = m["frac"] or ""
    end_of_day = h == 24 and mi == 0 and sec == 0 and not frac.strip("0")
    try:
        base = datetime(y, mo, d, 0 if end_of_day else h, mi, sec, tzinfo=timezone.utc)
    except ValueError as e:
        raise ValueError(f"'{s}' is not a valid date/time: {e}") from None
    seconds = calendar.timegm(base.utctimetuple()) + (86400 if end_of_day else 0)
    tz = m["tz"]
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        seconds -= sign * (int(tz[1:3]) * 3600 + int(tz[4:6]) * 60)
    return seconds, int(frac[:9].ljust(9, "0")) if frac else 0


def set_timestamp(msg: Any, v: Any, path: str) -> None:
    """Fill a ``google.protobuf.Timestamp`` from a date/time value."""
    try:
        msg.seconds, msg.nanos = parse_datetime(unwrap(v, path))
    except ValueError as e:
        raise ConversionError(f"{path}: {e}") from None
