"""Tests for XML -> protobuf conversion."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from google.protobuf import text_format

from xml2pb import Conversion, ConversionError, convert, load_schema, load_xml, serialize
from xml2pb.converter import (
    FillContext,
    check_subset,
    fill,
    find_root_element,
    set_field,
    xsd_attributes,
    xsd_children,
    xsd_repeated,
)
from xml2pb.protos import load_proto, message_class

# --------------------------------------------------------------------------- end to end


@pytest.fixture(scope="module")
def result(orders_xsd, orders_proto, order_xml) -> Conversion:
    return convert(orders_xsd, orders_proto, order_xml)


@pytest.fixture(scope="module")
def msg(result) -> Any:
    return result.message


class TestOrderFixture:
    def test_message_type(self, msg):
        assert msg.DESCRIPTOR.full_name == "acme.v1.Order"

    def test_scalars(self, msg):
        assert msg.order_id == 42
        assert msg.paid is True
        assert msg.weight == 1.5
        assert msg.ship == "2026-01-05"  # xs:date into a string field

    def test_decimal_to_string_is_exact(self, msg):
        assert msg.total == "19.95"

    def test_enum_from_xml_lexical_value(self, msg):
        assert msg.status == 2  # "in-progress" -> STATUS_IN_PROGRESS

    def test_timestamp(self, msg):
        assert (msg.placed_at.seconds, msg.placed_at.nanos) == (1767323045, 123_000_000)

    def test_repeated_messages_and_simple_content(self, msg):
        assert [(ln.sku, ln.qty) for ln in msg.line] == [("A1", 2), ("B2", 1)]
        assert (msg.line[1].price.currency, msg.line[1].price.value) == ("EUR", 9.95)

    def test_xs_list_into_repeated_scalar(self, msg):
        assert list(msg.tags) == [1, 2, 3]

    def test_hex_and_base64_binary(self, msg):
        assert msg.checksum == b"\xde\xad\xbe\xef"
        assert msg.blob == b"\xde\xad\xbe\xef"

    def test_nil_element_marks_message_present(self, msg):
        assert msg.HasField("discount") and msg.discount.value == 0

    def test_attribute_and_child_element(self, msg):
        assert (msg.customer.id, msg.customer.name) == ("C7", "Ada")

    def test_map_from_entry_elements(self, msg):
        assert dict(msg.meta) == {"channel": "web", "region": "eu"}

    def test_repeating_sequence_group(self, msg):
        # <ref> sits in <xs:sequence maxOccurs="unbounded">: repeated via its group.
        assert list(msg.refs.ref) == ["r1", "r2"]

    def test_attribute_default_applied(self, msg):
        assert msg.version == 1  # not in the XML; XSD default="1"

    def test_skipped_nodes_reported(self, result):
        assert result.skipped == ["Order/customer/email", "Order/note"]

    def test_no_issues(self, result):
        assert result.subset_issues == [] and result.validation_errors == []

    def test_serialize_round_trip(self, result):
        binary = result.serialize()
        again = type(result.message)()
        again.ParseFromString(binary)
        assert again == result.message
        text = result.serialize("text").decode()
        parsed = type(result.message)()
        text_format.Parse(text, parsed)
        assert parsed == result.message

    def test_binary_is_deterministic(self, orders_xsd, orders_proto, order_xml, result):
        again = convert(orders_xsd, orders_proto, order_xml)
        assert again.serialize() == result.serialize()


class TestSources:
    def test_bytes(self, orders_xsd, orders_proto, order_xml, result):
        r = convert(orders_xsd, orders_proto, order_xml.read_bytes())
        assert r.serialize() == result.serialize()

    def test_file_object(self, orders_xsd, orders_proto, order_xml, result):
        r = convert(orders_xsd, orders_proto, io.BytesIO(order_xml.read_bytes()))
        assert r.serialize() == result.serialize()

    def test_str_paths(self, orders_xsd, orders_proto, order_xml, result):
        r = convert(str(orders_xsd), str(orders_proto), str(order_xml))
        assert r.serialize() == result.serialize()

    def test_load_xml_errors(self, tmp_path):
        with pytest.raises(ConversionError, match="Could not read XML"):
            load_xml(tmp_path / "missing.xml")
        with pytest.raises(ConversionError, match=r"Could not read XML <input>: .*mismatched tag"):
            load_xml(b"<a><b></a>")

    def test_load_schema_errors(self, tmp_path, xsd):
        with pytest.raises(ConversionError, match="Could not load XSD"):
            load_schema(tmp_path / "missing.xsd")
        with pytest.raises(ConversionError, match="unknown type"):
            load_schema(xsd('<xs:element name="a" type="nope"/>'))


class TestSelection:
    def test_explicit_message(self, orders_xsd, orders_proto, write):
        xml = write("p.xml", '<ping xmlns="urn:acme:orders">hello</ping>')
        r = convert(orders_xsd, orders_proto, xml, message="Ping")
        assert r.message.DESCRIPTOR.full_name == "acme.v1.Ping"
        assert r.message.value == "hello"

    def test_root_matched_automatically(self, orders_xsd, orders_proto, write):
        xml = write("p.xml", '<ping xmlns="urn:acme:orders">hi</ping>')
        assert convert(orders_xsd, orders_proto, xml).message.DESCRIPTOR.name == "Ping"

    def test_unknown_message(self, orders_xsd, orders_proto, order_xml):
        with pytest.raises(ConversionError, match="'Nope' not found"):
            convert(orders_xsd, orders_proto, order_xml, message="Nope")

    def test_root_not_in_xsd(self, orders_xsd, orders_proto, write):
        xml = write("x.xml", '<invoice xmlns="urn:acme:orders"/>')
        with pytest.raises(ConversionError, match=r"<invoice> is not declared in the XSD"):
            convert(orders_xsd, orders_proto, xml, message="Order")

    def test_find_root_element_by_local_name(self, orders_xsd):
        schema = load_schema(orders_xsd)
        assert find_root_element(schema, "{urn:acme:orders}order") is not None
        assert find_root_element(schema, "order") is not None  # namespace-less fallback
        assert find_root_element(schema, "nope") is None


class TestSubsetCheck:
    def test_issues_reported(self, orders_xsd, data, order_xml):
        r = convert(orders_xsd, data / "not_subset.proto", order_xml)
        assert r.subset_issues == [
            "Order.no_such_field: no matching element/attribute in XSD",
            "Order.line.colour: no matching element/attribute in XSD",
        ]

    def test_strict_raises(self, orders_xsd, data, order_xml):
        with pytest.raises(ConversionError, match=r"not a subset of the XSD:\n  Order\.no_such"):
            convert(orders_xsd, data / "not_subset.proto", order_xml, strict=True)

    def test_json_name_and_text_fields_count_as_present(self, xsd, proto):
        schema = load_schema(
            xsd(
                """
                <xs:element name="r">
                  <xs:complexType><xs:simpleContent>
                    <xs:extension base="xs:string">
                      <xs:attribute name="unitCode" type="xs:string"/>
                    </xs:extension>
                  </xs:simpleContent></xs:complexType>
                </xs:element>
                """
            )
        )
        pool, fd, _ = load_proto(proto("message R { string unit_code = 1; string text = 2; }"))
        issues: list[str] = []
        check_subset(fd.message_types_by_name["R"], schema.elements["r"], "R", issues, set())
        assert issues == []

    def test_recursive_types_terminate(self, xsd, proto):
        schema = load_schema(
            xsd(
                """
                <xs:complexType name="Node"><xs:sequence>
                  <xs:element name="child" type="Node" minOccurs="0"/>
                </xs:sequence></xs:complexType>
                <xs:element name="node" type="Node"/>
                """
            )
        )
        pool, fd, _ = load_proto(proto("message Node { Node child = 1; string extra = 2; }"))
        issues: list[str] = []
        check_subset(
            fd.message_types_by_name["Node"], schema.elements["node"], "Node", issues, set()
        )
        assert issues == [
            "Node.child.extra: no matching element/attribute in XSD",
            "Node.extra: no matching element/attribute in XSD",
        ]


class TestValidation:
    @pytest.fixture
    def invalid_xml(self, order_xml, tmp_path) -> Path:
        p = tmp_path / "bad.xml"
        p.write_text(
            order_xml.read_text(encoding="utf-8").replace("<qty>2</qty>", "<qty>x</qty>"),
            encoding="utf-8",
        )
        return p

    def test_strict_by_default(self, orders_xsd, orders_proto, invalid_xml):
        with pytest.raises(ConversionError, match=r"not valid against the XSD.*\n.*'x'"):
            convert(orders_xsd, orders_proto, invalid_xml)

    def test_lax_reports_and_continues(self, orders_xsd, orders_proto, invalid_xml):
        r = convert(orders_xsd, orders_proto, invalid_xml, validate=False)
        assert len(r.validation_errors) == 1 and "'x'" in r.validation_errors[0]
        assert r.message.order_id == 42


class TestRequired:
    def test_missing_required(self, orders_xsd, data, order_xml):
        with pytest.raises(ConversionError, match=r"not populated: line\[0\]\.gift_wrap"):
            convert(orders_xsd, data / "required.proto", order_xml)


# --------------------------------------------------------------------------- focused cases

SIMPLE_XSD = """
<xs:simpleType name="Ints"><xs:list itemType="xs:int"/></xs:simpleType>
<xs:element name="r">
  <xs:complexType>
    <xs:sequence>
      <xs:element name="one" type="xs:string" minOccurs="0"/>
      <xs:element name="many" type="xs:string" minOccurs="0" maxOccurs="unbounded"/>
      <xs:element name="ints" type="Ints" minOccurs="0"/>
      <xs:element name="intRows" type="Ints" minOccurs="0" maxOccurs="unbounded"/>
      <xs:element name="sub" minOccurs="0">
        <xs:complexType><xs:sequence>
          <xs:element name="x" type="xs:int"/>
        </xs:sequence></xs:complexType>
      </xs:element>
    </xs:sequence>
    <xs:attribute name="codes" type="Ints"/>
  </xs:complexType>
</xs:element>
"""


@pytest.fixture
def run(xsd, proto, write):
    """run(proto_body, xml_text, **kw) -> Conversion against SIMPLE_XSD."""
    schema_path = xsd(SIMPLE_XSD)

    def _run(proto_body: str, xml: str, **kw) -> Conversion:
        return convert(schema_path, proto(proto_body), write("in.xml", xml), **kw)

    return _run


class TestLists:
    def test_xs_list_into_string_is_lexical_form(self, run):
        r = run("message R { string ints = 1; }", "<r><ints>1 2 3</ints></r>")
        assert r.message.ints == "1 2 3"

    def test_xs_list_attribute(self, run):
        r = run("message R { repeated int32 codes = 1; }", '<r codes="4 5"/>')
        assert list(r.message.codes) == [4, 5]

    def test_repeated_list_elements_keep_boundaries(self, run):
        r = run(
            "message R { repeated string int_rows = 1; }",
            "<r><intRows>1 2</intRows><intRows>3</intRows></r>",
        )
        assert list(r.message.int_rows) == ["1 2", "3"]

    def test_single_list_into_singular_int_fails_clearly(self, run):
        with pytest.raises(ConversionError, match=r"^R/ints: '1 2' is not an integer$"):
            run("message R { int32 ints = 1; }", "<r><ints>1 2</ints></r>")

    def test_repeated_element_into_singular_field_is_lossy_error(self, run):
        with pytest.raises(ConversionError, match=r"R/many: XML has 2 occurrences .*not repeated"):
            run("message R { string many = 1; }", "<r><many>a</many><many>b</many></r>")

    def test_single_occurrence_of_repeated_element(self, run):
        r = run("message R { repeated string many = 1; }", "<r><many>a</many></r>")
        assert list(r.message.many) == ["a"]


WILDCARD_XSD = """
<xs:element name="r">
  <xs:complexType>
    <xs:sequence>
      <xs:element name="one" type="xs:string"/>
      <xs:any processContents="lax" minOccurs="0" maxOccurs="unbounded"/>
    </xs:sequence>
    <xs:anyAttribute processContents="lax"/>
  </xs:complexType>
</xs:element>
"""


class TestWildcardsAndSkipping:
    def test_any_element_and_any_attribute_are_skipped(self, xsd, proto, write):
        r = convert(
            xsd(WILDCARD_XSD),
            proto("message R { string one = 1; }"),
            write("w.xml", '<r foo="1"><one>x</one><extra>y</extra></r>'),
        )
        assert r.message.one == "x"
        assert sorted(r.skipped) == ["R/extra", "R/foo"]

    def test_xmlns_and_xsi_attributes_ignored(self, run):
        r = run(
            "message R { string one = 1; }",
            '<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
            ' xsi:noNamespaceSchemaLocation="s.xsd"><one>x</one></r>',
        )
        assert r.skipped == []

    def test_unknown_child_fills_via_legacy_rules(self):
        """Without XSD info a list means repeated occurrences."""
        from google.protobuf import descriptor_pb2, descriptor_pool

        fdp = descriptor_pb2.FileDescriptorProto(name="w.proto", package="w", syntax="proto3")
        m = fdp.message_type.add(name="W")
        m.field.add(name="v", number=1, type=9, label=3)  # repeated string
        pool = descriptor_pool.DescriptorPool()
        pool.Add(fdp)
        msg: Any = message_class(pool, pool.FindMessageTypeByName("w.W"))()
        ctx = FillContext()
        fill(msg, {"v": ["a", "b"], "zzz": 1}, "W", ctx)
        assert list(msg.v) == ["a", "b"] and ctx.skipped == ["W/zzz"]


NIL_XSD = """
<xs:element name="r">
  <xs:complexType><xs:sequence>
    <xs:element name="n" type="xs:int" nillable="true" minOccurs="0" maxOccurs="unbounded"/>
    <xs:element name="one" type="xs:int" nillable="true" minOccurs="0"/>
  </xs:sequence></xs:complexType>
</xs:element>
"""
NIL_NS = 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'


class TestEmptyAndNil:
    def test_nil_scalar_left_unset(self, xsd, proto, write):
        r = convert(
            xsd(NIL_XSD),
            proto("message R { optional int32 one = 1; }"),
            write("n.xml", f'<r {NIL_NS}><one xsi:nil="true"/></r>'),
        )
        assert not r.message.HasField("one") and r.skipped == []

    def test_nil_occurrences_skipped_in_repeated_scalar(self, xsd, proto, write):
        r = convert(
            xsd(NIL_XSD),
            proto("message R { repeated int32 n = 1; }"),
            write("n.xml", f'<r {NIL_NS}><n>1</n><n xsi:nil="true"/><n>3</n></r>'),
        )
        assert list(r.message.n) == [1, 3]

    def test_nil_inside_list_without_xsd_info(self, orders_proto):
        pool, _, _ = load_proto(orders_proto)
        msg: Any = message_class(pool, pool.FindMessageTypeByName("acme.v1.Line"))()
        fill(msg, {"qty": [{"@xsi:nil": "true"}]}, "Line", FillContext())
        assert not msg.qty

    def test_empty_complex_child_sets_presence(self, run):
        r = run(
            "message R { Sub sub = 1; } message Sub { int32 x = 1; }", "<r><sub><x>1</x></sub></r>"
        )
        assert r.message.sub.x == 1

    def test_absent_element_leaves_field_unset(self, run):
        r = run("message R { Sub sub = 1; } message Sub { int32 x = 1; }", "<r/>")
        assert not r.message.HasField("sub")

    def test_none_value_on_repeated_is_noop(self, orders_proto):
        pool, _, _ = load_proto(orders_proto)
        msg: Any = message_class(pool, pool.FindMessageTypeByName("acme.v1.Order"))()
        f = msg.DESCRIPTOR.fields_by_name["line"]
        set_field(msg, f, None, "p", FillContext())
        assert len(msg.line) == 0

    def test_fill_none_is_noop(self, orders_proto):
        pool, _, _ = load_proto(orders_proto)
        msg: Any = message_class(pool, pool.FindMessageTypeByName("acme.v1.Line"))()
        fill(msg, None, "p", FillContext())
        assert msg.ByteSize() == 0

    def test_scalar_data_goes_to_text_field(self, orders_proto):
        pool, _, _ = load_proto(orders_proto)
        msg: Any = message_class(pool, pool.FindMessageTypeByName("acme.v1.Ping"))()
        fill(msg, "hello", "p", FillContext())
        assert msg.value == "hello"

    def test_text_without_text_field_is_skipped(self, orders_proto):
        pool, _, _ = load_proto(orders_proto)
        msg: Any = message_class(pool, pool.FindMessageTypeByName("acme.v1.Line"))()
        ctx = FillContext()
        fill(msg, "hello", "Line", ctx)
        assert ctx.skipped == ["Line/#text"]


MAP_XSD = """
<xs:complexType name="Pt"><xs:sequence>
  <xs:element name="x" type="xs:int"/>
</xs:sequence></xs:complexType>
<xs:element name="r">
  <xs:complexType><xs:sequence>
    <xs:element name="pts" minOccurs="0" maxOccurs="unbounded">
      <xs:complexType><xs:sequence>
        <xs:element name="key" type="xs:string" minOccurs="0"/>
        <xs:element name="value" type="Pt" minOccurs="0"/>
      </xs:sequence></xs:complexType>
    </xs:element>
    <xs:element name="nums" minOccurs="0" maxOccurs="unbounded">
      <xs:complexType><xs:sequence>
        <xs:element name="key" type="xs:int"/>
        <xs:element name="value" type="xs:string" minOccurs="0"/>
      </xs:sequence></xs:complexType>
    </xs:element>
    <xs:element name="flat" type="xs:string" minOccurs="0" maxOccurs="unbounded"/>
  </xs:sequence></xs:complexType>
</xs:element>
"""
MAP_PROTO = """
message Pt { int32 x = 1; }
message R {
  map<string, Pt> pts = 1;
  map<int32, string> nums = 2;
  map<string, string> flat = 3;
}
"""


class TestMaps:
    @pytest.fixture
    def run_map(self, xsd, proto, write):
        def _run(xml: str) -> Any:
            return convert(xsd(MAP_XSD), proto(MAP_PROTO), write("m.xml", xml)).message

        return _run

    def test_message_values(self, run_map):
        m = run_map(
            "<r><pts><key>a</key><value><x>1</x></value></pts>"
            "<pts><key>b</key><value><x>2</x></value></pts></r>"
        )
        assert {k: v.x for k, v in m.pts.items()} == {"a": 1, "b": 2}

    def test_message_value_absent_creates_entry(self, run_map):
        m = run_map("<r><pts><key>a</key></pts></r>")
        assert list(m.pts) == ["a"]

    def test_int_keys_and_missing_scalar_value(self, run_map):
        m = run_map("<r><nums><key>7</key><value>seven</value></nums><nums><key>8</key></nums></r>")
        assert dict(m.nums) == {7: "seven"}

    def test_missing_key(self, run_map):
        with pytest.raises(ConversionError, match=r"R/pts\[0\]: map entry has no key"):
            run_map("<r><pts><value><x>1</x></value></pts></r>")

    def test_entry_must_be_element(self, run_map):
        with pytest.raises(ConversionError, match=r"R/flat\[0\]: map entry needs <key>"):
            run_map("<r><flat>oops</flat></r>")


class TestXsdHelpers:
    def test_children_and_attributes(self, orders_xsd):
        el = load_schema(orders_xsd).elements["order"]
        kids = xsd_children(el.type)
        assert "line" in kids and "refs" in kids
        assert list(xsd_attributes(el.type)) == ["version"]

    def test_simple_types_have_none(self, orders_xsd):
        el = load_schema(orders_xsd).elements["ping"]
        assert xsd_children(el.type) == {} and xsd_attributes(el.type) == {}
        assert xsd_children(None) == {} and xsd_attributes(None) == {}

    def test_repeated(self, orders_xsd):
        kids = xsd_children(load_schema(orders_xsd).elements["order"].type)
        assert xsd_repeated(kids["line"]) and not xsd_repeated(kids["paid"])
        assert not xsd_repeated(None)
        attr = xsd_attributes(load_schema(orders_xsd).elements["order"].type)["version"]
        assert not xsd_repeated(attr)


class TestSerialize:
    def test_unknown_format(self, msg):
        with pytest.raises(ConversionError, match="Unknown format"):
            serialize(msg, "json")

    def test_text_is_utf8(self, run):
        r = run("message R { string one = 1; }", "<r><one>déjà</one></r>")
        assert "déjà".encode() in r.serialize("text")
