"""Generate a proto3 schema from chosen top-level XSD elements."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xmlschema.validators import XsdComplexType, XsdSimpleType

from .converter import TEXT_FIELDS, load_schema, xsd_repeated
from .errors import ConversionError
from .names import camel, local, norm, snake

XSD_NS = "http://www.w3.org/2001/XMLSchema"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
DECIMAL_MODES = ("double", "string")
DATETIME_MODES = ("timestamp", "string")

INT_MAP = {
    "int": "int32",
    "short": "int32",
    "byte": "int32",
    "long": "int64",
    "integer": "int64",
    "nonPositiveInteger": "int64",
    "negativeInteger": "int64",
    "unsignedInt": "uint32",
    "unsignedShort": "uint32",
    "unsignedByte": "uint32",
    "unsignedLong": "uint64",
    "nonNegativeInteger": "uint64",
    "positiveInteger": "uint64",
}


def builtin_name(st: Any) -> str | None:
    """Walk up the type derivation chain to the first built-in ``xs:`` type."""
    prefix = f"{{{XSD_NS}}}"
    t = st
    for _ in range(50):
        if t is None:
            return None
        n = getattr(t, "name", None)
        if n and n.startswith(prefix):
            return n[len(prefix) :]
        t = getattr(t, "base_type", None)
    return None


def enum_values(st: Any) -> list[str] | None:
    """The ``xs:enumeration`` values of a simple type, de-duplicated, in order."""
    vals = getattr(st, "enumeration", None)
    if vals is None:
        facet = (getattr(st, "facets", None) or {}).get(f"{{{XSD_NS}}}enumeration")
        vals = getattr(facet, "enumeration", None)
    if not vals:
        return None
    return list(dict.fromkeys(str(v) for v in vals))


def _check(obj: Any, method: str) -> bool:
    fn = getattr(obj, method, None)
    return bool(fn()) if callable(fn) else False


def declared_attributes(ctype: Any) -> list[tuple[str, Any]]:
    """``(local name, attribute)`` pairs, skipping ``anyAttribute`` and ``xsi:*``."""
    out = []
    for qn, attr in ctype.attributes.items():
        if not qn or qn.startswith(f"{{{XSI_NS}}}"):
            continue
        out.append((local(qn), attr))
    return out


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
    fields: list[_Field] = field(default_factory=list)
    oneof: str | None = None  # render all fields inside this oneof (envelopes)


@dataclass
class _Enum:
    name: str
    values: list[tuple[str, int]] = field(default_factory=list)


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _oneof_names():
    yield "payload"
    i = 2
    while True:
        yield f"payload{i}"
        i += 1


def _stderr_warning(text: str) -> None:
    print(f"warning: {text}", file=sys.stderr)


class ProtoGenerator:
    """Build a proto3 file for chosen top-level XSD elements.

    Naming is chosen so the result feeds straight back into the converter: the
    message for ``<order>`` is ``Order``, fields are snake_case of the XSD names,
    and enum values are ``PREFIXED_UPPER_SNAKE`` (the converter's tolerant matching
    maps XML values like ``in-progress`` to ``STATUS_IN_PROGRESS``).
    """

    def __init__(
        self,
        schema: Any,
        package: str = "",
        decimal: str = "double",
        datetime_mode: str = "timestamp",
        use_optional: bool = True,
        warn: Callable[[str], None] | None = None,
    ):
        if decimal not in DECIMAL_MODES:
            raise ConversionError(f"decimal must be one of {DECIMAL_MODES}, not {decimal!r}")
        if datetime_mode not in DATETIME_MODES:
            raise ConversionError(
                f"datetime_mode must be one of {DATETIME_MODES}, not {datetime_mode!r}"
            )
        self.schema = schema
        self.package = package
        self.decimal = decimal
        self.datetime_mode = datetime_mode
        self.use_optional = use_optional
        self.warn = warn or _stderr_warning
        self.names: set[str] = set()  # top-level message/enum names
        self.enum_value_names: set[str] = set()
        self.messages: list[_Msg] = []
        self.enums: list[_Enum] = []
        self.msg_cache: dict[int, _Msg] = {}
        self.enum_cache: dict[int, _Enum] = {}
        self.uses_timestamp = False

    # ---- naming ----------------------------------------------------------
    def _claim(self, base: str, alt: str | None = None) -> str:
        for cand in (base, alt):
            if cand and cand not in self.names:
                self.names.add(cand)
                return cand
        i = 2
        while f"{base}{i}" in self.names:
            i += 1
        self.names.add(f"{base}{i}")
        return f"{base}{i}"

    def _enum_value(self, base: str) -> str:
        cand, i = base, 2
        while cand in self.enum_value_names or cand in self.names:
            cand = f"{base}_{i}"
            i += 1
        self.enum_value_names.add(cand)
        return cand

    # ---- entry point -----------------------------------------------------
    def _find_element(self, name: str) -> Any:
        want = local(name)
        hits = [e for q, e in self.schema.elements.items() if local(q) == want]
        if not hits:  # tolerate case/separator differences
            hits = [e for q, e in self.schema.elements.items() if norm(local(q)) == norm(want)]
        if not hits:
            avail = ", ".join(sorted({local(q) for q in self.schema.elements})) or "(none)"
            raise ConversionError(
                f"Top-level element '{name}' not found in XSD. Available: {avail}"
            )
        if len(hits) > 1:
            self.warn(f"'{name}' matches {len(hits)} global elements; using the first")
        return hits[0]

    def build(self, names: Iterable[str], source: str, envelope: str | None = None) -> str:
        """Generate the .proto text for the given top-level element names.

        With ``envelope``, also emit a message of that name with one field per
        element, inside a ``oneof`` (an XML document has exactly one root).
        """
        if envelope is not None:
            if not _IDENT.fullmatch(envelope):
                raise ConversionError(f"Envelope name {envelope!r} is not a valid proto identifier")
            self.names.add(envelope)  # reserved before roots so nothing else takes it
        roots, seen = [], set()
        for n in names:
            el = self._find_element(n)
            ln = local(el.name)
            if ln in seen:
                continue
            seen.add(ln)
            if envelope is not None and camel(ln) == envelope:
                raise ConversionError(
                    f"Envelope name {envelope!r} clashes with the message for <{ln}>; "
                    "choose another name"
                )
            # Reserve every root name first so nested types can't steal them.
            roots.append((el, self._claim(camel(ln))))
        if not roots:
            raise ConversionError("No elements given to generate")
        env = None
        if envelope is not None:
            env = _Msg(envelope)
            self.messages.append(env)  # first: it is the entry point
        for el, mname in roots:
            self._build_message(el.type, mname)
        if env is not None:
            used: set[str] = set()
            for el, mname in roots:
                fname = snake(local(el.name))
                if fname in used:
                    self.warn(
                        f"{envelope}: '{local(el.name)}' collides with another field; omitted"
                    )
                    continue
                used.add(fname)
                env.fields.append(_Field(fname, mname, "message"))
            env.oneof = next(n for n in _oneof_names() if n not in used)
        return self.render(source, [local(e.name) for e, _ in roots], envelope)

    # ---- messages --------------------------------------------------------
    def _build_message(self, xtype: Any, name: str) -> _Msg:
        msg = _Msg(name)
        self.messages.append(msg)
        self.msg_cache.setdefault(id(xtype), msg)  # before filling: allows recursion
        self._fill(msg, xtype)
        return msg

    def _message_ref(self, xtype: Any, hint: str, parent: _Msg) -> str:
        cached = self.msg_cache.get(id(xtype))
        if cached:
            return cached.name
        tname = getattr(xtype, "name", None)
        base = camel(local(tname)) if tname else camel(hint)
        name = self._claim(base, camel(f"{parent.name}_{hint}"))
        return self._build_message(xtype, name).name

    def _fill(self, msg: _Msg, xtype: Any) -> None:
        used: set[str] = set()

        def add(name, ptype, kind, repeated=False, comment=None):
            fname = snake(name)
            if norm(fname) in used:
                self.warn(
                    f"{msg.name}: '{name}' collides with another field after "
                    "name normalisation; omitted"
                )
                return
            used.add(norm(fname))
            msg.fields.append(_Field(fname, ptype, kind, repeated, comment))

        # A global element of simple type: wrap its text in a message.
        if not isinstance(xtype, XsdComplexType):
            ptype, kind, c = self._simple(xtype, "value", msg)
            add("value", ptype, kind, comment=c)
            return

        for aname, attr in declared_attributes(xtype):
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
                rep = xsd_repeated(el)
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
    def _is_plain_text(ctype: Any) -> bool:
        """Complex type with simple content and no attributes decodes to a bare value."""
        return isinstance(ctype.content, XsdSimpleType) and not declared_attributes(ctype)

    # ---- simple types ----------------------------------------------------
    def _simple(self, st: Any, hint: str, parent: _Msg) -> tuple[str, str, str | None]:
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

    def _builtin(self, b: str | None) -> tuple[str, str, str | None]:
        if b is None or b == "string":
            return "string", "scalar", None
        c = f"xs:{b}"
        if b in INT_MAP:
            return INT_MAP[b], "scalar", c
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

    def _enum_ref(self, st: Any, vals: list[str], hint: str, parent: _Msg) -> str:
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
    def render(self, source: str, elements: list[str], envelope: str | None = None) -> str:
        out = [
            f"// Generated by xml2pb from {source}",
            f"// Top-level elements: {', '.join(elements)}",
            *([f"// Envelope: {envelope} (holds exactly one of the above)"] if envelope else []),
            "// Field numbers follow XSD order. Review before reusing this file for",
            "// data that is already serialized, and if the XSD later changes.",
            "",
            'syntax = "proto3";',
            "",
        ]
        if self.package:
            out += [f"package {self.package};", ""]
        if self.uses_timestamp:
            out += ['import "google/protobuf/timestamp.proto";', ""]
        for m in self.messages:
            if not m.fields:
                out += [f"message {m.name} {{}}", ""]
                continue
            out.append(f"message {m.name} {{")
            if m.oneof:
                out.append(f"  oneof {m.oneof} {{")
                out += [f"    {f.ptype} {f.name} = {i};" for i, f in enumerate(m.fields, 1)]
                out += ["  }", "}", ""]
                continue
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


def generate_proto(
    xsd: str | Path,
    elements: Iterable[str],
    package: str = "",
    decimal: str = "double",
    datetime_mode: str = "timestamp",
    use_optional: bool = True,
    warn: Callable[[str], None] | None = None,
    envelope: str | None = None,
) -> str:
    """Generate proto3 source for the given top-level elements of ``xsd``.

    ``envelope`` adds an enclosing message of that name with a ``oneof`` holding
    one field per element. ``warn`` receives non-fatal warnings (default: print
    to stderr).
    """
    schema = load_schema(xsd)
    gen = ProtoGenerator(schema, package, decimal, datetime_mode, use_optional, warn)
    return gen.build(elements, Path(xsd).name, envelope)
