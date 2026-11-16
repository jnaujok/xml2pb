"""Command-line interface for xml2pb."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .converter import FORMATS, convert
from .errors import ConversionError
from .generator import DATETIME_MODES, DECIMAL_MODES, generate_proto

EPILOG = """\
convert (XSD + .proto + XML -> protobuf):
  xml2pb schema.xsd messages.proto input.xml                   # -> input.pb
  xml2pb schema.xsd messages.proto input.xml -f text -o out.txtpb
  xml2pb schema.xsd messages.proto input.xml -m Order -I protos/
  cat input.xml | xml2pb schema.xsd messages.proto - -f text -o -
  xml2pb schema.xsd messages.proto input.xml --envelope Envelope   # root goes in Envelope

generate (XSD -> .proto; pass only the XSD):
  xml2pb schema.xsd --elements order -o messages.proto
  xml2pb schema.xsd -e order,invoice --package acme.v1 --decimal string -o -
  xml2pb schema.xsd -e order,invoice --envelope Envelope -o messages.proto
"""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="xml2pb",
        description="Convert XML to protobuf using an XSD and a .proto, "
        "or generate a .proto from an XSD with --elements.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("xsd", help="XSD schema for the XML")
    ap.add_argument("proto", nargs="?", help="(convert) the .proto file; a subset of the XSD")
    ap.add_argument("xml", nargs="?", help="(convert) the XML document; '-' = stdin")
    ap.add_argument(
        "-o",
        "--output",
        help="output file, '-' = stdout. Default: convert -> <xml>.pb / .txtpb; "
        "generate -> <xsd>.proto",
    )

    conv = ap.add_argument_group("convert options")
    conv.add_argument(
        "-f", "--format", choices=FORMATS, default="binary", help="output format (default: binary)"
    )
    conv.add_argument(
        "-m",
        "--message",
        help="root message, e.g. acme.v1.Order or just Order (default: match the root element)",
    )
    ap.add_argument(
        "-E",
        "--envelope",
        metavar="NAME",
        help="convert: wrap the result in this enclosing message (the root element fills "
        "the envelope field that matches it). generate: also emit an enclosing message "
        "of this name holding one of the generated messages",
    )
    conv.add_argument(
        "-I", "--include", action="append", default=[], help="extra proto import path (repeatable)"
    )
    conv.add_argument(
        "--no-validate",
        action="store_true",
        help="don't fail on XSD validation errors (report them as warnings)",
    )
    conv.add_argument(
        "--strict", action="store_true", help="treat proto fields missing from the XSD as errors"
    )
    conv.add_argument("-v", "--verbose", action="store_true", help="list skipped XML nodes")

    gen = ap.add_argument_group("generate a .proto from the XSD (pass only the XSD)")
    gen.add_argument(
        "-e",
        "--elements",
        action="append",
        help="comma-separated top-level XSD elements to generate, e.g. a,b,c (repeatable)",
    )
    gen.add_argument("--package", default="", help="proto package for the generated file")
    gen.add_argument(
        "--decimal",
        choices=DECIMAL_MODES,
        default="double",
        help="proto type for xs:decimal (string is exact; default: double)",
    )
    gen.add_argument(
        "--datetime",
        dest="datetime_mode",
        choices=DATETIME_MODES,
        default="timestamp",
        help="proto type for xs:dateTime (default: timestamp)",
    )
    gen.add_argument(
        "--no-optional",
        action="store_true",
        help="omit proto3 'optional' on singular scalar fields",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return ap


def _write(dest: str, data: bytes) -> None:
    if dest == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    else:
        Path(dest).write_bytes(data)
        print(f"wrote {dest}", file=sys.stderr)


def _generate(ap: argparse.ArgumentParser, a: argparse.Namespace) -> int:
    if a.proto or a.xml:
        ap.error("--elements generates a .proto from the XSD alone; omit the proto and xml")
    names = [n.strip() for chunk in a.elements for n in chunk.split(",") if n.strip()]
    if not names:
        ap.error("--elements needs at least one element name")
    text = generate_proto(
        a.xsd,
        names,
        a.package,
        a.decimal,
        a.datetime_mode,
        not a.no_optional,
        envelope=a.envelope,
    )
    dest = a.output or str(Path(a.xsd).with_suffix(".proto"))
    if dest != "-" and Path(dest).resolve() == Path(a.xsd).resolve():
        raise ConversionError("output would overwrite the XSD; use -o")
    _write(dest, text.encode("utf-8"))
    return 0


def _convert(ap: argparse.ArgumentParser, a: argparse.Namespace) -> int:
    if not (a.proto and a.xml):
        ap.error("proto and xml are required (or use --elements to generate a .proto)")
    from_stdin = a.xml == "-"
    if from_stdin and not a.output:
        ap.error("reading XML from stdin requires -o/--output")

    xml = sys.stdin.buffer.read() if from_stdin else a.xml
    result = convert(
        a.xsd,
        a.proto,
        xml,
        message=a.message,
        envelope=a.envelope,
        includes=a.include,
        validate=not a.no_validate,
        strict=a.strict,
    )
    for err in result.validation_errors:
        print(f"warning: {err}", file=sys.stderr)
    if result.subset_issues:
        print(
            "warning: proto fields without XSD counterpart:\n  "
            + "\n  ".join(result.subset_issues),
            file=sys.stderr,
        )
    if result.skipped:
        detail = ":\n  " + "\n  ".join(result.skipped) if a.verbose else " (use -v to list)"
        print(
            f"note: {len(result.skipped)} XML node(s) not in proto were skipped{detail}",
            file=sys.stderr,
        )

    if a.output:
        dest = a.output
    else:
        dest = str(Path(a.xml).with_suffix(".txtpb" if a.format == "text" else ".pb"))
    if not from_stdin and dest != "-" and Path(dest).resolve() == Path(a.xml).resolve():
        raise ConversionError("output would overwrite the input XML; use -o")
    _write(dest, result.serialize(a.format))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    try:
        return _generate(ap, a) if a.elements else _convert(ap, a)
    except ConversionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
