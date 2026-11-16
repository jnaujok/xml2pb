"""xml2pb - convert XSD-described XML documents to protobuf messages."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .converter import Conversion, convert, load_schema, load_xml, serialize
from .errors import ConversionError
from .generator import ProtoGenerator, generate_proto
from .protos import find_message, load_proto, message_class, pick_message
from .values import parse_datetime

try:
    __version__ = version("xml2pb")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"

__all__ = [
    "Conversion",
    "ConversionError",
    "ProtoGenerator",
    "__version__",
    "convert",
    "find_message",
    "generate_proto",
    "load_proto",
    "load_schema",
    "load_xml",
    "message_class",
    "parse_datetime",
    "pick_message",
    "serialize",
]
