"""XML -> protobuf conversion."""

from __future__ import annotations

import io
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any
from xml.etree import ElementTree

import xmlschema
from google.protobuf import text_format
from google.protobuf.descriptor import FieldDescriptor as FD
from google.protobuf.message import Message

from .errors import ConversionError
from .names import local, norm
from .protos import find_missing_required, is_map, is_repeated, load_proto, message_class
from .protos import pick_message as _pick_message
from .values import set_timestamp, to_scalar

TEXT_FIELDS = ("value", "text", "content", "body")  # proto field holding element text
WKT = "google.protobuf."
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
FORMATS = ("binary", "text")

XmlSource = str | Path | bytes | IO[bytes]


# --------------------------------------------------------------------------- loading


def load_schema(xsd: str | Path) -> Any:
    """Load and build an XSD, turning xmlschema errors into :class:`ConversionError`."""
    try:
        return xmlschema.XMLSchema(str(xsd))
    except (xmlschema.XMLSchemaException, OSError) as e:
        raise ConversionError(f"Could not load XSD {xsd}: {e}") from None


def load_xml(xml: XmlSource) -> Any:
    """Parse an XML document (path, raw bytes or binary file object)."""
    src: Any = io.BytesIO(xml) if isinstance(xml, bytes) else xml
    if isinstance(src, Path):
        src = str(src)
    try:
        return xmlschema.XMLResource(src)
    except (xmlschema.XMLSchemaException, ElementTree.ParseError, OSError) as e:
        label = xml if isinstance(xml, (str, Path)) else "<input>"
        raise ConversionError(f"Could not read XML {label}: {e}") from None


def find_root_element(schema: Any, tag: str) -> Any:
    """The global XSD element declaring ``tag`` (``{ns}name`` or bare name)."""
    el = schema.maps.elements.get(tag)
    if el is not None:
        return el
    want = local(tag)
    return next((e for n, e in schema.elements.items() if local(n) == want), None)


# --------------------------------------------------------------------------- XSD helpers


def xsd_children(xtype: Any) -> dict[str, Any]:
    """Local name -> child element declaration for a complex type."""
    out: dict[str, Any] = {}
    content = getattr(xtype, "content", None)
    if content is not None and hasattr(content, "iter_elements"):
        for c in content.iter_elements():
            if getattr(c, "name", None):
                out.setdefault(local(c.name), c)
    return out


def xsd_attributes(xtype: Any) -> dict[str, Any]:
    """Local name -> attribute declaration for a type (empty for simple types)."""
    attrs = getattr(xtype, "attributes", None) or {}
    return {local(k): a for k, a in attrs.items() if k}


def xsd_repeated(node: Any) -> bool:
    """True if an XSD element may occur more than once (attributes never do).

    Uses xmlschema's ``is_single()``, which also accounts for repeating parent
    groups (an element inside ``<xs:sequence maxOccurs="unbounded">``).
    """
    if node is None or not hasattr(node, "max_occurs"):
        return False
    is_single = getattr(node, "is_single", None)
    if callable(is_single):
        return not is_single()
    mo = node.max_occurs  # pragma: no cover - very old xmlschema
    return mo is None or mo > 1  # pragma: no cover


# --------------------------------------------------------------------------- subset check


def check_subset(desc: Any, xel: Any, path: str, issues: list[str], seen: set) -> None:
    """Append to ``issues`` every proto field with no counterpart in the XSD."""
    sig = (desc.full_name, id(xel))
    if sig in seen:
        return
    seen.add(sig)

    children = {norm(k): v for k, v in xsd_children(xel.type).items()}
    attrs = {norm(local(a)) for a in xel.attributes if a}
    has_simple = getattr(xel.type, "has_simple_content", None)
    simple = bool(has_simple()) if callable(has_simple) else False

    for f in desc.fields:
        keys = {norm(f.name), norm(f.json_name)}
        child = next((children[k] for k in keys if k in children), None)
        if child is not None:
            mt = f.message_type
            if mt is not None and not is_map(f) and not mt.full_name.startswith(WKT):
                check_subset(mt, child, f"{path}.{f.name}", issues, seen)
        elif keys & attrs:
            continue
        elif simple and norm(f.name) in TEXT_FIELDS:
            continue
        else:
            issues.append(f"{path}.{f.name}: no matching element/attribute in XSD")


# --------------------------------------------------------------------------- filling


@dataclass
class FillContext:
    """Collects XML nodes that had no proto field while filling a message."""

    skipped: list[str] = field(default_factory=list)


def _is_ignored_key(raw_key: object) -> bool:
    rk = str(raw_key).lstrip("@")
    return rk.startswith(("xmlns", "xsi:", "{" + XSI_NS + "}"))


def _nil_to_none(v: Any) -> Any:
    """``xsi:nil`` elements decode to ``{'@xsi:nil': 'true'}``; treat them as ``None``."""
    if isinstance(v, dict) and v and all(_is_ignored_key(k) for k in v):
        return None
    return v


def fill(msg: Any, data: Any, path: str, ctx: FillContext, xtype: Any = None) -> None:
    """Copy decoded XML ``data`` into ``msg``.

    ``xtype`` is the XSD type of the element being filled. It is optional but lets
    the filler tell an ``xs:list`` value apart from a repeated element (both decode
    to Python lists).
    """
    desc = msg.DESCRIPTOR
    if desc.full_name == WKT + "Timestamp":
        set_timestamp(msg, data, path)
        return
    if data is None:
        return
    if not isinstance(data, dict):
        data = {"$": data}

    by_name: dict[str, Any] = {}
    for f in desc.fields:
        by_name.setdefault(norm(f.name), f)
        by_name.setdefault(norm(f.json_name), f)
    children = xsd_children(xtype)
    attrs = xsd_attributes(xtype)

    for raw_key, val in data.items():
        if _is_ignored_key(raw_key):
            continue
        if str(raw_key) == "$":
            f = next((by_name[t] for t in TEXT_FIELDS if t in by_name), None)
            label, node = "#text", None
        else:
            label = local(raw_key)
            f = by_name.get(norm(label))
            node = (attrs if str(raw_key).startswith("@") else children).get(label)
        if f is None:
            ctx.skipped.append(f"{path}/{label}")
            continue
        set_field(msg, f, val, f"{path}/{label}", ctx, node)


def set_field(msg: Any, f: Any, val: Any, path: str, ctx: FillContext, node: Any = None) -> None:
    """Set proto field ``f`` from the decoded value(s) of one XML element/attribute.

    ``node`` is the XSD element/attribute declaration, if known.
    """
    val = _nil_to_none(val)
    if val is None:  # empty / xsi:nil element
        if f.cpp_type == FD.CPPTYPE_MESSAGE and not is_repeated(f):
            getattr(msg, f.name).SetInParent()
        return

    # A list is several occurrences of a repeated element, unless the XSD says the
    # element occurs once, in which case it is one xs:list value.
    if isinstance(val, list) and (node is None or xsd_repeated(node)):
        items = val
    else:
        items = [val]
    child_type = getattr(node, "type", None)

    if is_map(f):
        fill_map(msg, f, items, path, ctx, child_type)
        return

    if not is_repeated(f):
        if len(items) > 1:
            raise ConversionError(
                f"{path}: XML has {len(items)} occurrences but proto field "
                f"'{f.name}' is not repeated (data would be lost)"
            )
        item = _nil_to_none(items[0])
        if f.cpp_type == FD.CPPTYPE_MESSAGE:
            sub = getattr(msg, f.name)
            sub.SetInParent()
            fill(sub, item, path, ctx, child_type)
        elif item is not None:
            setattr(msg, f.name, to_scalar(f, item, path))
        return

    container = getattr(msg, f.name)
    if (
        f.cpp_type != FD.CPPTYPE_MESSAGE
        and len(items) == 1
        and isinstance(items[0], list)
        and not xsd_repeated(node)
    ):
        items = items[0]  # one xs:list value feeding a repeated scalar field
    for i, item in enumerate(items):
        p = f"{path}[{i}]"
        item = _nil_to_none(item)
        if f.cpp_type == FD.CPPTYPE_MESSAGE:
            fill(container.add(), item, p, ctx, child_type)
        elif item is not None:  # a nil occurrence has no scalar representation
            container.append(to_scalar(f, item, p))


def fill_map(
    msg: Any, f: Any, items: list, path: str, ctx: FillContext, entry_type: Any = None
) -> None:
    """Fill a ``map<K, V>`` field from entry elements with ``<key>``/``<value>`` children."""
    kf = f.message_type.fields_by_name["key"]
    vf = f.message_type.fields_by_name["value"]
    value_type = getattr(xsd_children(entry_type).get("value"), "type", None)
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
            fill(target[key], d.get("value"), p + "/value", ctx, value_type)
        elif d.get("value") is not None:
            target[key] = to_scalar(vf, d["value"], p + "/value")


# --------------------------------------------------------------------------- top level


def serialize(msg: Message, fmt: str) -> bytes:
    """Serialize as ``"text"`` (UTF-8 protobuf text format) or deterministic ``"binary"``."""
    if fmt == "text":
        return text_format.MessageToString(msg, as_utf8=True).encode("utf-8")
    if fmt == "binary":
        return msg.SerializeToString(deterministic=True)
    raise ConversionError(f"Unknown format: {fmt!r} (expected one of {FORMATS})")


@dataclass
class Conversion:
    """The result of :func:`convert`."""

    message: Any
    """The populated protobuf message (a :class:`google.protobuf.message.Message`).

    Typed as ``Any`` because its class is built at runtime from the .proto, so its
    fields are unknown to type checkers; ``result.message.order_id`` type-checks.
    """
    skipped: list[str] = field(default_factory=list)
    """XML nodes with no matching proto field (expected: the proto is a subset)."""
    subset_issues: list[str] = field(default_factory=list)
    """Proto fields with no counterpart in the XSD (errors with ``strict=True``)."""
    validation_errors: list[str] = field(default_factory=list)
    """XSD validation problems that were tolerated (only with ``validate=False``)."""

    def serialize(self, fmt: str = "binary") -> bytes:
        return serialize(self.message, fmt)


def convert(
    xsd: str | Path,
    proto: str | Path,
    xml: XmlSource,
    *,
    message: str | None = None,
    includes: Iterable[str | Path] = (),
    validate: bool = True,
    strict: bool = False,
) -> Conversion:
    """Convert an XML document to a protobuf message.

    1. Compile ``proto``; load ``xsd``; parse ``xml``.
    2. Pick the message (``message``, else the one named like the root element).
    3. Check that every proto field exists in the XSD (``strict`` makes gaps fatal).
    4. Validate and type-decode the XML (``validate=False`` tolerates errors).
    5. Copy matching values into the message; XML content without a proto field
       is skipped and reported in :attr:`Conversion.skipped`.
    """
    pool, file_desc, names = load_proto(proto, includes)
    schema = load_schema(xsd)
    resource = load_xml(xml)

    root_tag = str(resource.root.tag)
    desc = _pick_message(pool, file_desc, local(root_tag), message, names)

    xel = find_root_element(schema, root_tag)
    if xel is None:
        raise ConversionError(f"Root element <{local(root_tag)}> is not declared in the XSD")
    issues: list[str] = []
    check_subset(desc, xel, desc.name, issues, set())
    if issues and strict:
        raise ConversionError("Proto is not a subset of the XSD:\n  " + "\n  ".join(issues))

    errors: list[str] = []
    opts: dict[str, Any] = {"use_defaults": True, "binary_types": True}
    try:
        if validate:
            data = schema.to_dict(resource, validation="strict", **opts)
        else:
            data, errs = schema.to_dict(resource, validation="lax", **opts)
            errors = [str(getattr(e, "reason", None) or e) for e in errs]
    except xmlschema.XMLSchemaValidationError as e:
        raise ConversionError(f"XML is not valid against the XSD:\n{e}") from None

    msg: Any = message_class(pool, desc)()
    ctx = FillContext()
    fill(msg, data, desc.name, ctx, xel.type)

    missing = find_missing_required(msg)  # proto2 'required' fields
    if missing:
        raise ConversionError(f"Required proto fields not populated: {', '.join(missing)}")
    return Conversion(msg, ctx.skipped, issues, errors)
