#!/usr/bin/env python3
"""
xml2pb.py - Convert an XML document to a protobuf message, guided by an XSD
and a .proto file whose fields are a SUBSET of the XSD's elements/attributes.

Pipeline
  1. Compile the .proto in-process (grpcio-tools; no external protoc needed).
  2. Validate + decode the XML with the XSD (xmlschema). Decoding is type-aware,
     so xs:int, xs:boolean, xs:dateTime, xs:decimal etc. arrive as real values.
  3. Check that every proto field has a counterpart in the XSD.
  4. Copy matching values into the proto message (names matched ignoring case,
     underscores, hyphens and dots, so `order_id` == `orderId` == `OrderID`).
     XML content with no proto field is skipped (the proto is a subset).
  5. Write binary wire format or protobuf text format.

Install:  pip install xmlschema protobuf grpcio-tools

Usage (convert):
  python xml2pb.py schema.xsd messages.proto input.xml -o out.pb
  python xml2pb.py schema.xsd messages.proto input.xml --format text -o out.txtpb
  python xml2pb.py schema.xsd messages.proto input.xml --message my.pkg.Order -I protos/

Usage (generate a .proto from the XSD; pass only the XSD):
  python xml2pb.py schema.xsd --elements a,b,c -o messages.proto
  python xml2pb.py schema.xsd --elements order --package acme.v1 --decimal string
  Each listed top-level element becomes a message named after it (a -> A);
  complex types become nested messages, xs:enumeration becomes an enum,
  maxOccurs>1 becomes repeated, xs:dateTime becomes google.protobuf.Timestamp.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import calendar
import importlib.resources as ir
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

try:
    import xmlschema
    from xmlschema.validators import XsdComplexType, XsdSimpleType
    from google.protobuf import (
        descriptor_pb2,
        descriptor_pool,
        message_factory,
        text_format,
    )
    from google.protobuf.descriptor import FieldDescriptor as FD
    from grpc_tools import protoc
except ImportError as exc:  # pragma: no cover
    sys.exit(f"Missing dependency: {exc.name}\n"
             "Install with: pip install xmlschema protobuf grpcio-tools")

TEXT_FIELDS = ("value", "text", "content", "body")  # proto field holding element text
WKT = "google.protobuf."

INT_RANGES = {
    FD.CPPTYPE_INT32: (-(2**31), 2**31 - 1),
    FD.CPPTYPE_INT64: (-(2**63), 2**63 - 1),
    FD.CPPTYPE_UINT32: (0, 2**32 - 1),
    FD.CPPTYPE_UINT64: (0, 2**64 - 1),
}


class ConversionError(Exception):
    pass


# --------------------------------------------------------------------------
# Name helpers
# --------------------------------------------------------------------------
def norm(name: str) -> str:
    """Case/separator-insensitive key: order_id, orderId, Order-ID -> orderid."""
    return re.sub(r"[_\-.]", "", name).lower()


def local(key) -> str:
    """Strip '@' attribute marker and any namespace prefix / {uri}."""
    k = str(key).lstrip("@")
    return k.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def is_repeated(f) -> bool:
    r = getattr(f, "is_repeated", None)
    return bool(r) if r is not None else f.label == FD.LABEL_REPEATED


def is_map(f) -> bool:
    return f.message_type is not None and f.message_type.GetOptions().map_entry


# --------------------------------------------------------------------------
# Proto loading
# --------------------------------------------------------------------------
def load_proto(proto_file: str, includes: list[str]):
    """Compile .proto to a descriptor set and load it into a private pool."""
    proto_path = Path(proto_file).resolve()
    if not proto_path.exists():
        raise ConversionError(f"Proto file not found: {proto_file}")
    wkt_dir = str(ir.files("grpc_tools") / "_proto")  # google/protobuf/*.proto

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "desc.bin"
        args = ["protoc", f"-I{proto_path.parent}",
                *[f"-I{i}" for i in includes], f"-I{wkt_dir}",
                f"--descriptor_set_out={out}", "--include_imports",
                str(proto_path)]
        if protoc.main(args) != 0:
            raise ConversionError("protoc failed to compile the .proto file (see above)")
        fds = descriptor_pb2.FileDescriptorSet.FromString(out.read_bytes())

    pool = descriptor_pool.DescriptorPool()
    for fdp in fds.file:  # topologically sorted by protoc
        pool.AddSerializedFile(fdp.SerializeToString())
    return pool, pool.FindFileByName(fds.file[-1].name)  # last = requested file


def message_class(pool, desc):
    """Build a message class across protobuf versions (3.x - 6.x)."""
    if hasattr(message_factory, "GetMessageClass"):  # protobuf >= 4.22
        return message_factory.GetMessageClass(desc)
    factory = message_factory.MessageFactory(pool)
    if hasattr(factory, "GetPrototype"):  # older protobuf
        return factory.GetPrototype(desc)
    return factory.GetMessages([desc.file.name])[desc.full_name]


def is_required(f) -> bool:
    r = getattr(f, "is_required", None)
    return bool(r) if r is not None else f.label == FD.LABEL_REQUIRED


def find_missing_required(msg, path=""):
    """Own walk for unset proto2 `required` fields (avoids FindInitializationErrors)."""
    missing = []
    for f in msg.DESCRIPTOR.fields:
        p = f"{path}.{f.name}" if path else f.name
        if is_required(f) and not msg.HasField(f.name):
            missing.append(p)
        if f.cpp_type != FD.CPPTYPE_MESSAGE:
            continue
        if is_map(f):
            if f.message_type.fields_by_name["value"].cpp_type == FD.CPPTYPE_MESSAGE:
                for k, sub in getattr(msg, f.name).items():
                    missing += find_missing_required(sub, f"{p}[{k}]")
        elif is_repeated(f):
            for i, sub in enumerate(getattr(msg, f.name)):
                missing += find_missing_required(sub, f"{p}[{i}]")
        elif msg.HasField(f.name):
            missing += find_missing_required(getattr(msg, f.name), p)
    return missing


def pick_message(pool, file_desc, root_name: str, explicit: str | None):
    if explicit:
        try:
            return pool.FindMessageTypeByName(explicit)
        except KeyError:
            raise ConversionError(f"Message '{explicit}' not found in proto") from None
    msgs = file_desc.message_types_by_name
    for name, d in msgs.items():
        if norm(name) == norm(root_name):
            return d
    if len(msgs) == 1:
        return next(iter(msgs.values()))
    raise ConversionError(
        f"Can't tell which message maps to root element <{root_name}>. "
        f"Use --message. Available: {', '.join(msgs)}")


# --------------------------------------------------------------------------
# XSD <-> proto structural check
# --------------------------------------------------------------------------
def check_subset(desc, xel, path, issues, seen):
    """Warn about proto fields that have no counterpart in the XSD."""
    sig = (desc.full_name, id(xel))
    if sig in seen:
        return
    seen.add(sig)

    children = {}
    content = getattr(xel.type, "content", None)
    if content is not None and hasattr(content, "iter_elements"):
        for c in content.iter_elements():
            if getattr(c, "name", None):
                children.setdefault(norm(local(c.name)), c)
    attrs = {norm(local(a)) for a in xel.attributes if a}
    simple = xel.type.has_simple_content()

    for f in desc.fields:
        keys = {norm(f.name), norm(f.json_name)}
        child = next((children[k] for k in keys if k in children), None)
        if child is not None:
            mt = f.message_type
            if mt is not None and not is_map(f) and not mt.full_name.startswith(WKT):
                check_subset(mt, child, f"{path}/{f.name}", issues, seen)
        elif keys & attrs:
            continue
        elif simple and norm(f.name) in TEXT_FIELDS:
            continue
        else:
            issues.append(f"{path}.{f.name}: no matching element/attribute in XSD")


# --------------------------------------------------------------------------
# Value conversion
# --------------------------------------------------------------------------
def unwrap(v, path):
    """Element with attributes decodes to {'@a':..,'$': text}; pull out text."""
    if isinstance(v, dict):
        if "$" not in v:
            raise ConversionError(f"{path}: expected a simple value, got a complex element")
        return v["$"]
    if isinstance(v, list):
        return " ".join(str(x) for x in v)  # xs:list simple types
    return v


def to_scalar(f, v, path):
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
                d = Decimal(str(v).strip())
                if d != d.to_integral_value():
                    raise ValueError(f"'{v}' is not a whole number (would lose data)")
                n = int(d)
            lo, hi = INT_RANGES[c]
            if not lo <= n <= hi:
                raise ValueError(f"{n} out of range for {f.type_name if hasattr(f, 'type_name') else 'int'} [{lo}, {hi}]")
            return n

        if c in (FD.CPPTYPE_FLOAT, FD.CPPTYPE_DOUBLE):
            x = float(v)
            if c == FD.CPPTYPE_FLOAT and abs(x) != float("inf") and abs(x) > 3.4028235e38:
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
    raise ConversionError(f"{path}: unsupported field type {f.type}")


def to_bytes(v) -> bytes:
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    s, tname = str(v), type(v).__name__
    if "Hex" in tname:
        return bytes.fromhex(s)
    try:
        return base64.b64decode(s, validate=True)
    except binascii.Error:
        return s.encode("utf-8")


def to_enum(ed, v) -> int:
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
        short = n[len(prefix):] if n.startswith(prefix) else n
        if target in (n, short):
            return val.number
    raise ValueError(f"'{s}' is not a value of enum {ed.name} "
                     f"({', '.join(v.name for v in ed.values)})")


def set_timestamp(msg, v, path):
    s = str(unwrap(v, path)).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ConversionError(f"{path}: '{s}' is not an ISO-8601 date/time") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    msg.seconds = calendar.timegm(dt.utctimetuple())
    msg.nanos = dt.microsecond * 1000


# --------------------------------------------------------------------------
# Filling messages
# --------------------------------------------------------------------------
class Ctx:
    def __init__(self):
        self.skipped: list[str] = []


def fill(msg, data, path, ctx):
    desc = msg.DESCRIPTOR
    if desc.full_name == WKT + "Timestamp":
        set_timestamp(msg, data, path)
        return
    if data is None:
        return
    if not isinstance(data, dict):
        data = {"$": data}

    by_name = {}
    for f in desc.fields:
        by_name.setdefault(norm(f.name), f)
        by_name.setdefault(norm(f.json_name), f)

    for raw_key, val in data.items():
        rk = str(raw_key).lstrip("@")
        if rk.startswith("xmlns") or rk.startswith("xsi:"):
            continue
        if rk == "$":
            f = next((by_name[t] for t in TEXT_FIELDS if t in by_name), None)
            label = "#text"
        else:
            label = local(raw_key)
            f = by_name.get(norm(label))
        if f is None:
            ctx.skipped.append(f"{path}/{label}")
            continue
        set_field(msg, f, val, f"{path}/{label}", ctx)


def set_field(msg, f, val, path, ctx):
    if val is None:  # empty / xsi:nil element
        if f.cpp_type == FD.CPPTYPE_MESSAGE and not is_repeated(f):
            getattr(msg, f.name).SetInParent()
        return
    items = val if isinstance(val, list) else [val]

    if is_map(f):
        fill_map(msg, f, items, path, ctx)
        return

    if not is_repeated(f):
        if len(items) > 1:
            raise ConversionError(
                f"{path}: XML has {len(items)} occurrences but proto field "
                f"'{f.name}' is not repeated (data would be lost)")
        item = items[0]
        if f.cpp_type == FD.CPPTYPE_MESSAGE:
            sub = getattr(msg, f.name)
            sub.SetInParent()
            fill(sub, item, path, ctx)
        else:
            setattr(msg, f.name, to_scalar(f, item, path))
        return

    container = getattr(msg, f.name)
    for i, item in enumerate(items):
        p = f"{path}[{i}]"
        if f.cpp_type == FD.CPPTYPE_MESSAGE:
            fill(container.add(), item, p, ctx)
        else:
            container.append(to_scalar(f, item, p))


def fill_map(msg, f, items, path, ctx):
    kf = f.message_type.fields_by_name["key"]
    vf = f.message_type.fields_by_name["value"]
    target = getattr(msg, f.name)
    for i, item in enumerate(items):
        p = f"{path}[{i}]"
        if not isinstance(item, dict):
            raise ConversionError(f"{p}: map entry needs <key> and <value> children")
        d = {local(k): v for k, v in item.items()}
        if d.get("key") is None:
            raise ConversionError(f"{p}: map entry has no key")
        key = to_scalar(kf, d["key"], p + "/key")
        if vf.cpp_type == FD.CPPTYPE_MESSAGE:
            fill(target[key], d.get("value"), p + "/value", ctx)
        elif d.get("value") is not None:
            target[key] = to_scalar(vf, d["value"], p + "/value")


# --------------------------------------------------------------------------
# XSD -> .proto generation (--elements)
# --------------------------------------------------------------------------
XSD_NS = "http://www.w3.org/2001/XMLSchema"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

_INT_MAP = {
    "int": "int32", "short": "int32", "byte": "int32",
    "long": "int64", "integer": "int64",
    "nonPositiveInteger": "int64", "negativeInteger": "int64",
    "unsignedInt": "uint32", "unsignedShort": "uint32", "unsignedByte": "uint32",
    "unsignedLong": "uint64", "nonNegativeInteger": "uint64", "positiveInteger": "uint64",
}


def camel(s: str) -> str:
    """order_line / order-line / orderLine -> OrderLine (message and enum names)."""
    parts = [p for p in re.split(r"[^0-9A-Za-z]+", s) if p]
    out = "".join(p[:1].upper() + p[1:] for p in parts) or "Message"
    return out if out[0].isalpha() else "M" + out


def snake(s: str) -> str:
    """orderID / OrderLine / order-line -> order_id / order_line (field names)."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", s)
    s = re.sub(r"[^0-9A-Za-z]+", "_", s).strip("_").lower() or "field"
    return "f_" + s if s[0].isdigit() else s


def builtin_name(st) -> str | None:
    """Walk up the type derivation chain to the first built-in xs: type."""
    prefix = "{%s}" % XSD_NS
    t = st
    for _ in range(50):
        if t is None:
            return None
        n = getattr(t, "name", None)
        if n and n.startswith(prefix):
            return n[len(prefix):]
        t = getattr(t, "base_type", None)
    return None


def enum_values(st) -> list[str] | None:
    vals = getattr(st, "enumeration", None)
    if vals is None:
        facet = getattr(st, "facets", {}).get("{%s}enumeration" % XSD_NS)
        vals = getattr(facet, "enumeration", None)
    if not vals:
        return None
    return list(dict.fromkeys(str(v) for v in vals))  # de-duplicated, ordered


def _check(obj, method: str) -> bool:
    fn = getattr(obj, method, None)
    return bool(fn()) if callable(fn) else False


def _declared_attributes(ctype):
    """(local name, attribute) pairs, skipping anyAttribute and xsi:* ."""
    out = []
    for qn, attr in ctype.attributes.items():
        if not qn or qn.startswith("{%s}" % XSI_NS):
            continue
        out.append((local(qn), attr))
    return out


def _el_repeated(el) -> bool:
    try:
        return not el.is_single()
    except AttributeError:
        mo = el.max_occurs
        return mo is None or mo > 1


@dataclass
class _Field:
    name: str
    ptype: str
    kind: str  # "scalar" | "enum" | "message"
    repeated: bool = False
    comment: str | None = None


@dataclass
class _Msg:
    name: str
    fields: list = field(default_factory=list)


@dataclass
class _Enum:
    name: str
    values: list = field(default_factory=list)  # (name, number)


class ProtoGenerator:
    """Build a proto3 file for chosen top-level XSD elements.

    Naming is chosen so the result feeds straight back into the converter:
    the message for <order> is `Order`, fields are snake_case of the XSD names,
    and enum values are PREFIXED_UPPER_SNAKE (the converter's tolerant matching
    maps XML values like "in-progress" to STATUS_IN_PROGRESS).
    """

    def __init__(self, schema, package="", decimal="double",
                 datetime_mode="timestamp", use_optional=True):
        self.schema = schema
        self.package = package
        self.decimal = decimal
        self.datetime_mode = datetime_mode
        self.use_optional = use_optional
        self.names: set[str] = set()         # top-level message/enum names
        self.enum_value_names: set[str] = set()
        self.messages: list[_Msg] = []
        self.enums: list[_Enum] = []
        self.msg_cache: dict[int, _Msg] = {}
        self.enum_cache: dict[int, _Enum] = {}
        self.uses_timestamp = False

    # ---- naming ----------------------------------------------------------
    @staticmethod
    def warn(text):
        print(f"warning: {text}", file=sys.stderr)

    def _claim(self, base, alt=None):
        for cand in (base, alt):
            if cand and cand not in self.names:
                self.names.add(cand)
                return cand
        i = 2
        while f"{base}{i}" in self.names:
            i += 1
        self.names.add(f"{base}{i}")
        return f"{base}{i}"

    def _enum_value(self, base):
        cand, i = base, 2
        while cand in self.enum_value_names or cand in self.names:
            cand = f"{base}_{i}"
            i += 1
        self.enum_value_names.add(cand)
        return cand

    # ---- entry point -----------------------------------------------------
    def _find_element(self, name):
        want = local(name)
        hits = [e for q, e in self.schema.elements.items() if local(q) == want]
        if not hits:  # tolerate case/separator differences
            hits = [e for q, e in self.schema.elements.items() if norm(local(q)) == norm(want)]
        if not hits:
            avail = ", ".join(sorted({local(q) for q in self.schema.elements}))
            raise ConversionError(
                f"Top-level element '{name}' not found in XSD. Available: {avail}")
        if len(hits) > 1:
            self.warn(f"'{name}' matches {len(hits)} global elements; using the first")
        return hits[0]

    def build(self, names, source) -> str:
        roots, seen = [], set()
        for n in names:
            el = self._find_element(n)
            ln = local(el.name)
            if ln in seen:
                continue
            seen.add(ln)
            # Reserve every root name first so nested types can't steal them.
            roots.append((el, self._claim(camel(ln))))
        for el, mname in roots:
            self._build_message(el.type, mname)
        return self.render(source, [local(e.name) for e, _ in roots])

    # ---- messages --------------------------------------------------------
    def _build_message(self, xtype, name):
        msg = _Msg(name)
        self.messages.append(msg)
        self.msg_cache.setdefault(id(xtype), msg)  # before filling: allows recursion
        self._fill(msg, xtype)
        return msg

    def _message_ref(self, xtype, hint, parent):
        cached = self.msg_cache.get(id(xtype))
        if cached:
            return cached.name
        tname = getattr(xtype, "name", None)
        base = camel(local(tname)) if tname else camel(hint)
        name = self._claim(base, camel(f"{parent.name}_{hint}"))
        return self._build_message(xtype, name).name

    def _fill(self, msg, xtype):
        used: set[str] = set()

        def add(name, ptype, kind, repeated=False, comment=None):
            fname = snake(name)
            if norm(fname) in used:
                self.warn(f"{msg.name}: '{name}' collides with another field after "
                          "name normalisation; omitted")
                return
            used.add(norm(fname))
            msg.fields.append(_Field(fname, ptype, kind, repeated, comment))

        # A global element of simple type: wrap its text in a message.
        if not isinstance(xtype, XsdComplexType):
            ptype, kind, c = self._simple(xtype, "value", msg)
            add("value", ptype, kind, comment=c)
            return

        for aname, attr in _declared_attributes(xtype):
            ptype, kind, c = self._simple(attr.type, aname, msg)
            add(aname, ptype, kind, comment=", ".join(x for x in ("attribute", c) if x))

        content = xtype.content
        if isinstance(content, XsdSimpleType):  # text + attributes
            tname = next((n for n in TEXT_FIELDS if norm(n) not in used), "value")
            ptype, kind, c = self._simple(content, tname, msg)
            add(tname, ptype, kind, comment=", ".join(x for x in ("element text", c) if x))
        elif hasattr(content, "iter_elements"):
            children: dict[str, list] = {}
            for el in content.iter_elements():
                if not getattr(el, "name", None):  # xs:any wildcard
                    continue
                n = local(el.name)
                rep = _el_repeated(el)
                if n in children:
                    children[n][1] = children[n][1] or rep
                else:
                    children[n] = [el, rep]
            for n, (el, rep) in children.items():
                et = el.type
                if isinstance(et, XsdComplexType) and not self._is_plain_text(et):
                    add(n, self._message_ref(et, n, msg), "message", rep)
                else:
                    st = et.content if isinstance(et, XsdComplexType) else et
                    ptype, kind, c = self._simple(st, n, msg)
                    add(n, ptype, kind, rep, c)

    @staticmethod
    def _is_plain_text(ctype) -> bool:
        """Complex type with simple content and no attributes decodes to a bare value."""
        return isinstance(ctype.content, XsdSimpleType) and not _declared_attributes(ctype)

    # ---- simple types ----------------------------------------------------
    def _simple(self, st, hint, parent):
        """-> (proto type, kind, comment) for an XSD simple type."""
        if st is None:
            return "string", "scalar", None
        if _check(st, "is_list"):
            return "string", "scalar", "xs:list (space-separated)"
        if _check(st, "is_union"):
            return "string", "scalar", "xs:union"
        b = builtin_name(st)
        ptype, kind, comment = self._builtin(b)
        vals = enum_values(st)
        if vals and ptype == "string":
            return self._enum_ref(st, vals, hint, parent), "enum", None
        return ptype, kind, comment

    def _builtin(self, b):
        if b is None or b == "string":
            return "string", "scalar", None
        c = f"xs:{b}"
        if b in _INT_MAP:
            return _INT_MAP[b], "scalar", c
        if b == "boolean":
            return "bool", "scalar", None
        if b in ("float", "double"):
            return b, "scalar", None
        if b == "decimal":
            return ("double" if self.decimal == "double" else "string"), "scalar", c
        if b in ("dateTime", "dateTimeStamp") and self.datetime_mode == "timestamp":
            self.uses_timestamp = True
            return "google.protobuf.Timestamp", "message", c
        if b in ("base64Binary", "hexBinary"):
            return "bytes", "scalar", c
        return "string", "scalar", c

    def _enum_ref(self, st, vals, hint, parent):
        cached = self.enum_cache.get(id(st))
        if cached:
            return cached.name
        tname = getattr(st, "name", None)
        base = camel(local(tname)) if tname else camel(hint)
        name = self._claim(base, camel(f"{parent.name}_{hint}"))
        prefix = snake(name).upper()
        en = _Enum(name)
        en.values.append((self._enum_value(f"{prefix}_UNSPECIFIED"), 0))
        for i, v in enumerate(vals, 1):
            token = re.sub(r"[^0-9A-Za-z]+", "_", v).strip("_").upper() or "EMPTY"
            en.values.append((self._enum_value(f"{prefix}_{token}"), i))
        self.enums.append(en)
        self.enum_cache[id(st)] = en
        return name

    # ---- output ----------------------------------------------------------
    def render(self, source, elements) -> str:
        out = [f"// Generated by xml2pb.py from {source}",
               f"// Top-level elements: {', '.join(elements)}",
               "// Field numbers follow XSD order. Review before reusing this file for",
               "// data that is already serialized, and if the XSD later changes.",
               "", 'syntax = "proto3";', ""]
        if self.package:
            out += [f"package {self.package};", ""]
        if self.uses_timestamp:
            out += ['import "google/protobuf/timestamp.proto";', ""]
        for m in self.messages:
            if not m.fields:
                out += [f"message {m.name} {{}}", ""]
                continue
            out.append(f"message {m.name} {{")
            for i, f in enumerate(m.fields, 1):
                if f.repeated:
                    label = "repeated "
                elif self.use_optional and f.kind != "message":
                    label = "optional "  # keeps "absent" distinct from 0 / ""
                else:
                    label = ""
                c = f"  // {f.comment}" if f.comment else ""
                out.append(f"  {label}{f.ptype} {f.name} = {i};{c}")
            out += ["}", ""]
        for e in self.enums:
            out.append(f"enum {e.name} {{")
            out += [f"  {n} = {num};" for n, num in e.values]
            out += ["}", ""]
        return "\n".join(out).rstrip() + "\n"


def generate_proto(xsd, elements, package="", decimal="double",
                   datetime_mode="timestamp", use_optional=True) -> str:
    schema = xmlschema.XMLSchema(xsd)
    gen = ProtoGenerator(schema, package, decimal, datetime_mode, use_optional)
    return gen.build(elements, Path(xsd).name)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def convert(xsd, proto, xml, fmt="binary", message=None, includes=(),
            validate=True, strict=False, verbose=False) -> bytes | str:
    pool, file_desc = load_proto(proto, list(includes))
    schema = xmlschema.XMLSchema(xsd)

    root_tag = local(ET.parse(xml).getroot().tag)
    desc = pick_message(pool, file_desc, root_tag, message)

    # Structural check: proto must be a subset of the XSD
    xel = next((e for n, e in schema.elements.items() if local(n) == root_tag), None)
    if xel is None:
        raise ConversionError(f"Root element <{root_tag}> is not declared in the XSD")
    issues: list[str] = []
    check_subset(desc, xel, desc.name, issues, set())
    if issues:
        text = "\n  ".join(issues)
        if strict:
            raise ConversionError(f"Proto is not a subset of the XSD:\n  {text}")
        print(f"warning: proto fields without XSD counterpart:\n  {text}", file=sys.stderr)

    # Validate + typed decode
    try:
        if validate:
            data = schema.to_dict(xml, validation="strict", use_defaults=True)
        else:
            data, errs = schema.to_dict(xml, validation="lax", use_defaults=True)
            for e in errs:
                print(f"warning: {e.reason}", file=sys.stderr)
    except xmlschema.XMLSchemaValidationError as e:
        raise ConversionError(f"XML is not valid against the XSD:\n{e}") from None

    msg = message_class(pool, desc)()
    ctx = Ctx()
    fill(msg, data, desc.name, ctx)

    missing = find_missing_required(msg)  # proto2 'required' fields
    if missing:
        raise ConversionError(f"Required proto fields not populated: {', '.join(missing)}")

    if ctx.skipped:
        print(f"note: {len(ctx.skipped)} XML node(s) not in proto were skipped"
              + (":\n  " + "\n  ".join(ctx.skipped) if verbose else " (use -v to list)"),
              file=sys.stderr)

    if fmt == "text":
        return text_format.MessageToString(msg, as_utf8=True)
    return msg.SerializeToString(deterministic=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Convert XML to protobuf using an XSD and a .proto, "
                    "or generate a .proto from an XSD with --elements.")
    ap.add_argument("xsd")
    ap.add_argument("proto", nargs="?", help="(convert mode) the .proto file")
    ap.add_argument("xml", nargs="?", help="(convert mode) the XML document")
    ap.add_argument("-o", "--output",
                    help="output file. Convert: <xml>.pb/.txtpb. Generate: <xsd>.proto ('-' = stdout)")

    gen = ap.add_argument_group("generate a .proto from the XSD (pass only the XSD)")
    gen.add_argument("-e", "--elements", action="append",
                     help="comma-separated top-level XSD elements to generate, e.g. a,b,c")
    gen.add_argument("--package", default="", help="proto package for the generated file")
    gen.add_argument("--decimal", choices=["double", "string"], default="double",
                     help="proto type for xs:decimal (string is exact; default: double)")
    gen.add_argument("--datetime", dest="datetime_mode", choices=["timestamp", "string"],
                     default="timestamp", help="proto type for xs:dateTime (default: timestamp)")
    gen.add_argument("--no-optional", action="store_true",
                     help="omit proto3 'optional' on singular scalar fields")

    ap.add_argument("-f", "--format", choices=["binary", "text"], default="binary")
    ap.add_argument("-m", "--message", help="fully-qualified root message (default: match root element name)")
    ap.add_argument("-I", "--include", action="append", default=[], help="extra proto import path")
    ap.add_argument("--no-validate", action="store_true", help="don't fail on XSD validation errors")
    ap.add_argument("--strict", action="store_true", help="treat proto fields missing from the XSD as errors")
    ap.add_argument("-v", "--verbose", action="store_true", help="list skipped XML nodes")
    a = ap.parse_args(argv)

    try:
        if a.elements:  # ---- generate mode ----
            if a.proto or a.xml:
                ap.error("--elements generates a .proto from the XSD alone; "
                         "omit the proto and xml arguments")
            names = [n.strip() for chunk in a.elements for n in chunk.split(",") if n.strip()]
            text = generate_proto(a.xsd, names, a.package, a.decimal,
                                  a.datetime_mode, not a.no_optional)
            if a.output == "-":
                sys.stdout.write(text)
                return 0
            dest = Path(a.output) if a.output else Path(a.xsd).with_suffix(".proto")
            dest.write_text(text, encoding="utf-8")
            print(f"wrote {dest}", file=sys.stderr)
            return 0

        # ---- convert mode ----
        if not (a.proto and a.xml):
            ap.error("proto and xml are required (or use --elements to generate a .proto)")
        out = convert(a.xsd, a.proto, a.xml, a.format, a.message, a.include,
                      not a.no_validate, a.strict, a.verbose)
    except (ConversionError, xmlschema.XMLSchemaException) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    dest = Path(a.output) if a.output else Path(a.xml).with_suffix(
        ".txtpb" if a.format == "text" else ".pb")
    if isinstance(out, str):
        dest.write_text(out, encoding="utf-8")
    else:
        dest.write_bytes(out)
    print(f"wrote {dest}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
