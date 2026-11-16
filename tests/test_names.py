"""Tests for name normalisation and case conversion."""

from __future__ import annotations

import pytest

from xml2pb.names import camel, local, norm, snake


@pytest.mark.parametrize(
    "name", ["order_id", "orderId", "OrderID", "Order-ID", "order.id", "ORDER_ID"]
)
def test_norm_equates_separator_and_case_variants(name):
    assert norm(name) == "orderid"


def test_norm_keeps_digits():
    assert norm("line_2") == "line2"


@pytest.mark.parametrize(
    "key, expected",
    [
        ("name", "name"),
        ("@name", "name"),
        ("ns:name", "name"),
        ("@ns:name", "name"),
        ("{urn:acme}name", "name"),
        ("@{urn:acme}name", "name"),
        ("{http://x/y:z}name", "name"),
    ],
)
def test_local(key, expected):
    assert local(key) == expected


def test_local_accepts_non_strings():
    assert local(42) == "42"


@pytest.mark.parametrize(
    "s, expected",
    [
        ("order", "Order"),
        ("order_line", "OrderLine"),
        ("order-line", "OrderLine"),
        ("orderLine", "OrderLine"),
        ("ORDER", "ORDER"),
        ("a.b.c", "ABC"),
        ("", "Message"),
        ("---", "Message"),
        ("2fa", "M2fa"),
    ],
)
def test_camel(s, expected):
    assert camel(s) == expected


@pytest.mark.parametrize(
    "s, expected",
    [
        ("orderId", "order_id"),
        ("orderID", "order_id"),
        ("OrderLine", "order_line"),
        ("order-line", "order_line"),
        ("HTTPServer", "http_server"),
        ("already_snake", "already_snake"),
        ("x", "x"),
        ("", "field"),
        ("__", "field"),
        ("2fa", "f_2fa"),
        ("a.b", "a_b"),
    ],
)
def test_snake(s, expected):
    assert snake(s) == expected
