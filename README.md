# xml2pb

[![CI](https://github.com/jnaujok/xml2pb/actions/workflows/ci.yml/badge.svg)](https://github.com/jnaujok/xml2pb/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

Convert an XML document described by an **XSD** into a **protobuf** message
defined by a `.proto` file. The `.proto` only needs to cover the parts of the XML
you care about: it can be a *subset* of the XSD. xml2pb can also generate a
starting `.proto` from the XSD for you.

```console
$ xml2pb orders.xsd orders.proto order.xml -f text -o -
note: 2 XML node(s) not in proto were skipped (use -v to list)
order_id: 42
status: STATUS_IN_PROGRESS
placed_at {
  seconds: 1767323045
  nanos: 123000000
}
line {
  sku: "A1"
  qty: 2
  price {
    currency: "USD"
    value: 5.0
  }
}
...

$ xml2pb orders.xsd --elements order --package acme.v1 -o orders.proto
wrote orders.proto
```

**How it works**

1. Compiles the `.proto` in-process, using the `protoc` bundled with
   [`grpcio-tools`](https://pypi.org/project/grpcio-tools/). You don't need a
   separate `protoc` install.
2. Validates the XML against the XSD and decodes it with
   [`xmlschema`](https://pypi.org/project/xmlschema/). Decoding uses the schema's
   types, so `xs:int`, `xs:boolean`, `xs:decimal`, `xs:dateTime`,
   `xs:hexBinary` and so on arrive as real values, not strings.
3. Checks that every proto field has a counterpart in the XSD.
4. Copies matching values into the message. Names are matched ignoring case,
   underscores, hyphens and dots, so `order_id`, `orderId` and `OrderID` all
   match. XML content with no proto field is skipped and reported.
5. Writes binary wire format or protobuf text format.

**Highlights**

- **Lossless or loud.** Fractional values for integer fields, out-of-range
  integers, float32 overflow, unknown enum values, and several occurrences of an
  element mapped to a non-repeated field are errors, never silent truncation.
- **Schema-aware.** It knows the difference between an `xs:list` value and a
  repeated element, between hex and base64 binary, and between an attribute and
  a child element.
- **Friendly enums.** The XML value `in-progress` maps to the enum value
  `STATUS_IN_PROGRESS`.
- **Full-precision timestamps.** `xs:dateTime` becomes a
  `google.protobuf.Timestamp` with nanosecond precision, honouring the
  timezone offset.
- **Deterministic output.** The same input always gives the same bytes, maps
  included.
- **Library and CLI.** Everything the CLI does is available from Python.

---

## Contents

- [Installation](#installation)
- [Converting XML](#converting-xml)
- [Generating a .proto from an XSD](#generating-a-proto-from-an-xsd)
- [Envelope messages](#envelope-messages)
- [How XML maps onto protobuf](#how-xml-maps-onto-protobuf)
- [Python API](#python-api)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Installation

Requires **Python 3.10+**. Dependencies are installed automatically:
`xmlschema` 3.0+, `protobuf` 4.25+ and `grpcio-tools` 1.62+. CI tests both the
oldest supported versions and the latest ones.

```bash
pipx install git+https://github.com/jnaujok/xml2pb.git   # isolated CLI install
# or
pip install git+https://github.com/jnaujok/xml2pb.git
xml2pb --version
```

Every CI run also uploads the built wheel and sdist as the `dist` artifact.

From source:

```bash
git clone https://github.com/jnaujok/xml2pb.git
cd xml2pb
poetry install              # creates .venv with xml2pb and the dev tools
poetry run xml2pb --help
```

You can also run it as a module: `python -m xml2pb ...`.

## Converting XML

```text
xml2pb XSD PROTO XML [-o OUT] [-f binary|text] [-m MESSAGE] [-E ENVELOPE]
                     [-I DIR ...] [--no-validate] [--strict] [-v]
```

| Option | Description |
| --- | --- |
| `XSD` | The schema the XML conforms to. |
| `PROTO` | The `.proto` defining the target message. It may cover only part of the XSD. |
| `XML` | The document to convert. `-` reads stdin. |
| `-o, --output FILE` | Output file. `-` writes to stdout. Default: the XML path with `.pb`, or `.txtpb` for text. |
| `-f, --format` | `binary` (default, deterministic wire format) or `text` (protobuf text format). |
| `-m, --message NAME` | The root message. Default: the top-level message named like the root element, or the only top-level message if there is just one. Accepts `acme.v1.Order`, `.acme.v1.Order`, `v1.Order` or just `Order`, plus nested names such as `Order.Line` and types from imported files. An ambiguous short name is an error that lists the candidates. |
| `-E, --envelope NAME` | Wrap the result in this enclosing message. The root element fills the envelope field that matches it. See [Envelope messages](#envelope-messages). |
| `-I, --include DIR` | Extra import path for the `.proto`. Repeatable. The `.proto`'s own directory and the well-known types are always available. |
| `--no-validate` | Don't stop on XSD validation errors; print them as warnings and convert what decodes. |
| `--strict` | Treat proto fields with no XSD counterpart as an error instead of a warning. |
| `-v, --verbose` | List each skipped XML node instead of just counting them. |

Examples:

```bash
xml2pb orders.xsd orders.proto order.xml                  # -> order.pb
xml2pb orders.xsd orders.proto order.xml -f text          # -> order.txtpb
xml2pb orders.xsd orders.proto order.xml -m Order -o -    # binary to stdout
curl -s https://example.com/order.xml | xml2pb orders.xsd orders.proto - -f text -o -
xml2pb schemas/orders.xsd protos/orders.proto order.xml -I third_party/protos
xml2pb orders.xsd envelope.proto order.xml --envelope Envelope   # Envelope{order: ...}
```

### What gets reported

All diagnostics go to **stderr**, so stdout stays clean for piping.

| Message | Meaning |
| --- | --- |
| `note: N XML node(s) not in proto were skipped` | XML content with no matching proto field. This is expected when the proto is a subset. Use `-v` to list them. |
| `warning: proto fields without XSD counterpart: ...` | Proto fields xml2pb couldn't find in the XSD. These are usually typos or renamed elements. `--strict` makes this an error. |
| `warning: <reason>` | A validation problem tolerated because of `--no-validate`. |
| `error: ...` | Conversion failed (exit code 1). Nothing is written. |

Exit codes: `0` success, `1` conversion error, `2` invalid command-line usage.

## Generating a .proto from an XSD

Pass only the XSD, plus `--elements`:

```text
xml2pb XSD --elements a,b,c [-o OUT] [--package PKG] [--envelope NAME]
                            [--decimal double|string] [--datetime timestamp|string]
                            [--no-optional]
```

| Option | Description |
| --- | --- |
| `-e, --elements` | Comma-separated top-level elements to generate. Repeatable. Each becomes a message named after it, so `purchase-order` becomes `PurchaseOrder`. |
| `-o, --output` | Output file. `-` writes to stdout. Default: the XSD path with `.proto`. |
| `--package` | `package` line for the generated file. |
| `-E, --envelope NAME` | Also generate an enclosing message `NAME` with a `oneof` holding one field per element. See [Envelope messages](#envelope-messages). |
| `--decimal` | `double` (default) or `string`. `string` keeps `xs:decimal` values exact. |
| `--datetime` | `timestamp` (default, `google.protobuf.Timestamp`) or `string`. |
| `--no-optional` | Don't mark singular scalar fields `optional`. By default they are, so that "absent" stays distinct from `0` or `""`. |

What the generator produces:

| XSD construct | Generated proto |
| --- | --- |
| Top-level element | `message` named in CamelCase (`order` becomes `Order`) |
| Named complex type | `message` named after the type, shared by every element that uses it |
| Anonymous complex type | `message` named after the element. On a name clash it gets a parent prefix (`BItem`), then a numeric suffix (`Item2`). |
| Attribute | Field with comment `// attribute` |
| Simple content plus attributes | Attribute fields plus a `value` field (or `text`, `content`, `body`) for the element text |
| Simple content without attributes | A plain scalar field, with no wrapper message |
| `maxOccurs > 1`, or inside a repeating group | `repeated` |
| `xs:enumeration` on a string type | `enum` with `NAME_UNSPECIFIED = 0` and `NAME_VALUE` entries |
| `xs:list` | `string` holding the space-separated values (comment says so) |
| `xs:union` | `string` |
| Recursive types | Self-referencing message |
| `xs:any` / `xs:anyAttribute` | Skipped |

Field names are the snake_case of the XSD names (`orderId` becomes
`order_id`). Field numbers follow XSD order. **Review the generated file before
using it for data that is already serialized.** If the XSD later changes,
regenerating can renumber fields.

The output is designed to feed straight back into the converter. Running
`xml2pb x.xsd gen.proto doc.xml --strict` with a generated `gen.proto` reports
no gaps. You can then trim the generated file down to the fields you need.

## Envelope messages

Some `.proto` files wrap their payload messages in an *envelope*: a message
whose fields hold the real messages. Often every message is declared at top
level:

```protobuf
message Order { ... }
message Ping { ... }

message Envelope {
  string trace_id = 1;
  oneof body {
    Order order = 2;
    Ping ping = 3;
  }
}
```

An XML document's root is `<order>` or `<ping>`, not `<envelope>`, so by
default xml2pb produces a bare `Order` or `Ping`. Use `--envelope` to get the
enclosing message instead:

```console
$ xml2pb orders.xsd envelope.proto order.xml --envelope Envelope -f text -o -
order {
  order_id: 42
  ...
}
```

The root element is converted exactly as without `--envelope`, then placed in
the envelope field chosen like this:

1. A message field named like the root element (`<order>` goes to `order`),
   ignoring case and separators. The JSON name counts too.
2. Otherwise, the one message field whose **type** is named like the root
   element (`<order>` goes to `Order payload = 1;`).
3. With `-m` as well, only fields of that inner type are considered, and if
   there is just one, it is used whatever its name.

A `repeated` envelope field gets the document as its single entry. Map and
scalar fields are never candidates. If no field fits, or several do, the error
lists the envelope's message fields. The envelope's other fields stay unset,
and proto2 `required` fields on the envelope are still checked. Skipped-node
and subset-check paths start with the envelope, for example
`Envelope/order/note`.

`--envelope NAME` accepts the same name forms as `-m`: `env.v1.Envelope`,
`Envelope`, and so on.

### Generating an envelope

When generating, `--envelope NAME` adds the enclosing message, first in the
file, with one field per generated element inside a `oneof`:

```console
$ xml2pb orders.xsd -e order,ping --envelope Envelope -o envelope.proto
$ grep -A6 "message Envelope" envelope.proto
message Envelope {
  oneof payload {
    Order order = 1;
    Ping ping = 2;
  }
}
```

Field names are the snake_case element names, so the generated file works
with `--envelope` for conversion straight away. The name must be a valid proto
identifier and must not clash with a generated message. Nested types that
would have taken the name are renamed around it. The `oneof` is called
`payload`, or `payload2` and so on if an element is itself named `payload`.

## How XML maps onto protobuf

### Names

An XML element or attribute matches a proto field when the two names are equal
after removing `_`, `-` and `.` and lower-casing. The field's JSON name is
also tried. Namespace prefixes are ignored. The root element is matched to a
top-level message in the same way.

### Types

| Decoded XSD value | Proto field type | Rule |
| --- | --- | --- |
| integer types, `xs:decimal`, numeric text | `int32`/`int64`/`uint32`/`uint64`/`sint*`/`fixed*` | Must be a whole number within the type's range. `4.0` is accepted; `4.5` is an error. |
| any number | `float`/`double` | `float` errors if the value overflows float32. |
| `xs:boolean`, `true`/`false`/`1`/`0` | `bool` | |
| anything | `string` | Its text form. `xs:decimal` keeps its digits (`19.95`, `5.00`). Booleans become `true` or `false`. |
| `xs:hexBinary`, `xs:base64Binary` | `bytes` | Decoded to the raw bytes. Any other text is encoded as UTF-8. |
| enumeration value or text | `enum` | Exact value name (`STATUS_DONE`), a number, or the text matched ignoring case and separators, with or without the enum-name prefix (`done`, `in-progress`). |
| `xs:dateTime`, `xs:date` | `google.protobuf.Timestamp` | Nanosecond precision (further digits are truncated). A value with no timezone is taken as UTC. `24:00:00` means end of day. |
| element text | wrapper types (`google.protobuf.Int32Value`, ...) | Filled through their `value` field. |

### Structure

| XML | Proto |
| --- | --- |
| Child element | Field of the same name. A complex child fills a sub-message. |
| Attribute | Field of the same name. Attributes and child elements share one namespace in the message. |
| Element text when the element also has attributes | First field named `value`, `text`, `content` or `body` |
| Repeated element | `repeated` field. Several occurrences going into a non-repeated field is an error. |
| `xs:list` value (`<tags>1 2 3</tags>`) | `repeated` scalar field gets one entry per item. A `string` field gets the space-separated text. |
| Entry elements with `<key>` and `<value>` children | `map<K, V>` field |
| Empty or `xsi:nil="true"` element | Sub-message field: marked present, with no fields set. Scalar field: left unset. A nil occurrence in a repeated scalar is dropped. |
| XSD default or fixed values | Applied (decoding uses the schema's defaults) |
| `xmlns`, `xsi:*` attributes | Ignored |

proto2 `required` fields that end up unset are an error, listed by path, for
example `line[0].sku`.

## Python API

```python
from pathlib import Path
import xml2pb

result = xml2pb.convert(
    "orders.xsd",
    "orders.proto",
    "order.xml",  # or bytes, or a binary file object
    message="Order",  # optional; same lookup rules as -m
    envelope=None,  # or "Envelope": wrap the result in that enclosing message
    includes=["third_party/protos"],
    validate=True,  # False: tolerate validation errors (see result.validation_errors)
    strict=False,  # True: proto fields missing from the XSD raise
)

msg = result.message  # a regular protobuf message
print(msg.order_id, msg.status, msg.placed_at.ToDatetime())
print(result.skipped)  # ['Order/customer/email', 'Order/note']
print(result.subset_issues)  # proto fields with no XSD counterpart
Path("order.pb").write_bytes(result.serialize())  # or .serialize("text")

proto_text = xml2pb.generate_proto(
    "orders.xsd",
    ["order"],
    package="acme.v1",
    decimal="string",  # or "double"
    datetime_mode="timestamp",  # or "string"
    use_optional=True,
    envelope="Envelope",  # optional enclosing message with a oneof of the elements
    warn=print,  # receives non-fatal warnings; default prints to stderr
)
```

| Name | Purpose |
| --- | --- |
| `convert(xsd, proto, xml, *, message, envelope, includes, validate, strict)` | Full conversion. Returns a `Conversion`. |
| `Conversion` | Holds `.message`, `.skipped`, `.subset_issues` and `.validation_errors`, plus `.serialize(fmt)`. |
| `generate_proto(xsd, elements, package, decimal, datetime_mode, use_optional, warn, envelope)` | Generates `.proto` source text. |
| `ProtoGenerator(schema, ...)` | The generator, for an already-loaded `xmlschema` schema (for example an `XMLSchema11`). |
| `load_proto(proto, includes)` | Compiles a `.proto` into a private descriptor pool. Returns `(pool, file_descriptor, file_names)`. |
| `find_message(pool, file_desc, name, file_names)` / `pick_message(...)` | Message lookup, using the same rules as the CLI. |
| `message_class(pool, desc)` | Builds a message class from a descriptor. |
| `protos.find_envelope_field(envelope, root_name, inner=None)` | The envelope field the root element goes into, using the rules above. |
| `load_schema(xsd)` / `load_xml(xml)` | Load inputs, turning failures into `ConversionError`. |
| `serialize(msg, fmt)` | `"binary"` (deterministic) or `"text"` (UTF-8). |
| `parse_datetime(value)` | `xs:dateTime`/`xs:date` to `(seconds, nanos)`. |
| `ConversionError` | Raised for every user-facing failure. |

The library never prints, apart from the generator's default warning handler.
Each call compiles the `.proto` into its own descriptor pool, so different
schemas can be used side by side. Message classes from different calls are
separate types, so compare their serialized bytes, not the objects.

## Development

```text
xml2pb/
├── pyproject.toml          # Poetry / PEP 621 metadata and tool config
├── poetry.toml             # keeps the virtualenv in .venv (used by Pyright / Pylance)
├── src/xml2pb/
│   ├── __init__.py         # public API
│   ├── __main__.py         # python -m xml2pb
│   ├── cli.py              # argparse front end (console script)
│   ├── converter.py        # subset check, message filling, convert()
│   ├── generator.py        # XSD -> .proto
│   ├── protos.py           # .proto compilation, message lookup, required fields
│   ├── values.py           # scalar / enum / bytes / timestamp conversion
│   ├── names.py            # name normalisation and case conversion
│   └── errors.py
├── tests/                  # pytest suite; tests/data holds the fixture XSD/XML/protos
└── .github/workflows/ci.yml
```

```bash
poetry install
poetry run pytest                  # run the tests
poetry run pytest --cov            # with branch coverage
poetry run ruff check .            # lint
poetry run ruff format .           # format (CI runs --check; Markdown code blocks too)
poetry run pyright                 # type check (the engine behind VS Code's Pylance)
poetry build                       # sdist + wheel in dist/
```

The test suite covers:

- name matching, every scalar conversion and range check, enums, binary types
  and date/time parsing, including timezones, nanoseconds, `xs:date` and
  `24:00:00`
- message lookup, proto2 required fields, the subset check, and strict and
  lax validation
- lists against repeated elements, maps, nil handling, wildcards and
  repeating groups
- every generator construct and option, plus generate-then-convert round trips
- envelopes: field choice by name, by type and with `-m`, repeated fields,
  ambiguity and error cases, generated envelopes and their round trip
- the CLI end to end: file, stdout and stdin I/O, error exits, overwrite
  protection and `python -m xml2pb`

### Continuous integration

`.github/workflows/ci.yml` runs on pushes, pull requests and manual dispatch:

1. **Lint**: `poetry check --lock`, `ruff check`, `ruff format --check`,
   `pyright`.
2. **Test**: pytest with coverage (must be at least 95%) on Python 3.10–3.14 on
   Ubuntu, plus Python 3.13 on macOS and Windows.
3. **Test (minimum dependencies)**: Python 3.10 with xmlschema 3.0, protobuf
   4.25 and grpcio-tools 1.62, installed with plain pip. This keeps the version
   floors honest.
4. **Build**: runs once lint and both test jobs pass. It builds the wheel,
   installs it into a clean virtualenv, generates a `.proto` from the fixture
   XSD, converts with it under `--strict`, converts with the hand-written proto,
   and uploads `dist/`.

## Troubleshooting

**`error: XML is not valid against the XSD: ...`**
The message includes the failing path and reason. Fix the document, or use
`--no-validate` to convert what decodes. Invalid values are then left unset.

**`error: Can't tell which message maps to root element <x>; use --message`**
No top-level message is named like the root element. Pass `-m`. Short names
like `-m Order` work.

**`warning: proto fields without XSD counterpart`**
A proto field's name doesn't match any element or attribute at that level of
the XSD, after normalisation. Check for typos and renamed elements. Use
`--strict` in pipelines to make this fail.

**`error: <path>: XML has N occurrences but proto field 'x' is not repeated`**
The element repeats in the XML. Make the proto field `repeated`.

**`error: <path>: '4.5' is not a whole number (would lose data)`**
The XSD type is fractional (often `xs:decimal`) but the proto field is an
integer. Use `double`, or `string` for exact values.

**`error: <path>: 'x' is not a value of enum E (...)`**
The XML value doesn't match any enum value, even ignoring case, separators and
the enum-name prefix. Add the value to the proto enum.

**`error: Required proto fields not populated: ...`**
proto2 `required` fields had no data in the XML. Make them `optional`, or
check the names.

**`error: protoc failed to compile the .proto file (see above)`**
Read protoc's message above it. For `File not found` on an import, add `-I`.

**Pylance or Pyright reports imports that can't be resolved**
VS Code is using a different interpreter. Run `poetry install`, then use
**Python: Select Interpreter** and choose `.venv/bin/python`
(`.venv\Scripts\python.exe` on Windows).

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
