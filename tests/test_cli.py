"""End-to-end tests for the command-line interface."""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import xml2pb
from xml2pb import convert
from xml2pb.cli import build_parser, main


class _FakeStdio:
    def __init__(self, data: bytes = b""):
        self.buffer = io.BytesIO(data)


@pytest.fixture
def work(tmp_path, data) -> Path:
    """A writable copy of the test data directory."""
    for f in data.iterdir():
        shutil.copy(f, tmp_path / f.name)
    return tmp_path


@pytest.fixture
def args(work):
    return [str(work / "orders.xsd"), str(work / "orders.proto"), str(work / "order.xml")]


@pytest.fixture
def expected(work) -> bytes:
    return convert(work / "orders.xsd", work / "orders.proto", work / "order.xml").serialize()


class TestConvert:
    def test_default_output_is_binary_next_to_xml(self, work, args, expected, capsys):
        assert main(args) == 0
        out = work / "order.pb"
        assert out.read_bytes() == expected
        err = capsys.readouterr().err
        assert f"wrote {out}" in err
        assert "note: 2 XML node(s) not in proto were skipped (use -v to list)" in err

    def test_text_format_default_extension(self, work, args):
        assert main([*args, "-f", "text"]) == 0
        assert (work / "order.txtpb").read_text(encoding="utf-8").startswith("order_id: 42\n")

    def test_explicit_output(self, work, args, expected):
        assert main([*args, "-o", str(work / "x.bin")]) == 0
        assert (work / "x.bin").read_bytes() == expected

    def test_stdout(self, args, expected, monkeypatch, capsys):
        fake = _FakeStdio()
        monkeypatch.setattr(sys, "stdout", fake)
        assert main([*args, "-o", "-"]) == 0
        assert fake.buffer.getvalue() == expected
        assert "wrote" not in capsys.readouterr().err

    def test_stdin(self, work, expected, monkeypatch):
        monkeypatch.setattr(sys, "stdin", _FakeStdio((work / "order.xml").read_bytes()))
        out = _FakeStdio()
        monkeypatch.setattr(sys, "stdout", out)
        argv = [str(work / "orders.xsd"), str(work / "orders.proto"), "-", "-o", "-"]
        assert main(argv) == 0
        assert out.buffer.getvalue() == expected

    def test_stdin_requires_output(self, work, capsys):
        with pytest.raises(SystemExit) as exc:
            main([str(work / "orders.xsd"), str(work / "orders.proto"), "-"])
        assert exc.value.code == 2
        assert "requires -o" in capsys.readouterr().err

    def test_verbose_lists_skipped(self, args, capsys):
        assert main([*args, "-v", "-f", "text", "-o", "-"]) == 0
        err = capsys.readouterr().err
        assert "skipped:\n  Order/customer/email\n  Order/note" in err

    def test_message_flag(self, work, capsys):
        (work / "p.xml").write_text('<ping xmlns="urn:acme:orders">hi</ping>', encoding="utf-8")
        argv = [str(work / "orders.xsd"), str(work / "orders.proto"), str(work / "p.xml")]
        assert main([*argv, "-m", "Ping", "-f", "text", "-o", str(work / "p.txt")]) == 0
        assert (work / "p.txt").read_text() == 'value: "hi"\n'

    def test_include_flag(self, work):
        (work / "vendor").mkdir()
        shutil.move(work / "orders.proto", work / "vendor" / "orders.proto")
        (work / "wrap.proto").write_text(
            'syntax = "proto3"; package w; import "orders.proto";'
            " message Order { acme.v1.Status status = 1; int64 order_id = 2; }"
        )
        argv = [str(work / "orders.xsd"), str(work / "wrap.proto"), str(work / "order.xml")]
        assert main([*argv, "-o", str(work / "o.pb")]) == 1  # import not found
        assert main([*argv, "-I", str(work / "vendor"), "-o", str(work / "o.pb")]) == 0

    def test_subset_warning(self, work, capsys):
        argv = [str(work / "orders.xsd"), str(work / "not_subset.proto"), str(work / "order.xml")]
        assert main([*argv, "-o", str(work / "o.pb")]) == 0
        err = capsys.readouterr().err
        assert "warning: proto fields without XSD counterpart:\n  Order.no_such_field" in err

    def test_strict(self, work, capsys):
        argv = [str(work / "orders.xsd"), str(work / "not_subset.proto"), str(work / "order.xml")]
        assert main([*argv, "--strict", "-o", str(work / "o.pb")]) == 1
        assert "error: Proto is not a subset" in capsys.readouterr().err
        assert not (work / "o.pb").exists()

    def test_invalid_xml_and_no_validate(self, work, capsys):
        xml = work / "order.xml"
        xml.write_text(xml.read_text(encoding="utf-8").replace("<qty>2", "<qty>x"), "utf-8")
        argv = [
            str(work / "orders.xsd"),
            str(work / "orders.proto"),
            str(xml),
            "-f",
            "text",
            "-o",
            "-",
        ]
        assert main(argv) == 1
        assert "error: XML is not valid against the XSD" in capsys.readouterr().err
        assert main([*argv, "--no-validate"]) == 0
        assert "warning: " in capsys.readouterr().err

    def test_refuses_to_overwrite_xml(self, work, capsys):
        xml = work / "order.pb"  # an XML file that happens to end in .pb
        shutil.copy(work / "order.xml", xml)
        before = xml.read_bytes()
        argv = [str(work / "orders.xsd"), str(work / "orders.proto"), str(xml)]
        assert main(argv) == 1
        assert "would overwrite the input XML" in capsys.readouterr().err
        assert xml.read_bytes() == before


class TestConvertErrors:
    @pytest.mark.parametrize(
        "which, message",
        [
            (0, "Could not load XSD"),
            (1, "Proto file not found"),
            (2, "Could not read XML"),
        ],
    )
    def test_missing_inputs(self, args, which, message, capsys):
        args[which] = args[which] + ".missing"
        assert main([*args, "-o", "-"]) == 1
        assert f"error: {message}" in capsys.readouterr().err

    def test_malformed_xml(self, work, capsys):
        (work / "bad.xml").write_text("<order><a></order>")
        argv = [str(work / "orders.xsd"), str(work / "orders.proto"), str(work / "bad.xml")]
        assert main([*argv, "-o", "-"]) == 1
        assert "error: Could not read XML" in capsys.readouterr().err

    def test_needs_proto_and_xml(self, work, capsys):
        with pytest.raises(SystemExit) as exc:
            main([str(work / "orders.xsd"), str(work / "orders.proto")])
        assert exc.value.code == 2
        assert "proto and xml are required" in capsys.readouterr().err


class TestGenerate:
    def test_default_output(self, work, capsys):
        assert main([str(work / "orders.xsd"), "-e", "order"]) == 0
        out = work / "orders.proto"  # replaces the copy in the work dir
        assert "message Order {" in out.read_text(encoding="utf-8")
        assert f"wrote {out}" in capsys.readouterr().err

    def test_stdout_and_options(self, work, monkeypatch):
        fake = _FakeStdio()
        monkeypatch.setattr(sys, "stdout", fake)
        argv = [
            str(work / "orders.xsd"),
            "--elements",
            "order, ping",
            "--package",
            "acme.gen",
            "--decimal",
            "string",
            "--datetime",
            "string",
            "--no-optional",
            "-o",
            "-",
        ]
        assert main(argv) == 0
        text = fake.buffer.getvalue().decode()
        assert "package acme.gen;" in text and "message Ping {" in text
        assert "  string total = 6;  // xs:decimal" in text
        assert "  string placed_at = 4;  // xs:dateTime" in text
        assert "optional" not in text

    def test_repeatable_elements_flag(self):
        a = build_parser().parse_args(["s.xsd", "-e", "a,b", "-e", "c"])
        assert a.elements == ["a,b", "c"]

    def test_rejects_proto_and_xml(self, work, capsys):
        with pytest.raises(SystemExit) as exc:
            main([str(work / "orders.xsd"), "x.proto", "-e", "order"])
        assert exc.value.code == 2
        assert "omit the proto and xml" in capsys.readouterr().err

    def test_requires_names(self, work, capsys):
        with pytest.raises(SystemExit):
            main([str(work / "orders.xsd"), "-e", " , "])
        assert "at least one element" in capsys.readouterr().err

    def test_unknown_element(self, work, capsys):
        assert main([str(work / "orders.xsd"), "-e", "nope", "-o", "-"]) == 1
        assert "error: Top-level element 'nope' not found" in capsys.readouterr().err

    def test_refuses_to_overwrite_xsd(self, work, capsys):
        xsd = work / "orders.xsd"
        before = xsd.read_bytes()
        assert main([str(xsd), "-e", "order", "-o", str(xsd)]) == 1
        assert "would overwrite the XSD" in capsys.readouterr().err
        assert xsd.read_bytes() == before

    def test_generated_proto_feeds_convert(self, work, capsys):
        assert main([str(work / "orders.xsd"), "-e", "order", "-o", str(work / "g.proto")]) == 0
        argv = [str(work / "orders.xsd"), str(work / "g.proto"), str(work / "order.xml")]
        assert main([*argv, "--strict", "-f", "text", "-o", str(work / "o.txtpb")]) == 0
        assert 'note: "leave at door"' in (work / "o.txtpb").read_text(encoding="utf-8")
        assert "skipped" not in capsys.readouterr().err


class TestMeta:
    def test_version(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert xml2pb.__version__ in capsys.readouterr().out

    def test_help_has_examples(self, capsys):
        with pytest.raises(SystemExit):
            main(["--help"])
        out = capsys.readouterr().out
        assert "convert (XSD + .proto + XML -> protobuf):" in out and "--elements" in out

    def test_python_dash_m(self, args, expected):
        r = subprocess.run(
            [sys.executable, "-m", "xml2pb", *args, "-o", "-"], capture_output=True, check=False
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout == expected

    def test_python_dash_m_error_exit_code(self, args):
        r = subprocess.run(
            [sys.executable, "-m", "xml2pb", *args[:2], "missing.xml", "-o", "-"],
            capture_output=True,
            check=False,
        )
        assert r.returncode == 1 and b"error: Could not read XML" in r.stderr
