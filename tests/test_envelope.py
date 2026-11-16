"""Tests for --envelope: wrapping the converted root in an enclosing message."""

from __future__ import annotations

import pytest

from xml2pb import ConversionError, convert, generate_proto, load_proto
from xml2pb.cli import main
from xml2pb.protos import find_envelope_field, find_message


@pytest.fixture(scope="module")
def envelope_proto(data):
    return data / "envelope.proto"


@pytest.fixture(scope="module")
def env_schema(envelope_proto):
    return load_proto(envelope_proto)


def _msg(env_schema, name):
    pool, fd, names = env_schema
    return find_message(pool, fd, name, names)


@pytest.fixture
def ping_xml(write):
    return write("ping.xml", '<ping xmlns="urn:acme:orders">hello</ping>')


# --------------------------------------------------------------------------- field choice


class TestFindEnvelopeField:
    def test_by_field_name(self, env_schema):
        env = _msg(env_schema, "Envelope")
        assert find_envelope_field(env, "order").name == "order"
        assert find_envelope_field(env, "PING").name == "ping"

    def test_by_type_name(self, env_schema):
        assert find_envelope_field(_msg(env_schema, "ByType"), "order").name == "payload"

    def test_repeated_field_by_type(self, env_schema):
        assert find_envelope_field(_msg(env_schema, "Batch"), "order").name == "orders"

    def test_explicit_inner_type(self, env_schema):
        two = _msg(env_schema, "TwoOrders")
        order = _msg(env_schema, "acme.v1.Order")
        with pytest.raises(ConversionError, match=r"several fields .*<order>: first, second"):
            find_envelope_field(two, "order", order)
        by_type = _msg(env_schema, "ByType")
        # a single field of the forced type is used whatever its name
        assert find_envelope_field(by_type, "anything", order).name == "payload"

    def test_inner_type_filters_name_match(self, env_schema):
        env = _msg(env_schema, "Envelope")
        ping = _msg(env_schema, "acme.v1.Ping")
        assert find_envelope_field(env, "order", ping).name == "ping"

    def test_ambiguous_by_type(self, env_schema):
        with pytest.raises(ConversionError, match=r"TwoOrders has several fields .*first, second"):
            find_envelope_field(_msg(env_schema, "TwoOrders"), "order")

    def test_maps_and_scalars_never_match(self, env_schema):
        with pytest.raises(ConversionError) as exc:
            find_envelope_field(_msg(env_schema, "NoMatch"), "order")
        assert "has no message field for root element <order>" in str(exc.value)
        assert "Message fields: (none)" in str(exc.value)

    def test_not_found_lists_fields(self, env_schema):
        with pytest.raises(ConversionError, match=r"Message fields: payload \(acme\.v1\.Order\)"):
            find_envelope_field(_msg(env_schema, "ByType"), "invoice")

    def test_not_found_with_inner_type(self, env_schema):
        line = _msg(env_schema, "acme.v1.Line")
        with pytest.raises(ConversionError, match=r"no message field of type acme\.v1\.Line"):
            find_envelope_field(_msg(env_schema, "Envelope"), "order", line)


# --------------------------------------------------------------------------- convert


class TestConvert:
    def test_wraps_root(self, orders_xsd, envelope_proto, order_xml, orders_proto):
        r = convert(orders_xsd, envelope_proto, order_xml, envelope="Envelope")
        m = r.message
        assert m.DESCRIPTOR.full_name == "env.v1.Envelope"
        assert m.WhichOneof("body") == "order"
        assert m.order.order_id == 42 and len(m.order.line) == 2
        assert m.trace_id == ""
        # the wrapped order is exactly what a plain conversion produces
        plain = convert(orders_xsd, orders_proto, order_xml).serialize()
        assert m.order.SerializeToString(deterministic=True) == plain

    def test_other_root_goes_to_its_field(self, orders_xsd, envelope_proto, ping_xml):
        m = convert(orders_xsd, envelope_proto, ping_xml, envelope="env.v1.Envelope").message
        assert m.WhichOneof("body") == "ping" and m.ping.value == "hello"

    def test_short_envelope_name(self, orders_xsd, envelope_proto, order_xml):
        m = convert(orders_xsd, envelope_proto, order_xml, envelope="ByType").message
        assert m.payload.order_id == 42 and not m.HasField("other")

    def test_repeated_envelope_field(self, orders_xsd, envelope_proto, order_xml):
        m = convert(orders_xsd, envelope_proto, order_xml, envelope="Batch").message
        assert len(m.orders) == 1 and m.orders[0].order_id == 42

    def test_message_selects_inner_type(self, orders_xsd, envelope_proto, ping_xml):
        # -m names the inner type; <ping> is then converted as a Ping into Envelope.ping
        m = convert(
            orders_xsd, envelope_proto, ping_xml, envelope="Envelope", message="Ping"
        ).message
        assert m.ping.value == "hello"

    def test_message_does_not_resolve_ambiguity_by_itself(
        self, orders_xsd, envelope_proto, order_xml
    ):
        with pytest.raises(ConversionError, match="several fields"):
            convert(orders_xsd, envelope_proto, order_xml, envelope="TwoOrders", message="Order")

    def test_empty_root_still_sets_field(self, orders_xsd, envelope_proto, write):
        xml = write("p.xml", '<ping xmlns="urn:acme:orders"></ping>')
        m = convert(orders_xsd, envelope_proto, xml, envelope="Envelope").message
        assert m.WhichOneof("body") == "ping"

    def test_paths_include_envelope(self, orders_xsd, envelope_proto, order_xml, data):
        r = convert(orders_xsd, envelope_proto, order_xml, envelope="Envelope")
        assert r.skipped == ["Envelope/order/customer/email", "Envelope/order/note"]

    def test_subset_issues_include_envelope(self, orders_xsd, data, order_xml, write):
        write("not_subset.proto", (data / "not_subset.proto").read_text())
        env = write(
            "e.proto",
            'syntax = "proto3"; package e; import "not_subset.proto";'
            " message Env { bad.Order order = 1; }",
        )
        r = convert(orders_xsd, env, order_xml, envelope="Env")
        assert r.subset_issues[0] == "Env.order.no_such_field: no matching element/attribute in XSD"

    def test_unknown_envelope(self, orders_xsd, envelope_proto, order_xml):
        with pytest.raises(ConversionError, match="Message 'Nope' not found"):
            convert(orders_xsd, envelope_proto, order_xml, envelope="Nope")

    def test_required_fields_of_envelope_checked(self, orders_xsd, data, order_xml, write):
        write("orders.proto", (data / "orders.proto").read_text())
        env = write(
            "req_env.proto",
            'syntax = "proto2"; package r; import "orders.proto";'
            " message Env { required string trace_id = 1; optional acme.v1.Order order = 2; }",
        )
        with pytest.raises(ConversionError, match="Required proto fields not populated: trace_id"):
            convert(orders_xsd, env, order_xml, envelope="Env")

    def test_without_envelope_unchanged(self, orders_xsd, envelope_proto, order_xml):
        # envelope.proto imports orders.proto; plain conversion still finds Order
        r = convert(orders_xsd, envelope_proto, order_xml, message="acme.v1.Order")
        assert r.message.DESCRIPTOR.full_name == "acme.v1.Order"


# --------------------------------------------------------------------------- generate


def _gen(orders_xsd, elements, **kw):
    return generate_proto(orders_xsd, elements, **kw)


class TestGenerate:
    def test_envelope_message(self, orders_xsd):
        text = _gen(orders_xsd, ["order", "ping"], envelope="Envelope")
        assert "// Envelope: Envelope (holds exactly one of the above)\n" in text
        assert (
            "message Envelope {\n"
            "  oneof payload {\n"
            "    Order order = 1;\n"
            "    Ping ping = 2;\n"
            "  }\n"
            "}\n"
        ) in text
        assert text.index("message Envelope") < text.index("message Order")

    def test_no_envelope_by_default(self, orders_xsd):
        text = _gen(orders_xsd, ["order"])
        assert "oneof" not in text and "Envelope" not in text

    def test_round_trip(self, orders_xsd, order_xml, tmp_path):
        p = tmp_path / "g.proto"
        p.write_text(_gen(orders_xsd, ["order", "ping"], envelope="Wrapper", package="g"))
        r = convert(orders_xsd, p, order_xml, envelope="Wrapper", strict=True)
        assert r.message.WhichOneof("payload") == "order"
        assert r.message.order.order_id == 42 and r.skipped == []

    def test_envelope_name_reserved_before_nested_types(self, xsd):
        p = xsd(
            """
            <xs:element name="a"><xs:complexType><xs:sequence>
              <xs:element name="envelope"><xs:complexType><xs:sequence>
                <xs:element name="x" type="xs:int"/></xs:sequence></xs:complexType></xs:element>
            </xs:sequence></xs:complexType></xs:element>
            """
        )
        text = generate_proto(p, ["a"], envelope="Envelope")
        assert "  AEnvelope envelope = 1;" in text  # nested type renamed around it
        assert "    A a = 1;" in text

    def test_oneof_name_avoids_field_names(self, xsd):
        p = xsd(
            """
            <xs:element name="payload"><xs:complexType/></xs:element>
            <xs:element name="other"><xs:complexType/></xs:element>
            """
        )
        text = generate_proto(p, ["payload", "other"], envelope="Env")
        assert "  oneof payload2 {\n    Payload payload = 1;\n    Other other = 2;" in text

    def test_field_collision_warns(self, xsd):
        p = xsd(
            """
            <xs:element name="aB"><xs:complexType/></xs:element>
            <xs:element name="a_b"><xs:complexType/></xs:element>
            """
        )
        warnings: list[str] = []
        text = generate_proto(p, ["aB", "a_b"], envelope="Env", warn=warnings.append)
        assert warnings == ["Env: 'a_b' collides with another field; omitted"]
        assert "    AB a_b = 1;\n  }" in text

    def test_clash_with_root_message(self, orders_xsd):
        with pytest.raises(ConversionError, match="clashes with the message for <order>"):
            _gen(orders_xsd, ["order"], envelope="Order")

    @pytest.mark.parametrize("name", ["1Env", "my-env", "env.v1", ""])
    def test_invalid_name(self, orders_xsd, name):
        with pytest.raises(ConversionError, match="not a valid proto identifier"):
            _gen(orders_xsd, ["order"], envelope=name)

    def test_compiles(self, orders_xsd, tmp_path):
        p = tmp_path / "g.proto"
        p.write_text(_gen(orders_xsd, ["order", "ping"], envelope="Envelope"))
        pool, fd, _ = load_proto(p)
        env = fd.message_types_by_name["Envelope"]
        assert [f.name for f in env.oneofs_by_name["payload"].fields] == ["order", "ping"]


# --------------------------------------------------------------------------- CLI


class TestCli:
    def test_convert_flag(self, orders_xsd, envelope_proto, order_xml, tmp_path):
        out = tmp_path / "o.txtpb"
        argv = [str(orders_xsd), str(envelope_proto), str(order_xml)]
        assert main([*argv, "--envelope", "Envelope", "-f", "text", "-o", str(out)]) == 0
        assert out.read_text(encoding="utf-8").startswith("order {\n  order_id: 42\n")

    def test_short_flag_and_error(self, orders_xsd, envelope_proto, order_xml, capsys):
        argv = [str(orders_xsd), str(envelope_proto), str(order_xml)]
        assert main([*argv, "-E", "TwoOrders", "-o", "-"]) == 1
        assert "error: Envelope env.v1.TwoOrders has several fields" in capsys.readouterr().err

    def test_generate_flag(self, orders_xsd, tmp_path):
        out = tmp_path / "g.proto"
        assert main([str(orders_xsd), "-e", "order,ping", "-E", "Envelope", "-o", str(out)]) == 0
        assert "  oneof payload {" in out.read_text(encoding="utf-8")

    def test_generate_invalid_name(self, orders_xsd, capsys):
        assert main([str(orders_xsd), "-e", "order", "-E", "a-b", "-o", "-"]) == 1
        assert "not a valid proto identifier" in capsys.readouterr().err
