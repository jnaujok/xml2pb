"""Compile .proto files and work with their descriptors."""

from __future__ import annotations

import importlib.resources as ir
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.descriptor import FieldDescriptor as FD
from google.protobuf.message import Message

from .errors import ConversionError
from .names import norm

# --------------------------------------------------------------------------- fields


def is_repeated(f: Any) -> bool:
    """True for repeated (including map) fields, across protobuf versions."""
    r = getattr(f, "is_repeated", None)
    return bool(r) if r is not None else f.label == FD.LABEL_REPEATED


def is_required(f: Any) -> bool:
    """True for proto2 ``required`` fields, across protobuf versions."""
    r = getattr(f, "is_required", None)
    return bool(r) if r is not None else f.label == FD.LABEL_REQUIRED


def is_map(f: Any) -> bool:
    """True for ``map<K, V>`` fields (repeated synthetic ``*Entry`` messages)."""
    return f.message_type is not None and f.message_type.GetOptions().map_entry


# --------------------------------------------------------------------------- loading


def load_proto(proto_file: str | Path, includes: Iterable[str | Path] = ()):
    """Compile ``proto_file`` (in-process, via grpcio-tools) into a private pool.

    Returns ``(pool, file_descriptor, file_names)`` where ``file_names`` lists every
    compiled file (imports first, the requested file last).
    """
    proto_path = Path(proto_file).resolve()
    if not proto_path.is_file():
        raise ConversionError(f"Proto file not found: {proto_file}")
    from grpc_tools import protoc  # deferred: slow import, only needed here

    wkt_dir = str(ir.files("grpc_tools") / "_proto")  # google/protobuf/*.proto

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "desc.bin"
        args = [
            "protoc",
            f"-I{proto_path.parent}",
            *[f"-I{i}" for i in includes],
            f"-I{wkt_dir}",
            f"--descriptor_set_out={out}",
            "--include_imports",
            str(proto_path),
        ]
        if protoc.main(args) != 0:
            raise ConversionError("protoc failed to compile the .proto file (see above)")
        fds = descriptor_pb2.FileDescriptorSet.FromString(out.read_bytes())

    pool = descriptor_pool.DescriptorPool()
    for fdp in fds.file:  # topologically sorted by protoc
        pool.AddSerializedFile(fdp.SerializeToString())
    names = [f.name for f in fds.file]
    return pool, pool.FindFileByName(names[-1]), names


def message_class(pool, desc) -> type[Message]:
    """Build a concrete message class for ``desc``.

    ``pool`` is accepted for API symmetry; the class is built from the descriptor,
    which already belongs to its pool.
    """
    return message_factory.GetMessageClass(desc)


# --------------------------------------------------------------------------- lookup


def _walk_messages(descs) -> Iterator:
    for d in descs:
        yield d
        yield from _walk_messages(d.nested_types)


def all_messages(pool, file_names: Iterable[str]) -> list:
    """Every message (top-level and nested) defined in the given files."""
    out = []
    for name in file_names:
        out.extend(_walk_messages(pool.FindFileByName(name).message_types_by_name.values()))
    return out


def find_message(pool, file_desc, name: str, all_files: Iterable[str] = ()):
    """Resolve a message name given by the user.

    Accepts the fully-qualified name (``acme.v1.Order``), with or without a leading
    dot, or any unambiguous dotted suffix of it (``Order``, ``v1.Order``,
    ``Order.Line``). The requested file is searched first, then its imports.
    """
    want = name.strip().lstrip(".")
    try:
        return pool.FindMessageTypeByName(want)
    except KeyError:
        pass
    own = all_messages(pool, [file_desc.name])
    for candidates in (own, all_messages(pool, all_files)):
        hits = [d for d in candidates if d.full_name == want or d.full_name.endswith("." + want)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise ConversionError(
                f"Message '{name}' is ambiguous; use one of: {', '.join(d.full_name for d in hits)}"
            )
    names = ", ".join(d.full_name for d in own) or "(none)"
    raise ConversionError(f"Message '{name}' not found. Available: {names}")


def pick_message(
    pool, file_desc, root_name: str, explicit: str | None, all_files: Iterable[str] = ()
):
    """Choose the message for the XML root element.

    With ``explicit`` (``--message``), resolve it as in :func:`find_message`.
    Otherwise use the top-level message whose name matches ``root_name`` (ignoring
    case and separators), or the only top-level message if there is just one.
    """
    if explicit:
        return find_message(pool, file_desc, explicit, all_files)
    msgs = list(file_desc.message_types_by_name.values())
    for d in msgs:
        if norm(d.name) == norm(root_name):
            return d
    if len(msgs) == 1:
        return msgs[0]
    raise ConversionError(
        f"Can't tell which message maps to root element <{root_name}>; use --message. "
        f"Available: {', '.join(d.full_name for d in msgs) or '(none)'}"
    )


def find_envelope_field(envelope, root_name: str, inner=None):
    """The field of ``envelope`` that should hold the converted root element.

    Tried in order: a message field named like ``root_name`` (ignoring case and
    separators, JSON name included), then a message field whose *type* is named
    like ``root_name``. With ``inner`` (an explicit ``--message``), only fields of
    that type are considered, and a single such field is used whatever its name.
    Map fields are never candidates.
    """
    cands = [f for f in envelope.fields if f.message_type is not None and not is_map(f)]
    if inner is not None:
        cands = [f for f in cands if f.message_type.full_name == inner.full_name]
    want = norm(root_name)
    by_name = [f for f in cands if want in (norm(f.name), norm(f.json_name))]
    if by_name:
        return by_name[0]  # field names are unique after protoc's own checks
    by_type = [f for f in cands if norm(f.message_type.name) == want]
    if len(by_type) == 1:
        return by_type[0]
    if inner is not None and len(cands) == 1:
        return cands[0]
    if len(by_type) > 1 or (inner is not None and cands):
        hits = by_type if len(by_type) > 1 else cands
        raise ConversionError(
            f"Envelope {envelope.full_name} has several fields that could hold "
            f"<{root_name}>: {', '.join(f.name for f in hits)}; rename one to match "
            "the root element"
        )
    fields = [
        f"{f.name} ({f.message_type.full_name})"
        for f in envelope.fields
        if f.message_type is not None and not is_map(f)
    ]
    what = f"of type {inner.full_name} " if inner is not None else ""
    raise ConversionError(
        f"Envelope {envelope.full_name} has no message field {what}for root element "
        f"<{root_name}>. Message fields: {', '.join(fields) or '(none)'}"
    )


# --------------------------------------------------------------------------- checks


def find_missing_required(msg: Any, path: str = "") -> list[str]:
    """Paths of unset proto2 ``required`` fields, recursing into set sub-messages."""
    missing: list[str] = []
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
