"""Tests for .proto compilation, message lookup and required-field checks."""

from __future__ import annotations

from typing import Any

import pytest

from xml2pb import ConversionError, find_message, load_proto, message_class, pick_message
from xml2pb.protos import all_messages, find_missing_required, is_map, is_repeated, is_required


class TestLoadProto:
    def test_loads_with_imports(self, orders_proto):
        pool, fd, names = load_proto(orders_proto)
        assert fd.name == "orders.proto" and names[-1] == "orders.proto"
        assert "google/protobuf/timestamp.proto" in names
        assert "google/protobuf/wrappers.proto" in names

    def test_missing(self, tmp_path):
        with pytest.raises(ConversionError, match="Proto file not found"):
            load_proto(tmp_path / "x.proto")

    def test_syntax_error(self, proto):
        with pytest.raises(ConversionError, match="protoc failed"):
            load_proto(proto("message M { int32 x = 1 }"))

    def test_include_path(self, write, proto):
        write("vendor/v.proto", 'syntax = "proto3"; package v; message V { int32 x = 1; }')
        main = proto('import "v.proto"; message M { v.V v = 1; }')
        with pytest.raises(ConversionError):
            load_proto(main)
        _, _, names = load_proto(main, [main.parent / "vendor"])
        assert "v.proto" in names


@pytest.fixture(scope="module")
def schema(orders_proto):
    return load_proto(orders_proto)


@pytest.fixture(scope="module")
def order_fields(schema):
    pool, _, _ = schema
    return pool.FindMessageTypeByName("acme.v1.Order").fields_by_name


@pytest.fixture(scope="module")
def required_order(data):
    pool, _, _ = load_proto(data / "required.proto")
    return message_class(pool, pool.FindMessageTypeByName("req.Order"))


class TestFind:
    @pytest.mark.parametrize(
        "name, expected",
        [
            ("acme.v1.Order", "acme.v1.Order"),
            (".acme.v1.Order", "acme.v1.Order"),
            (" Order ", "acme.v1.Order"),
            ("v1.Order", "acme.v1.Order"),
            ("Line", "acme.v1.Line"),
            ("Timestamp", "google.protobuf.Timestamp"),
            ("MetaEntry", "acme.v1.Order.MetaEntry"),
            ("Order.MetaEntry", "acme.v1.Order.MetaEntry"),
        ],
    )
    def test_resolves(self, schema, name, expected):
        pool, fd, names = schema
        assert find_message(pool, fd, name, names).full_name == expected

    def test_not_found_lists_names(self, schema):
        pool, fd, names = schema
        with pytest.raises(ConversionError, match=r"'Nope' not found. Available: .*acme\.v1\.Line"):
            find_message(pool, fd, "Nope", names)

    def test_whole_components_only(self, schema):
        pool, fd, names = schema
        with pytest.raises(ConversionError, match="not found"):
            find_message(pool, fd, "rder", names)

    def test_ambiguous(self, proto):
        pool, fd, names = load_proto(proto("package p; message A {} message B { message A {} }"))
        with pytest.raises(ConversionError, match=r"ambiguous; use one of: p\.A, p\.B\.A"):
            find_message(pool, fd, "A", names)
        assert find_message(pool, fd, "B.A", names).full_name == "p.B.A"

    def test_requested_file_wins(self, write, proto):
        write("dep.proto", 'syntax = "proto3"; package dep; message Thing {}')
        pool, fd, names = load_proto(proto('package main; import "dep.proto"; message Thing {}'))
        assert find_message(pool, fd, "Thing", names).full_name == "main.Thing"

    def test_empty_file(self, proto):
        pool, fd, names = load_proto(proto("package e;"))
        with pytest.raises(ConversionError, match=r"Available: \(none\)"):
            find_message(pool, fd, "X", names)


class TestPickMessage:
    @pytest.mark.parametrize("root", ["order", "Order", "ORDER"])
    def test_matches_root_element(self, schema, root):
        pool, fd, names = schema
        assert pick_message(pool, fd, root, None, names).full_name == "acme.v1.Order"

    def test_root_match_ignores_separators(self, proto):
        pool, fd, names = load_proto(proto("message PurchaseOrder {} message Other {}"))
        assert pick_message(pool, fd, "purchase-order", None, names).name == "PurchaseOrder"

    def test_explicit_wins(self, schema):
        pool, fd, names = schema
        assert pick_message(pool, fd, "order", "Ping", names).full_name == "acme.v1.Ping"

    def test_single_message_fallback(self, proto):
        pool, fd, names = load_proto(proto("message Only {}"))
        assert pick_message(pool, fd, "whatever", None, names).name == "Only"

    def test_no_match(self, schema):
        pool, fd, names = schema
        with pytest.raises(ConversionError, match=r"<invoice>; use --message.*acme\.v1\.Ping"):
            pick_message(pool, fd, "invoice", None, names)

    def test_no_messages(self, proto):
        pool, fd, names = load_proto(proto("package e;"))
        with pytest.raises(ConversionError, match=r"Available: \(none\)"):
            pick_message(pool, fd, "x", None, names)


class TestFieldHelpers:
    def test_is_repeated(self, order_fields):
        order = order_fields
        assert is_repeated(order["line"]) and is_repeated(order["meta"])
        assert not is_repeated(order["order_id"])

    def test_is_map(self, order_fields):
        order = order_fields
        assert is_map(order["meta"])
        assert not is_map(order["line"]) and not is_map(order["order_id"])

    def test_is_required(self, data):
        pool, _, _ = load_proto(data / "required.proto")
        f = pool.FindMessageTypeByName("req.Line").fields_by_name
        assert is_required(f["sku"]) and not is_required(f["qty"])

    def test_legacy_label_fallback(self):
        class F:  # protobuf versions without is_repeated / is_required
            label = 3
            message_type = None

        assert is_repeated(F()) and not is_required(F())

    def test_all_messages_includes_nested(self, orders_proto):
        pool, fd, _ = load_proto(orders_proto)
        names = {d.full_name for d in all_messages(pool, [fd.name])}
        assert {"acme.v1.Order", "acme.v1.Order.MetaEntry", "acme.v1.Line"} <= names


class TestMissingRequired:
    def test_reports_nested_paths(self, required_order):
        Order: Any = required_order
        msg = Order()
        line = msg.line.add()
        line.qty = 1
        assert find_missing_required(msg) == ["order_id", "line[0].sku", "line[0].gift_wrap"]

    def test_none_missing(self, required_order):
        Order: Any = required_order
        msg = Order(order_id=1)
        msg.line.add(sku="a", gift_wrap="no")
        assert find_missing_required(msg) == []

    def test_maps_and_singular_messages(self, write):
        p = write(
            "r.proto",
            'syntax = "proto2"; package r;'
            " message In { required int32 x = 1; }"
            " message Out { optional In one = 1; map<string, In> many = 2;"
            " map<string, int32> plain = 3; }",
        )
        pool, _, _ = load_proto(p)
        Out: Any = message_class(pool, pool.FindMessageTypeByName("r.Out"))
        msg = Out()
        msg.one.SetInParent()
        msg.many["k"].SetInParent()
        msg.plain["z"] = 1
        assert find_missing_required(msg) == ["one.x", "many[k].x"]
