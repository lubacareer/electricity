import os
from pathlib import Path
from xml.etree.ElementTree import ParseError

import pytest

from smd_twin_lab import xml_security
from smd_twin_lab.eda.kicad import _semantic_netlist_summary
from smd_twin_lab.importers.parsers import ArtifactParseError, parse_logical_netlist, validate_svg


@pytest.mark.parametrize("entry", ["netlist", "semantic", "svg"])
@pytest.mark.parametrize("limit", ["bytes", "nodes", "depth"])
def test_untrusted_xml_limits_are_enforced_during_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, limit: str
) -> None:
    root = "svg" if entry == "svg" else "export"
    if limit == "bytes":
        monkeypatch.setattr(xml_security, "MAX_XML_BYTES", 64)
        content = f"<{root}>" + "x" * 64 + f"</{root}>"
    elif limit == "nodes":
        monkeypatch.setattr(xml_security, "MAX_XML_NODES", 4)
        content = f"<{root}>" + "<x/>" * 4 + f"</{root}>"
    else:
        monkeypatch.setattr(xml_security, "MAX_XML_DEPTH", 4)
        content = f"<{root}><x><x><x><x/></x></x></x></{root}>"

    path = tmp_path / "artifact.xml"
    path.write_text(content, encoding="utf-8")
    parse = {
        "netlist": parse_logical_netlist,
        "semantic": _semantic_netlist_summary,
        "svg": validate_svg,
    }[entry]
    expected = xml_security.XmlLimitExceeded if entry == "semantic" else ArtifactParseError
    with pytest.raises(expected, match="limit exceeded"):
        parse(path)


def test_limits_accept_document_at_boundaries_and_harmless_doctype(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b'<!DOCTYPE export><export><nets><net name="GND"/></nets></export>'
    monkeypatch.setattr(xml_security, "MAX_XML_BYTES", len(content))
    monkeypatch.setattr(xml_security, "MAX_XML_NODES", 3)
    monkeypatch.setattr(xml_security, "MAX_XML_DEPTH", 3)
    path = tmp_path / "netlist.xml"
    path.write_bytes(content)
    assert len(parse_logical_netlist(path)[0]) == 1
    assert _semantic_netlist_summary(path) == ((), ("GND",))


def test_utf16_entity_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "netlist.xml"
    path.write_bytes(
        (
            '<?xml version="1.0" encoding="UTF-16"?>'
            '<!DOCTYPE export [<!ENTITY x "expanded">]><export>&x;</export>'
        ).encode("utf-16")
    )
    with pytest.raises(ArtifactParseError):
        parse_logical_netlist(path)


@pytest.mark.parametrize(
    "content",
    [
        b"<export>" + b"<x>" * 128 + b"</x>" * 128 + b"</export>",
        b"<export>" + b"<x/>" * 250_000 + b"</export>",
    ],
    ids=["depth", "nodes"],
)
def test_default_depth_and_node_limits_reject_large_inputs(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "netlist.xml"
    path.write_bytes(content)
    with pytest.raises(ArtifactParseError, match="limit exceeded"):
        parse_logical_netlist(path)


def test_non_regular_xml_source_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ArtifactParseError, match="regular file"):
        parse_logical_netlist(tmp_path)
    with pytest.raises(xml_security.UnsafeXmlSource, match="regular file"):
        _semantic_netlist_summary(tmp_path)


@pytest.mark.parametrize("entry", ["netlist", "semantic", "svg"])
def test_unknown_encoding_uses_normal_parse_error(tmp_path: Path, entry: str) -> None:
    root = "svg" if entry == "svg" else "export"
    path = tmp_path / "unknown.xml"
    path.write_text(f'<?xml version="1.0" encoding="not-a-codec"?><{root}/>', encoding="utf-8")
    parse = {
        "netlist": parse_logical_netlist,
        "semantic": _semantic_netlist_summary,
        "svg": validate_svg,
    }[entry]
    expected = ParseError if entry == "semantic" else ArtifactParseError
    with pytest.raises(expected, match="unsupported XML encoding"):
        parse(path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_fifo_swapped_after_stat_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fifo = tmp_path / "netlist.xml"
    os.mkfifo(fifo)
    regular = tmp_path / "regular.xml"
    regular.write_text("<export/>", encoding="utf-8")
    actual_stat = Path.stat

    def replaced_stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        if path == fifo:
            return actual_stat(regular)
        return actual_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", replaced_stat)
    with pytest.raises(ArtifactParseError, match="regular file"):
        parse_logical_netlist(fifo)
