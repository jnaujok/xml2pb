"""Shared fixtures for the xml2pb test suite."""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

DATA = Path(__file__).parent / "data"

XS_HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n'


@pytest.fixture(scope="session")
def data() -> Path:
    return DATA


@pytest.fixture(scope="session")
def orders_xsd() -> Path:
    return DATA / "orders.xsd"


@pytest.fixture(scope="session")
def orders_proto() -> Path:
    return DATA / "orders.proto"


@pytest.fixture(scope="session")
def order_xml() -> Path:
    return DATA / "order.xml"


@pytest.fixture
def write(tmp_path: Path) -> Callable[[str, str], Path]:
    """Write a dedented text file into tmp_path and return its path."""

    def _write(name: str, content: str) -> Path:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
        return p

    return _write


@pytest.fixture
def xsd(write) -> Callable[[str], Path]:
    """Write an XSD whose body is wrapped in an <xs:schema> (no target namespace)."""

    def _xsd(body: str, name: str = "schema.xsd") -> Path:
        return write(
            name,
            XS_HEADER
            + '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
            + textwrap.dedent(body)
            + "\n</xs:schema>\n",
        )

    return _xsd


@pytest.fixture
def proto(write) -> Callable[[str], Path]:
    """Write a proto3 file (``syntax`` line added) and return its path."""

    def _proto(body: str, name: str = "m.proto") -> Path:
        return write(name, 'syntax = "proto3";\n' + textwrap.dedent(body))

    return _proto
